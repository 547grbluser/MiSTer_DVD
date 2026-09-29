//============================================================================
// dec_duty.sv -- where the decoder's time goes (DVD-FORK DEBUG, telemetry).
//
// WHY. "Compute-bound" was the standing explanation for lates on busy 29.97
// content, but lates on Thayer's Quest depend on the OUTPUT MODE (~7.4/s on
// Progressive, 0 on Interlaced, same disc) -- which decode cost alone cannot
// explain. The counters that existed (lates, drops, pickups) all describe the
// DISPLAY; none could say whether the decoder was busy, starved, or parked
// waiting for the display. docs/decode_pacing.md.
//
// WHAT. Every clk_dec cycle is put in exactly ONE of four VLD classes, plus one
// independent motion-comp class:
//
//   disp    picbuf_busy: the VLD reached the NEXT picture header and is parked
//           there until the display picks up the picture ahead of it
//           (motcomp_picbuf STATE_IP_FRAME_0 / STATE_WAIT_0). The decoder is
//           IDLE because of the display -- if this is large while lates
//           happen, the lates are a scheduling defect, not decode cost.
//   starve  ~disp && ~getbits_valid: no bitstream to parse (VBUF/getbits empty).
//   back    ~disp && getbits_valid && ~vld_en: parse stalled by downstream
//           (rld/mvec fifo almost full, motcomp flushing) or a wait state --
//           the decode pipeline is the bottleneck.
//   (active = the remainder; the host computes it from elapsed time.)
//   ref     recon_ref_stall: motion-comp recon waiting on REFERENCE PIXELS
//           from DDR3 -- the memory share of decode time. Compared between
//           Progressive and Interlaced on the same scene, it separates DDR3
//           contention (display reads are top priority) from pure compute.
//
// HOW. Each class is a free-running 28-bit cycle count; the telemetry word is
// bits [27:12] (1 LSB = 4096 cycles = 50.6 us at 81 MHz). At 100 % duty a word
// advances ~19.8 k/s and wraps every ~3.3 s, well above the host's 0.5 s poll,
// and it moves at most once per 4096 cycles, so dvd_telem's two-agree sampler
// sees it settled. Reset on hard_rst ONLY (the pin): no flush, seek or soft
// reset zeroes these, so a window never contains a reset.
//
// PER PICTURE (added 2026-09-29, docs/decode_pacing.md §7 "Instrument"). The
// duty classes are averages; lates come from the pictures in the TAIL, and a
// 0.5 s row cannot say which picture missed. A picture's decode is one
// picbuf_busy-LOW stretch: picbuf_busy falls when the picbuf lets the VLD start
// the picture and rises at the NEXT picture's header (update_picture_buffers,
// once per frame -- a field pair is one picture here). Its decode time counts
// the stretch's NON-STARVED cycles (back + active, the same "decode ms" the
// duty words give on average; waiting for bitstream is not decode cost).
//   pic_max   the longest picture in the last COMPLETED window of 2^WIN_BITS
//             cycles (0.83 s at 81 MHz: longer than the 250 ms poll, so no
//             window goes unread). Held for the whole next window, so the
//             sampler sees it settled. Same unit as the duty words.
//   pic_n     free-running count of pictures completed.
//   pic_over  ... of those whose decode time exceeded ONE FRAME PERIOD of the
//             content (frame_rate_code, exact at clk_dec = 81 MHz).
// A dropped B picture fires no update, so its (skipped) parse is folded into
// the previous stretch; the max can only read long there, never short.
//============================================================================
module dec_duty (
    input  wire        clk,
    input  wire        rst,            // active LOW, pin reset only (mpeg2video hard_rst)
    input  wire        picbuf_busy,
    input  wire        getbits_valid,
    input  wire        vld_en,
    input  wire        ref_stall,
    input  wire  [3:0] frame_rate_code,  // par. 6.3.3 Table 6-4, the current sequence
    output wire [15:0] disp_cnt,
    output wire [15:0] starve_cnt,
    output wire [15:0] back_cnt,
    output wire [15:0] ref_cnt,
    output wire [15:0] pic_max,          // cycles/4096 of the longest picture, last window
    output wire [15:0] pic_n,            // pictures completed (wraps)
    output wire [15:0] pic_over          // ... that took longer than one frame period (wraps)
);
    parameter WIN_BITS = 26;             // 2^26 / 81 MHz = 0.83 s (a bench shortens it)
    reg [27:0] c_disp, c_starve, c_back, c_ref;

    wire is_disp   = picbuf_busy;
    wire is_starve = ~picbuf_busy & ~getbits_valid;
    wire is_back   = ~picbuf_busy &  getbits_valid & ~vld_en;

    always @(posedge clk)
        if (~rst) begin
            c_disp <= 28'd0; c_starve <= 28'd0; c_back <= 28'd0; c_ref <= 28'd0;
        end else begin
            if (is_disp)   c_disp   <= c_disp   + 28'd1;
            if (is_starve) c_starve <= c_starve + 28'd1;
            if (is_back)   c_back   <= c_back   + 28'd1;
            if (ref_stall) c_ref    <= c_ref    + 28'd1;
        end

    // ---- per picture ----
    // One frame period in clk_dec cycles, 81e6 / fps exactly (1001-rate codes:
    // 81e6 * 1001 / (1000 * n)). Codes outside 1..8 are forbidden in MPEG-2;
    // they fall back to 29.97.
    // A continuous assign, not an always @* case: iverilog never runs an always @*
    // whose inputs are constant from time zero, and the threshold read X in the bench.
    wire [22:0] thr = (frame_rate_code == 4'd1) ? 23'd3378375 :   // 23.976
                      (frame_rate_code == 4'd2) ? 23'd3375000 :   // 24
                      (frame_rate_code == 4'd3) ? 23'd3240000 :   // 25
                      (frame_rate_code == 4'd5) ? 23'd2700000 :   // 30
                      (frame_rate_code == 4'd6) ? 23'd1620000 :   // 50
                      (frame_rate_code == 4'd7) ? 23'd1351350 :   // 59.94
                      (frame_rate_code == 4'd8) ? 23'd1350000 :   // 60
                                                  23'd2702700;    // 29.97 (code 4)

    reg               busy_q;
    reg        [27:0] c_pic;             // non-starved cycles of the picture under decode (saturates)
    reg        [27:0] m_cur, m_pub;      // this window's max so far / the last window's
    reg [WIN_BITS-1:0] win;
    reg        [15:0] n_pic, n_over;

    wire        pic_end = picbuf_busy & ~busy_q;   // the next header: this picture is parsed
    wire        win_end = &win;
    wire [27:0] m_new   = (pic_end && (c_pic > m_cur)) ? c_pic : m_cur;

    always @(posedge clk)
        if (~rst) begin
            busy_q <= 1'b1; c_pic <= 28'd0; m_cur <= 28'd0; m_pub <= 28'd0;
            win <= {WIN_BITS{1'b0}}; n_pic <= 16'd0; n_over <= 16'd0;
        end else begin
            busy_q <= picbuf_busy;
            if (pic_end) c_pic <= 28'd0;
            else if (~picbuf_busy & getbits_valid & ~&c_pic) c_pic <= c_pic + 28'd1;
            win <= win + 1'b1;
            if (win_end) begin m_pub <= m_new; m_cur <= 28'd0; end
            else m_cur <= m_new;
            if (pic_end) begin
                n_pic <= n_pic + 16'd1;
                if (c_pic > {5'd0, thr}) n_over <= n_over + 16'd1;
            end
        end

    assign pic_max    = m_pub[27:12];
    assign pic_n      = n_pic;
    assign pic_over   = n_over;

    assign disp_cnt   = c_disp[27:12];
    assign starve_cnt = c_starve[27:12];
    assign back_cnt   = c_back[27:12];
    assign ref_cnt    = c_ref[27:12];
endmodule
