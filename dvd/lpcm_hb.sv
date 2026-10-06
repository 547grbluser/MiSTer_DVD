//============================================================================
//  lpcm_hb.sv -- the 2:1 half-band decimator for 96 kHz LPCM on a 48 kHz HDMI link.
//  docs/lpcm_full.md §4; the golden model is tools/lpcm_model.py halfband().
//
//  Stereo s16 pairs in at 96 kHz (lpcm_unpack's downmix), pairs out at 48 kHz (its
//  pair FIFO). Dropping every other sample would fold 24-48 kHz into the audio band,
//  so each output is a 71-tap FIR, a Kaiser (beta 9) windowed half-band in Q1.17:
//  flat to 0.0006 dB below 20 kHz, below -88 dB from 28 kHz. Every even offset from
//  the centre is zero, so an output is 37 multiply-accumulates per channel.
//
//  Output m is computed once input 2m+1 has arrived:
//      y[m] = sat16((sum_k h[k] x[2m+1-k] + 2^16) >>> 17),  x[i < 0] = 0
//  Term j of an output is tap k = 2j (j < 36; h[2j] = h[70-2j]) or the centre k = 35
//  (j = 36). Left and right run side by side (two multipliers), one term a cycle, so
//  an output takes ~41 cycles: at 27 MHz the budget is 562 a 48 kHz output.
//
//  The inputs wait in a 128-pair ring (one M10K). The output being computed reads back
//  70 pairs; `hold` stops the producer while 40 or more inputs are queued beyond it,
//  so a write can never land on a pair still to be read (the ring has 128 - 71 = 57
//  to spare; the margin covers what is in flight when hold rises). An output starts
//  only when the pair FIFO has room (out_room); nothing else writes it meanwhile.
//
//  rst: synchronous, active-high. It does not clear the ring: until 71 inputs have
//  arrived, a tap reaching before the first one is masked to 0 (as the model's x[<0]).
//============================================================================

`timescale 1ns/1ps
`default_nettype none

