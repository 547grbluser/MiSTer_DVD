// dac_dither_tb.sv — the analog DAC's ordered dither (dvd/dac_dither.sv,
// docs/single_raster_analog.md §8). Run through bench/dvd/run_dac_dither.sh (--red).
//
// A small progressive raster (40 clocks x 14 lines, 16 x 8 active) runs through the module
// one field per level. Every claim is scored against the DAC's CONTRACT (what the 6-bit
// pins show), not against the module's own threshold, which the bench never reads:
//
//   [T1] average   v <= 252: the 6-bit codes over a field average to v / 4 EXACTLY
//                  (4 * sum(out >> 2) == N * v), and each output is v + 0..3
//   [T2] multiple  v % 4 == 0: every output's 6-bit code is v / 4 (black, YPbPr's 128)
//   [T3] saturate  v > 252: every output is in [v, 255] -- no wrap to black
//   [T4] blanking  outside DE the word passes bit-exact (the line-21 caption level and the
//                  burst live there), with a non-zero pattern on din
//   [T5] bypass    en = 0: every word, in DE or not, passes bit-exact
//   [T6] fields    the same position in two consecutive fields adds exactly 3 LSB in all:
//                  the pattern inverts, so a still picture does not stand as a crosshatch
//
// All comparisons use !== so an X on dout fails rather than passing vacuously.

`timescale 1ns/1ps
`default_nettype none

