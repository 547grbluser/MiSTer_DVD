//============================================================================
//  pts_assoc.sv — attach each PES PTS to the picture it belongs to, by position.
//
//  THE RULE (ISO 13818-1 2.4.3.7): a PES packet's PTS belongs to the first
//  access unit -- picture -- whose start code lies at or after the first byte
//  of that PES packet's payload. DVDs carry a video PTS only about once per
//  VOBU (~11 pictures), flat .mpg files about once per picture; either way
//  the association is positional, and the two positions it compares are exact
//  by construction (docs/av_sync.md "THE STC IS A CLOCK"):
//
//    stamp  the byte position the marked payload byte will occupy in the VBUF
//           (dvd/vbuf_pos.sv, counted from the flush)
//    header the byte position of the picture start code the vld just parsed
//           (getbits_fifo.bitpos - 32, same origin -- pinned by pts_assoc_tb)
//
//  A FIFO of {pts, stamp}. The HEAD (and the entry behind it) live in
//  registers so the compare below is a register read, never an async array
//  read -- the LUT-RAM pattern this project keeps paying for; the rest of the
//  queue is a sync-read circular buffer (area pass 2026-09-10: this was a
//  16-deep shift register of 57-bit entries = 912 flops that packed at under
//  two per ALM, 563 ALMs for a queue that is rarely more than 3 deep). At
//  every picture header ONE
//  decision, in one cycle: if the head stamp lies at or before the start code,
//  pop it and it is this picture's tag; otherwise this picture has none.
//  The tag is REGISTERED and held until the next header. motcomp_picbuf reads
//  it at its STATE_UPDATE for the picture, which is >= 3 cycles after the
//  header (update_picture_buffers -> mvec fifo -> picbuf) and can be no later
//  than the next header (the vld is frozen at the header until picbuf has
//  rotated).
//
//  ★ A SECOND FIELD NEVER TAKES A TAG (DVD-FORK FIX, 2026-09-29). The field
//  pair is ONE coded frame, so the second field's start code begins no access
//  unit: a mark that lies between the two fields of a pair belongs to the NEXT
//  frame and stays queued for it. At a second-field header nothing is popped,
//  the tag reads invalid, and last_hdr does not advance (otherwise pop_drop
//  would discard that pending mark the very next cycle).
//  This used to tag the second field and have the scheduler subtract one field
//  (tag_second). MEASURED on Thayer's Quest VTS_08 (field coded): the mark that
//  lands there carries exactly the NEXT I-frame's display time (16.377 s, the B
//  it landed on displays at 16.310), so the frame was stamped ~50 ms late, the
//  next correct tag read as a 2-frame BACKWARD jump, and disp_sched treated
//  that as a content discontinuity -- an audio flush every few clips, and when
//  it fell within the re-phase cooldown of a real cell join, audio left 1.4 s
//  EARLY for the whole next clip (docs/nonseamless_audio.md 2b). tag_second and
//  the picbuf re-latch path it fed are kept as ports and now always read 0 /
//  never fire; gated by run_pts_assoc.sh's pts_thayer arm.
//
//  Modular compare over 24 bits of bytes (16 MB) with the MSB-of-difference
//  test; correct while the tracked distance is under 8 MB against a 2 MB VBUF.
//  A stamp RATE LIMIT keeps a per-picture-PTS .mpg (a stamp every ~30 KB, up
//  to a whole 2 MB VBUF in flight) from overflowing DEPTH=16: a stamp is
//  accepted only once MIN_GAP bytes have been written since the last accepted
//  one, so at most ~14 are ever in flight and the ones kept are still exact.
//  A full FIFO drops the NEWEST and counts it (dbg_ovf), never the oldest.
//
//  No `function`, no N'(expr) cast: Quartus 17 miscompiles both silently.
//============================================================================