module lpcm_hb (
    input  wire               clk,
    input  wire               rst,
    input  wire               in_v,
    input  wire signed [15:0] in_l,
    input  wire signed [15:0] in_r,
    output logic              hold,
    input  wire               out_room,
    output logic              out_v,
    output logic signed [15:0] out_l,
    output logic signed [15:0] out_r
);

    // ---- the ring: one write port (inputs), one registered read port (taps) ----
    logic [31:0] ring [0:127];
    logic  [6:0] wa;                  // where the next input lands (= nrecv mod 128)
    logic [15:0] nrecv;               // inputs taken since reset (wraps; only differences used)
    logic [15:0] nnext;               // the next output's newest input, 2m + 1
    always_ff @(posedge clk) if (in_v) ring[wa] <= {in_l, in_r};

    wire  [15:0] ahead = nrecv - nnext;          // inputs queued from the next output's newest
    wire         owed  = (ahead != 16'd0) && !ahead[15];   // input 2m+1 has arrived
    assign hold = !ahead[15] && (ahead >= 16'd41);         // >= 40 beyond it

    // ---- the taps: j -> h ----
    logic  [5:0] j;                   // the term being issued
    logic signed [17:0] hc;
    wire   [5:0] tj = 6'd35 - j;           // h[2j] = h[70 - 2j]: tap 35 - j, j >= 18
    wire   [4:0] ti = (j == 6'd36) ? 5'd18 : (j < 6'd18) ? j[4:0] : tj[4:0];
    always_comb begin
        case (ti)
            5'd0:  hc = -18'sd1;
            5'd1:  hc =  18'sd6;
            5'd2:  hc = -18'sd16;
            5'd3:  hc =  18'sd37;
            5'd4:  hc = -18'sd74;
            5'd5:  hc =  18'sd135;
            5'd6:  hc = -18'sd229;
            5'd7:  hc =  18'sd369;
            5'd8:  hc = -18'sd569;
            5'd9:  hc =  18'sd847;
            5'd10: hc = -18'sd1230;
            5'd11: hc =  18'sd1752;
            5'd12: hc = -18'sd2469;
            5'd13: hc =  18'sd3486;
            5'd14: hc = -18'sd5022;
            5'd15: hc =  18'sd7649;
            5'd16: hc = -18'sd13480;
            5'd17: hc =  18'sd41577;
            default: hc = 18'sd65536;              // 18: the centre, 0.5
        endcase
    end
    wire   [6:0] k  = (j == 6'd36) ? 7'd35 : {j, 1'b0};

    // ---- the output being computed ----
    logic        run;                 // terms are being issued
    logic  [6:0] nlo;                 // its newest input's ring slot
    logic        warm;                // 71 inputs have been seen: every tap is real
    wire         tap_ok = warm || (k <= nlo);   // before warm, nlo is the absolute index

    // the pipeline: issue (a_*: address, tap) -> read (b_*: q valid) -> multiply
    // (c_*: products valid) -> accumulate. One term a cycle.
    logic [6:0]  a_ra;
    logic        a_v, b_v, c_v;
    logic        a_ok, b_ok;
    logic        a_last, b_last, c_last;
    logic signed [17:0] a_h, b_h;
    logic signed [33:0] pl, pr;
    logic signed [39:0] al, ar;
    logic [31:0] q;
    always_ff @(posedge clk) q <= ring[a_ra];

    wire signed [39:0] rl = al + pl + 40'sd65536;    // the last term's sum, rounded
    wire signed [39:0] rr = ar + pr + 40'sd65536;
    wire signed [22:0] yl = rl[39:17];
    wire signed [22:0] yr = rr[39:17];

    always_ff @(posedge clk) begin
        if (rst) begin
            wa <= '0; nrecv <= '0; nnext <= 16'd1;
            run <= 1'b0; j <= '0; warm <= 1'b0; nlo <= 7'd1;
            a_v <= 1'b0; b_v <= 1'b0; c_v <= 1'b0;
            out_v <= 1'b0;
            al <= '0; ar <= '0;
        end else begin
            out_v <= 1'b0;
            if (in_v) begin wa <= wa + 7'd1; nrecv <= nrecv + 16'd1; end

            // issue: start an output once the pipeline is empty, then its 37 terms.
            // !out_v: the previous output's FIFO write lands at the end of this cycle,
            // so out_room cannot count it yet (pressure arm R lost 7 pairs to a full
            // FIFO without this)
            a_v <= 1'b0;
            if (!run) begin
                if (owed && out_room && !out_v && !a_v && !b_v && !c_v) begin
                    run <= 1'b1; j <= '0;
                    nlo <= nnext[6:0];
                    al <= '0; ar <= '0;
                end
            end else begin
                a_ra   <= nlo - k;
                a_h    <= hc;
                a_ok   <= tap_ok;
                a_last <= (j == 6'd36);
                a_v    <= 1'b1;
                if (j == 6'd36) begin
                    run   <= 1'b0;
                    nnext <= nnext + 16'd2;
                    if (nnext >= 16'd69) warm <= 1'b1;      // the next output's newest is
                end else j <= j + 6'd1;                     // >= 71: every tap is real
            end
            // read: q <= ring[a_ra] (above)
            b_v <= a_v; b_h <= a_h; b_ok <= a_ok; b_last <= a_last;
            // multiply
            c_v <= b_v; c_last <= b_last;
            if (b_v) begin
                pl <= b_ok ? $signed(q[31:16]) * b_h : 34'sd0;
                pr <= b_ok ? $signed(q[15:0])  * b_h : 34'sd0;
            end
            // accumulate; on the last term round, saturate and emit
            if (c_v) begin
                al <= al + pl;
                ar <= ar + pr;
                if (c_last) begin
                    out_v <= 1'b1;
                    out_l <= (yl > 23'sd32767) ? 16'sh7FFF : (yl < -23'sd32768) ? 16'sh8000 : yl[15:0];
                    out_r <= (yr > 23'sd32767) ? 16'sh7FFF : (yr < -23'sd32768) ? 16'sh8000 : yr[15:0];
                end
            end
        end
    end

endmodule

`default_nettype wire
