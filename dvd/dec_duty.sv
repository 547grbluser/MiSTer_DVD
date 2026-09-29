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
//============================================================================
module dec_duty (
    input  wire        clk,
    input  wire        rst,            // active LOW, pin reset only (mpeg2video hard_rst)
    input  wire        picbuf_busy,
    input  wire        getbits_valid,
    input  wire        vld_en,
    input  wire        ref_stall,
    output wire [15:0] disp_cnt,
    output wire [15:0] starve_cnt,
    output wire [15:0] back_cnt,
    output wire [15:0] ref_cnt
);
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

    assign disp_cnt   = c_disp[27:12];
    assign starve_cnt = c_starve[27:12];
    assign back_cnt   = c_back[27:12];
    assign ref_cnt    = c_ref[27:12];
endmodule