module dac_dither_tb;

    localparam integer HT = 40, VT = 14;          // clocks per line, lines per field
    localparam integer HA0 = 8,  HAW = 16;        // active columns [8, 24)
    localparam integer VA0 = 4,  VAL = 8;         // active lines [4, 12)
    localparam integer N   = HAW * VAL;           // samples per field (a multiple of 16)

    reg clk = 1'b0;
    always #5 clk = ~clk;

    integer hc = 0, vc = 0;
    wire hs = (hc >= 30) && (hc < 34);
    wire vs = (vc < 2);
    wire de = (hc >= HA0) && (hc < HA0 + HAW) && (vc >= VA0) && (vc < VA0 + VAL);

    reg        en = 1'b1;
    reg  [7:0] lr = 8'd0, lg = 8'd0, lb = 8'd0;   // this field's levels
    wire [23:0] din = de ? {lr, lg, lb} : {8'h5A, 8'hA5, lr ^ 8'h3C};
    wire [23:0] dout;

    dac_dither dut (.clk(clk), .en(en), .hs(hs), .vs(vs), .de(de), .din(din), .dout(dout));

    // ------------------------------------------------------------------ scoring
    integer f1 = 0, f2 = 0, f3 = 0, f4 = 0, f5 = 0, f6 = 0;
    integer sum_r = 0, sum_g = 0, sum_b = 0;
    reg [1:0] prev_d [0:2][0:VAL-1][0:HAW-1];     // field n's offset, per channel and position
    reg       prev_ok = 1'b0;                     // prev_d holds the previous field, same levels
    reg       scoring = 1'b0;                     // en settled for this field

    task automatic chan(input integer ch, input [7:0] v, input [7:0] o, input integer y,
                        input integer x);
        integer d;
        begin
            d = o - v;
            if (v <= 8'd252) begin
                if ((^o === 1'bx) || d < 0 || d > 3) begin
                    if (f1 < 4) $display("FAIL [T1] ch%0d v=%0d out=%0d at (%0d,%0d): not v + 0..3", ch, v, o, y, x);
                    f1 = f1 + 1;
                end
                if (v[1:0] == 2'd0 && (o[7:2] !== v[7:2])) begin
                    if (f2 < 4) $display("FAIL [T2] ch%0d v=%0d out=%0d: a multiple of 4 changed at 6 bits", ch, v, o);
                    f2 = f2 + 1;
                end
                if (prev_ok && ({30'd0, prev_d[ch][y][x]} + d !== 3)) begin
                    if (f6 < 4) $display("FAIL [T6] ch%0d v=%0d at (%0d,%0d): offsets %0d then %0d, not complementary", ch, v, y, x, prev_d[ch][y][x], d);
                    f6 = f6 + 1;
                end
                prev_d[ch][y][x] = d[1:0];
            end else begin
                if ((^o === 1'bx) || o < v) begin
                    if (f3 < 4) $display("FAIL [T3] ch%0d v=%0d out=%0d: wrapped", ch, v, o);
                    f3 = f3 + 1;
                end
            end
        end
    endtask

    always @(negedge clk) if (scoring) begin
        if (!en) begin
            if (dout !== din) begin
                if (f5 < 4) $display("FAIL [T5] bypass: din=%h dout=%h (de=%0d)", din, dout, de);
                f5 = f5 + 1;
            end
        end else if (!de) begin
            if (dout !== din) begin
                if (f4 < 4) $display("FAIL [T4] blanking: din=%h dout=%h at line %0d clock %0d", din, dout, vc, hc);
                f4 = f4 + 1;
            end
        end else begin
            chan(0, lr, dout[23:16], vc - VA0, hc - HA0);
            chan(1, lg, dout[15:8],  vc - VA0, hc - HA0);
            chan(2, lb, dout[7:0],   vc - VA0, hc - HA0);
            sum_r = sum_r + dout[23:18];
            sum_g = sum_g + dout[15:10];
            sum_b = sum_b + dout[7:2];
        end
    end

    task automatic avg_check(input integer ch, input [7:0] v, input integer s);
        if (v <= 8'd252 && 4 * s != N * v) begin
            if (f1 < 8) $display("FAIL [T1] ch%0d v=%0d: field mean of the 6-bit code %0d/%0d, want %0d/4", ch, v, s, N, v);
            f1 = f1 + 1;
        end
    endtask

    // ------------------------------------------------------------------ raster
    // One field = VT lines. `field` runs a whole field at the given levels; the scorer
    // sees it only when `score` is set (the field after an en change is the settle field).
    task automatic field(input [7:0] r, input [7:0] g, input [7:0] b, input score, input pair);
        begin
            lr = r; lg = g; lb = b;
            sum_r = 0; sum_g = 0; sum_b = 0;
            if (!pair) prev_ok = 1'b0;
            scoring = score;
            repeat (HT * VT) begin
                @(posedge clk);
                if (hc == HT - 1) begin hc <= 0; vc <= (vc == VT - 1) ? 0 : vc + 1; end
                else hc <= hc + 1;
            end
            @(negedge clk);
            if (score && en) begin
                avg_check(0, r, sum_r); avg_check(1, g, sum_g); avg_check(2, b, sum_b);
                prev_ok = 1'b1;
            end
            scoring = 1'b0;
        end
    endtask

    integer v;
    initial begin
        // the raster's first field primes vs/hs edges and the two en flops
        field(8'd0, 8'd0, 8'd0, 1'b0, 1'b0);

        // T1/T2/T3/T6: every level, in two consecutive fields at the same levels
        for (v = 0; v < 256; v = v + 1) begin
            field(v[7:0], v[7:0] + 8'd85, 8'd255 - v[7:0], 1'b1, 1'b0);
            field(v[7:0], v[7:0] + 8'd85, 8'd255 - v[7:0], 1'b1, 1'b1);
        end

        // T5: bypass, after one settle field
        en = 1'b0;
        field(8'd17, 8'd18, 8'd19, 1'b0, 1'b0);
        for (v = 0; v < 256; v = v + 37)
            field(v[7:0], 8'd255 - v[7:0], v[7:0] ^ 8'h55, 1'b1, 1'b0);

        $display("dac_dither_tb: T1 %0d  T2 %0d  T3 %0d  T4 %0d  T5 %0d  T6 %0d failure(s)",
                 f1, f2, f3, f4, f5, f6);
        if (f1 + f2 + f3 + f4 + f5 + f6 != 0) $fatal(1, "FAIL dac_dither_tb");
        $display("PASS dac_dither_tb");
        $finish;
    end

endmodule

`default_nettype wire
