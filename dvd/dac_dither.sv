/*
 * dac_dither.sv — DVD-FORK (ordered dither ahead of the analog DAC, 2026-10-06)
 *
 * WHY THIS EXISTS
 * ---------------
 * The DE10-Nano's classic analog I/O board drives its VGA DAC from the top SIX bits of
 * each 8-bit channel (sys/sys_top.v: `VGA_R = vga_o[23:18]`, the low two bits go only to
 * the SDIO pins of boards that have an 8-bit DAC). 64 steps from black to white is too
 * few for film: dark gradients contour into visible bands on a CRT. A Y 16 -> 64 ramp is
 * RGB 0 -> 56 after the BT.601 matrix, i.e. 14 steps of 4 at the DAC.
 *
 * This adds a 4 x 4 ordered (Bayer) threshold t of 0..3 LSB to every channel before the
 * truncation. Over the pattern each t occurs four times, and by Hermite's identity
 *
 *     sum_{t=0..3} floor((v + t) / 4) = v
 *
 * so the DAC's output averages to the 8-bit value exactly (v <= 252; above that the sum
 * saturates at 255). Every other field (frame, on Progressive) the matrix is inverted
 * (15 - b), so a still picture's pattern also cancels over two fields instead of standing
 * as a fixed crosshatch on flat areas.
 *
 *   the place    the core-raster VGA path's last 24-bit word (vga_o, after vga_out and the
 *                yc_out mux: RGB, YPbPr or S-Video / composite alike). HDMI never sees it,
 *                and nor does the scaler-to-VGA path (vgas_o, vga_scaler=1).
 *   the pattern  keyed on CLOCKS (clk_vid = CLK_VIDEO = clk_sys, 27 MHz), not pixels:
 *                column = clock in the line mod 4, row = line mod 4. On Progressive (480p,
 *                a pixel per clock) that is a classic per-pixel ordered dither; on 480i /
 *                240p (a pixel held two clocks) each pixel gets two thresholds and the
 *                pattern repeats every two pixels. Its fundamental is 6.75 MHz either way,
 *                above NTSC/PAL luma bandwidth on S-Video and composite.
 *   only DE      blanking, sync levels and the colour burst are untouched — and so is the
 *                line-21 caption waveform, which the core writes OUTSIDE DE
 *                (sys/sys_top.v, the scanlines instance's de_emu note). A multiple of 4 is
 *                unchanged at 6 bits whatever the threshold (black 0, YPbPr's Pb/Pr 128).
 *   8-bit DACs   (boards taking the low bits on SDIO) see v + 0..3: a 1.5 LSB mean in 256,
 *                invisible — but there is nothing to fix there either, so the OSD option
 *                Analog Dither (emu's VGA_DITHER, status[8]) turns it on, Off by default.
 *   bypass       en = 0 -> dout = din bit-exact.
 *
 * `en` comes from emu's status register, which is clocked by clk_sys — the SAME 27 MHz net
 * as clk_vid in this core (CLK_VIDEO = clk_sys), so there is no clock crossing and no SDC
 * entry. The two flops are kept anyway so a future CLK_VIDEO change does not silently
 * create an unsynchronised path.
 *
 * Docs: docs/single_raster_analog.md §8. Bench: bench/dvd/run_dac_dither.sh (--red).
 * No `function`, no N'(expr) cast (Quartus 17).
 */
`default_nettype none

module dac_dither (
    input  wire        clk,
    input  wire        en,      // level, 0 = bypass (bit-exact)
    input  wire        hs,      // the word's own syncs and DE (vga_out / yc_out outputs)
    input  wire        vs,
    input  wire        de,
    input  wire [23:0] din,
    output reg  [23:0] dout
);

    // No reset: sys_top's VGA path has none. Power-up values, like the framework's own.
    reg [1:0] col = 2'd0, row = 2'd0;
    reg       fld = 1'b0, hs_q = 1'b0, vs_q = 1'b0;
    reg       en_s1 = 1'b0, en_s2 = 1'b0;
    always @(posedge clk) begin
        en_s1 <= en;
        en_s2 <= en_s1;
        hs_q  <= hs;
        vs_q  <= vs;
        col   <= de ? col + 2'd1 : 2'd0;
        if (hs && !hs_q) row <= row + 2'd1;
        if (vs && !vs_q) begin
            row <= 2'd0;
            fld <= ~fld;
        end
    end

    // 4 x 4 Bayer matrix (0..15); the threshold is its top two bits.
    reg [3:0] b;
    always @(*)
        case ({row, col})
            4'h0: b = 4'd0;  4'h1: b = 4'd8;  4'h2: b = 4'd2;  4'h3: b = 4'd10;
            4'h4: b = 4'd12; 4'h5: b = 4'd4;  4'h6: b = 4'd14; 4'h7: b = 4'd6;
            4'h8: b = 4'd3;  4'h9: b = 4'd11; 4'hA: b = 4'd1;  4'hB: b = 4'd9;
            4'hC: b = 4'd15; 4'hD: b = 4'd7;  4'hE: b = 4'd13; default: b = 4'd5;
        endcase
    wire [3:0] bf = fld ? (4'd15 - b) : b;
    wire [1:0] t  = bf[3:2];

    wire [8:0] s2 = {1'b0, din[23:16]} + {7'd0, t};
    wire [8:0] s1 = {1'b0, din[15:8]}  + {7'd0, t};
    wire [8:0] s0 = {1'b0, din[7:0]}   + {7'd0, t};

    always @(*)
        if (!en_s2 || !de) dout = din;
        else dout = {s2[8] ? 8'hFF : s2[7:0],
                     s1[8] ? 8'hFF : s1[7:0],
                     s0[8] ? 8'hFF : s0[7:0]};

endmodule

`default_nettype wire