`default_nettype none

module pts_assoc #(
    parameter int DEPTH     = 16,
    parameter int PW        = 24,        // compared position width, bytes (modular)
    parameter int MIN_GAP_W = 16         // 2^16 = 64 KB between accepted stamps
) (
    input  wire          clk,            // clk_dec
    input  wire          rst_n,          // sync_rst (decoder)
    input  wire          flush,          // VBUF flush LEVEL: drop everything in flight

    // write side: a PTS-bearing PES payload's first byte was written at stamp_pos
    input  wire          stamp_valid,
    input  wire [32:0]   stamp_pts,
    input  wire [PW-1:0] stamp_pos,

    // read side: the vld parsed a picture header (one-cycle pulse)
    input  wire          hdr_pulse,
    input  wire [PW-1:0] hdr_pos,        // byte position of the start code
    input  wire          hdr_second,     // the second field of a pair

    // the tag for the picture just parsed; held until the next header
    output logic         tag_valid,
    output logic [32:0]  tag_pts,
    output logic         tag_second,
    output logic         tag_commit,     // one-cycle: tag_* just changed

    output logic [7:0]   dbg_ovf
);

    localparam int CW = $clog2(DEPTH + 1);
    localparam int AW = $clog2(DEPTH);
    localparam int EW = 33 + PW;               // one entry: {pts, pos}

    // ---- entry store ----------------------------------------------------------
    // Every accepted stamp is written to the ring at wr_ptr; the two entries at
    // the read side (head = rd_ptr, nxt = rd_ptr+1) are ALSO held in registers
    // so a pop needs no RAM latency: head <= nxt, nxt <= the entry at rd_ptr+2,
    // which the ring read port fetched the cycle before. The read address is
    // computed from the POST-pop pointer, so back-to-back pops (a pop_tag at a
    // header followed by a pop_drop the next cycle) each find their third entry
    // ready. A same-cycle write to the address being read is bypassed
    // (byp_*), the one read-during-write case a sync RAM cannot serve itself.
    // No init, no reset of the ring: entries beyond cnt are unreachable.
    (* ramstyle = "no_rw_check" *) logic [EW-1:0] mem [DEPTH];
    logic [AW-1:0] rd_ptr, wr_ptr;
    logic [CW-1:0] cnt;
    logic [32:0]   head_pts, nxt_pts;
    logic [PW-1:0] head_pos, nxt_pos;
    logic [EW-1:0] ram_q, byp_q;
    logic          byp_v;
    logic [PW-1:0] last_hdr;             // position of the last header parsed
    logic          hdr_seen;

    wire full  = (cnt == DEPTH);
    wire empty = (cnt == '0);

    // head stamp at or before a position: (pos - head) has its MSB clear
    wire [PW-1:0] d_hdr  = hdr_pos  - head_pos;
    wire [PW-1:0] d_last = last_hdr - head_pos;
    wire head_le_hdr  = !empty && !d_hdr[PW-1];
    wire head_le_last = !empty && hdr_seen && !d_last[PW-1];

    // a second field begins no access unit: it neither claims a mark nor moves
    // the drop horizon (see the header)
    wire hdr_frame = hdr_pulse && !hdr_second;
    wire pop_tag  = hdr_frame && head_le_hdr;            // this picture's tag
    wire pop_drop = !hdr_pulse && head_le_last;          // belongs to no picture
    wire do_pop   = pop_tag || pop_drop;

    // stamp rate limit (modular, forward-only distance)
    logic [PW-1:0] last_stamp;
    logic          gap_armed;
    localparam [PW-1:0] MIN_GAP = (1 << MIN_GAP_W);
    wire [PW-1:0] gap_d  = stamp_pos - last_stamp;
    wire gap_ok  = !gap_armed || (gap_d >= MIN_GAP);
    wire do_push = stamp_valid && !full && gap_ok;

    wire [EW-1:0] push_d  = {stamp_pts, stamp_pos};
    wire [CW-1:0] cnt_pp  = do_pop ? (cnt - 1'b1) : cnt;        // count after this pop
    wire [AW-1:0] rd_nxt  = do_pop ? (rd_ptr + 1'b1) : rd_ptr;  // rd_ptr after this pop
    wire [AW-1:0] rd_addr = rd_nxt + 2'd2;                       // entry that becomes `third`
    wire [EW-1:0] third   = byp_v ? byp_q : ram_q;               // entry at rd_ptr+2 (valid when cnt >= 3)

    always_ff @(posedge clk) begin
        if (do_push) mem[wr_ptr] <= push_d;
        ram_q <= mem[rd_addr];
        byp_v <= do_push && (wr_ptr == rd_addr);
        byp_q <= push_d;
    end

    always_ff @(posedge clk) begin
        tag_commit <= 1'b0;
        if (!rst_n || flush) begin
            cnt        <= '0;
            rd_ptr     <= '0;
            wr_ptr     <= '0;
            gap_armed  <= 1'b0;
            hdr_seen   <= 1'b0;
            last_hdr   <= '0;
            last_stamp <= '0;
            tag_valid  <= 1'b0;
            tag_pts    <= '0;
            tag_second <= 1'b0;
            head_pts   <= '0;
            head_pos   <= '0;
            nxt_pts    <= '0;
            nxt_pos    <= '0;
            if (!rst_n) dbg_ovf <= '0;
        end else begin
            if (hdr_pulse) begin
                if (!hdr_second) begin
                    last_hdr <= hdr_pos;
                    hdr_seen <= 1'b1;
                end
                tag_valid  <= head_le_hdr && !hdr_second;
                tag_pts    <= head_pts;
                tag_second <= 1'b0;             // see the header: a second field is never tagged
                tag_commit <= 1'b1;
            end
            if (do_push) begin
                last_stamp <= stamp_pos;
                gap_armed  <= 1'b1;
                wr_ptr     <= wr_ptr + 1'b1;
            end else if (stamp_valid && full && gap_ok && ~&dbg_ovf)
                dbg_ovf <= dbg_ovf + 1'b1;
            if (do_pop) rd_ptr <= rd_ptr + 1'b1;
            cnt <= cnt_pp + (do_push ? 1'b1 : 1'b0);
            // the registered window
            if (do_pop) begin
                {head_pts, head_pos} <= (cnt_pp >= 1) ? {nxt_pts, nxt_pos} : push_d;
                {nxt_pts,  nxt_pos}  <= (cnt_pp >= 2) ? third              : push_d;
            end else begin
                if (do_push && (cnt == 0)) {head_pts, head_pos} <= push_d;
                if (do_push && (cnt == 1)) {nxt_pts,  nxt_pos}  <= push_d;
            end
        end
    end

endmodule

`default_nettype wire
