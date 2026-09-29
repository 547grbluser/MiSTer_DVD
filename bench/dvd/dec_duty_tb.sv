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
//   [5] reset clears all four
// Scored with !== so an X/Z output cannot pass.
//============================================================================
module dec_duty_tb;
    reg clk = 0;
    always #5 clk = ~clk;
    reg rst = 0;
    reg picbuf_busy = 0, getbits_valid = 1, vld_en = 1, ref_stall = 0;
    wire [15:0] d, s, b, r;
    integer errors = 0;

    dec_duty dut (.clk(clk), .rst(rst), .picbuf_busy(picbuf_busy),
                  .getbits_valid(getbits_valid), .vld_en(vld_en), .ref_stall(ref_stall),
                  .disp_cnt(d), .starve_cnt(s), .back_cnt(b), .ref_cnt(r));

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

        if (errors == 0) $display("dec_duty_tb: ALL GREEN");
        else             $display("dec_duty_tb: FAILURES");
        if (errors != 0) $fatal(1, "%0d failure(s)", errors);
        $finish;
    end
endmodule
