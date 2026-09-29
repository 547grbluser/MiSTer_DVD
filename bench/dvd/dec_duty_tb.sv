`timescale 1ns/1ps
//============================================================================
// dec_duty_tb -- dvd/dec_duty.sv classifies every cycle into ONE VLD class.
//
// The instrument is only worth having if its classes are exclusive and its
// scale is exact: disp + starve + back + active must add up to elapsed time,
// or a host computing "active = elapsed - the rest" reports a decoder that is
// busier (or idler) than it was. Arms:
//   [1] each class alone counts exactly N*4096 cycles as N
//   [2] precedence: picbuf_busy wins over ~getbits_valid and ~vld_en (the VLD
//       parked on the display is idle BECAUSE of the display, whatever else
//       is true), and starve wins over back
//   [3] vld_en high with nothing else = active: no class counts
//   [4] ref is independent of the VLD classes
//   [5] reset clears all four (and the per-picture counters)
//   PER PICTURE (docs/decode_pacing.md §7 "Instrument"):
//   [6] one frame period is the threshold, EXACTLY: at 29.97 a picture of 2,702,700
//       decode cycles is not over and 2,702,701 is; parked cycles never count
//   [7] STARVED cycles are not decode time: 2.0 M decode + 1.0 M starved stays under
//       29.97's period (3.0 M would be over)
//   [8] the threshold follows frame_rate_code: 3.0 M cycles is under 25 fps's period
//   [9] pic_max is WINDOWED: 0 until the window closes, then the longest picture of
//       that window (3.0 M cycles = 732 units), then 0 after a window with none
// Scored with !== so an X/Z output cannot pass.
//============================================================================
module dec_duty_tb;
    reg clk = 0;
    always #5 clk = ~clk;
    reg rst = 0;
    reg picbuf_busy = 0, getbits_valid = 1, vld_en = 1, ref_stall = 0;
    reg [3:0] frc = 4'd4;
    wire [15:0] d, s, b, r, pmax, pn, po;
    integer errors = 0;
    reg [15:0] n0, o0;

    // a 16.8 M-cycle window: every picture of [6]-[8] completes inside the first one
    dec_duty #(.WIN_BITS(24)) dut (.clk(clk), .rst(rst), .picbuf_busy(picbuf_busy),
                  .getbits_valid(getbits_valid), .vld_en(vld_en), .ref_stall(ref_stall),
                  .frame_rate_code(frc),
                  .disp_cnt(d), .starve_cnt(s), .back_cnt(b), .ref_cnt(r),
                  .pic_max(pmax), .pic_n(pn), .pic_over(po));

    // one picture: `dec` decode cycles, then `st` starved ones, then the next header
    task picture(input integer dec, input integer st);
        begin
            picbuf_busy = 0; getbits_valid = 1; cycles(dec);
            getbits_valid = 0; cycles(st);
            getbits_valid = 1; picbuf_busy = 1; cycles(100);   // parked: not counted
        end
    endtask

    task chk(input [127:0] name, input [15:0] got, input [15:0] want);
        if (got !== want) begin
            $display("  FAIL %0s: got %0d want %0d", name, got, want); errors = errors + 1;
        end else $display("  ok   %0s = %0d", name, got);
    endtask

    task cycles(input integer n);
        repeat (n) @(negedge clk);
    endtask

    initial begin
        cycles(4); rst = 1; cycles(1);

        $display("[1] each class alone, 3 x 4096 cycles");
        picbuf_busy = 1; getbits_valid = 1; vld_en = 0; cycles(3*4096);
        picbuf_busy = 0; getbits_valid = 0; vld_en = 0; cycles(3*4096);
        picbuf_busy = 0; getbits_valid = 1; vld_en = 0; cycles(3*4096);
        picbuf_busy = 0; getbits_valid = 1; vld_en = 1; cycles(1);
        chk("disp",   d, 16'd3);
        chk("starve", s, 16'd3);
        chk("back",   b, 16'd3);
        chk("ref",    r, 16'd0);

        $display("[2] precedence: disp > starve > back");
        picbuf_busy = 1; getbits_valid = 0; vld_en = 0; cycles(2*4096);  // disp only
        picbuf_busy = 0; getbits_valid = 0; vld_en = 0; cycles(2*4096);  // starve only
        picbuf_busy = 0; getbits_valid = 1; vld_en = 1; cycles(1);
        chk("disp",   d, 16'd5);
        chk("starve", s, 16'd5);
        chk("back",   b, 16'd3);

        $display("[3] vld_en with nothing else = active: no class counts");
        cycles(5*4096);
        chk("disp",   d, 16'd5);
        chk("starve", s, 16'd5);
        chk("back",   b, 16'd3);

        $display("[4] ref is independent of the VLD classes");
        ref_stall = 1; picbuf_busy = 1; cycles(4*4096);
        ref_stall = 0; picbuf_busy = 0; cycles(1);
        chk("ref",  r, 16'd4);
        chk("disp", d, 16'd9);

        $display("[5] reset clears all four");
        rst = 0; cycles(2); rst = 1; cycles(1);
        chk("disp",   d, 16'd0);
        chk("starve", s, 16'd0);
        chk("back",   b, 16'd0);
        chk("ref",    r, 16'd0);
        chk("pic_n",  pn, 16'd0);
        chk("pic_over", po, 16'd0);
        chk("pic_max", pmax, 16'd0);

        $display("[6] the threshold is one frame period, exactly (29.97: 2,702,700 cycles)");
        picbuf_busy = 1; cycles(10);                 // park (closes the short stretch since reset)
        n0 = pn; o0 = po;
        frc = 4'd4;
        picture(2702700, 0);                         // exactly one period: not over
        chk("pic_n +1", pn - n0, 16'd1);
        chk("pic_over +0 at exactly 1 period", po - o0, 16'd0);
        picture(2702701, 0);                         // one cycle more: over
        chk("pic_n +2", pn - n0, 16'd2);
        chk("pic_over +1 at 1 period + 1", po - o0, 16'd1);

        $display("[7] starved cycles are not decode time");
        picture(2000000, 1000000);                   // 3.0 M elapsed, 2.0 M decode
        chk("pic_over unchanged (2.0 M decode)", po - o0, 16'd1);

        $display("[8] the threshold follows frame_rate_code (25 fps: 3,240,000)");
        frc = 4'd3;
        picture(3000000, 0);
        chk("pic_n +4", pn - n0, 16'd4);
        chk("pic_over unchanged at 25 fps", po - o0, 16'd1);

        $display("[9] pic_max is windowed");
        chk("pic_max before the window closes", pmax, 16'd0);
        while (dut.win != 0) @(negedge clk);         // the first window has closed
        cycles(2);
        chk("pic_max = the longest picture (3.0 M / 4096)", pmax, 16'd732);
        while (dut.win != 24'hFFFFFF) @(negedge clk);
        cycles(4);                                   // a window with no picture completed
        chk("pic_max after an empty window", pmax, 16'd0);

        if (errors == 0) $display("dec_duty_tb: ALL GREEN");
        else             $display("dec_duty_tb: FAILURES");
        if (errors != 0) $fatal(1, "%0d failure(s)", errors);
        $finish;
    end
endmodule
