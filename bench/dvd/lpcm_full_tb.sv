//============================================================================
//  lpcm_full_tb.sv -- every DVD-Video LPCM format through dvd/lpcm_unpack.sv (+ its
//  lpcm_hb), pair for pair against tools/lpcm_model.py. docs/lpcm_full.md.
//
//  The fixtures (tools/lpcm_model.py --fixture, CASES) are 18 arms, A-R: mono at
//  16/20/24 bit, 3-8 channels downmixed, 96 kHz through the half-band, 96 kHz on a
//  96 kHz link (original path and downmix only), a stereo control, and two pressure
//  arms (Q: the FIFO full, R: the half-band's ring backed up) and T (mono 16-bit into
//  a full FIFO: a pair per 2 bytes, so pairs are in flight when it fills). Each arm resets the
//  DUT -- but NOT lpcm_hb's ring RAM, so a half-band arm after another starts with the
//  previous one's samples in it, as on hardware after a track change.
//
//  Scoring: every popped pair is compared with !== (an X can never pass), the count
//  must be exact, and nothing may pop after the last expected pair.
//  Output: "PASS <arm>" / "FAIL <arm>: ..." per arm, then LPCM_FULL_TB: ALL TESTS
//  PASSED, or $fatal.
//============================================================================
`timescale 1ns/1ps

module lpcm_full_tb;
    `include "lpcm_full_sizes.svh"

    logic clk = 0; always #5 clk = ~clk;
    logic        rst;
    logic [1:0]  quant;
    logic [2:0]  nch_m1;
    logic        dec;
    logic        wr_en;
    logic [7:0]  wr_data;
    logic        full;
    logic        aud_ce;
    logic signed [15:0] audio_l, audio_r;
    logic        aud_valid;

    lpcm_unpack #(.FIFO_AW(6)) dut (
        .clk(clk), .rst(rst), .quant(quant), .le(1'b0), .nch_m1(nch_m1), .dec(dec),
        .wr_en(wr_en), .wr_data(wr_data), .full(full), .afull(),
        .aud_ce(aud_ce), .audio_l(audio_l), .audio_r(audio_r), .aud_valid(aud_valid),
        .cp_mode(1'b0), .cp_step(1'b0)
    );

    logic [7:0]  bytes [0:NBYTE-1];
    logic [31:0] pairs [0:NPAIR-1];
    logic [119:0] cases [0:NCASE-1];   // arm q nm1 dec press | bo bn po pn
    initial begin
        $readmemh("lpcm_full_bytes.hex", bytes);
        $readmemh("lpcm_full_pairs.hex", pairs);
        $readmemh("lpcm_full_cases.hex", cases);
    end

    integer errs = 0, arm_errs;
    integer ci, bi, got, want_n, p0, b0, bn, press;
    logic [7:0] arm;
    integer ce_div, ce_cnt, fed_gap, idle;
    realtime t_arm;
    logic feeding;

    // the byte source: honours `full` exactly as dvd_audio_decode's dispatcher does
    // (the byte is taken on the cycle it is offered with full low)
    always @(negedge clk) begin
        wr_en = 1'b0;
        if (feeding && (bi < bn) && !full && (fed_gap == 0)) begin
            wr_en   = 1'b1;
            wr_data = bytes[b0 + bi];
        end
    end
    always @(posedge clk) begin
        if (wr_en) begin
            bi = bi + 1;
            fed_gap = press ? 0 : 7;              // fast arms: a byte per 8 cycles
        end else if (fed_gap > 0) fed_gap = fed_gap - 1;
    end

    // the drain: aud_ce every ce_div cycles
    always @(negedge clk) begin
        aud_ce = 1'b0;
        if (feeding) begin
            if (ce_cnt == 0) begin aud_ce = 1'b1; ce_cnt = ce_div - 1; end
            else ce_cnt = ce_cnt - 1;
        end
    end

    // capture + score
    always @(posedge clk) begin
        if (feeding && aud_valid) begin
            if (got >= want_n) begin
                if (arm_errs < 10) $display("FAIL %s: an extra pair %04h %04h after the %0d expected",
                                           arm, audio_l, audio_r, want_n);
                arm_errs = arm_errs + 1;
            end else if ({audio_l, audio_r} !== pairs[p0 + got]) begin
                if (arm_errs < 10) $display("FAIL %s: pair %0d = %04h %04h, want %08h",
                                           arm, got, audio_l, audio_r, pairs[p0 + got]);
                arm_errs = arm_errs + 1;
            end
            got = got + 1;
        end
    end

    initial begin
        rst = 1; wr_en = 0; aud_ce = 0; feeding = 0; quant = 0; nch_m1 = 3'd1; dec = 0;
        bi = 0; bn = 0; b0 = 0; fed_gap = 0; ce_cnt = 0; ce_div = 2; press = 0;
        got = 0; want_n = 0; p0 = 0; arm = "?"; arm_errs = 0;
        repeat (4) @(posedge clk);
        for (ci = 0; ci < NCASE; ci = ci + 1) begin
            arm    = cases[ci][119:112];
            quant  = cases[ci][109:108];
            nch_m1 = cases[ci][106:104];
            dec    = cases[ci][100];
            press  = cases[ci][96];
            b0     = cases[ci][95:72];
            bn     = cases[ci][71:48];
            p0     = cases[ci][47:24];
            want_n = cases[ci][23:0];
            ce_div = press ? 150 : 2;
            arm_errs = 0; got = 0; bi = 0; fed_gap = 0; ce_cnt = 0;
            // reset between arms (the ring RAM keeps the last arm's samples)
            @(negedge clk) rst = 1;
            repeat (3) @(posedge clk);
            @(negedge clk) rst = 0;
            feeding = 1;
            // run until every byte is in and every expected pair is out, then 2000
            // more cycles in which nothing may pop
            idle = 0; t_arm = $realtime;
            while (idle < 2000) begin
                @(posedge clk);
                if ((bi >= bn) && (got >= want_n)) idle = idle + 1;
                if (idle == 0 && $realtime - t_arm > 100_000_000) begin   // 100 ms an arm
                    $display("FAIL %s: TIMEOUT, %0d/%0d bytes in, %0d/%0d pairs out", arm, bi, bn, got, want_n);
                    arm_errs = arm_errs + 1; idle = 2000;
                end
            end
            feeding = 0;
            if (got != want_n && arm_errs == 0) begin
                $display("FAIL %s: %0d pairs out, want %0d", arm, got, want_n);
                arm_errs = arm_errs + 1;
            end
            if (arm_errs == 0) $display("PASS %s (%0d pairs)", arm, want_n);
            errs = errs + arm_errs;
        end
        if (errs == 0) $display("LPCM_FULL_TB: ALL TESTS PASSED");
        else $fatal(1, "LPCM_FULL_TB: %0d errors", errs);
        $finish;
    end
endmodule
