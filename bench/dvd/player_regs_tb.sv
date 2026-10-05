// bench/dvd/player_regs_tb.sv -- dvd/player_regs.sv: SPRM14 / SPRM15 / SPRM20
// (feature/player-regs; docs/dvd_vm.md "Player parameters SPRM14/15/20").
//
// The reference is written differently from the RTL on purpose (an explicit decision
// table for SPRM14, a counting scan for SPRM20, named bit constants for SPRM15) so a
// shared mistake cannot pass on both.
//
//   [P1] SPRM20: all 256 masks -- one-hot of the lowest allowed region; mask 0 reads
//        region 1 (today's value); all-prohibited falls back to region 1 AND flags it
//   [P2] SPRM14: every output path x Analog Aspect, against the decided table
//   [P3] SPRM15: AC-3 + MPEG always; DTS iff Passthru or the codebooks are loaded;
//        never SDDS or karaoke
//   [P4] the decided spot values, written out, so the table cannot drift unnoticed
`timescale 1ns/1ps
module player_regs_tb;
    reg  [7:0] rmask;
    reg        aa_live, pass_mode, dts_ok;
    reg  [1:0] aa_sel;
    wire [15:0] sprm14, sprm15, sprm20;
    wire        allp;

    player_regs dut (.rmask(rmask), .aa_live(aa_live), .aa_sel(aa_sel),
                     .pass_mode(pass_mode), .dts_ok(dts_ok),
                     .sprm14(sprm14), .sprm15(sprm15), .sprm20(sprm20),
                     .rmask_all_prohibited(allp));

    integer errors = 0, e1 = 0, e2 = 0, e3 = 0;

    localparam [15:0] B_AC3 = 16'h4000, B_MPEG = 16'h1000, B_DTS = 16'h0800;

    function automatic [15:0] ref20(input [7:0] m);
        integer r;
        begin
            ref20 = 16'h0001;
            r = 0;
            while (r < 8 && m[r]) r = r + 1;     // first region whose bit is CLEAR
            if (r < 8) ref20 = 16'd1 * (2 ** r);
        end
    endfunction

    function automatic [15:0] ref14(input a, input [1:0] s);
        begin
            if (!a)            ref14 = 16'h0C00;                  // HDMI: 16:9 TV, wide
            else if (s == 2'd1) ref14 = 16'h0C00;                 // Fit: 16:9 TV
            else if (s == 2'd3) ref14 = 16'h0100;                 // Crop: 4:3, pan&scan
            else               ref14 = 16'h0200;                  // Auto/Letterbox
        end
    endfunction

    initial begin
        for (int m = 0; m < 256; m++)
          for (int a = 0; a < 2; a++)
            for (int s = 0; s < 4; s++)
              for (int p = 0; p < 2; p++)
                for (int d = 0; d < 2; d++) begin
                    rmask = m[7:0]; aa_live = a[0]; aa_sel = s[1:0];
                    pass_mode = p[0]; dts_ok = d[0]; #1;
                    if (sprm20 !== ref20(m[7:0]) || allp !== (m == 255)) begin
                        e1++; if (e1 < 6) $display("  FAIL [P1] mask=%02x -> sprm20=%04x allp=%b (want %04x %b)",
                                                   m, sprm20, allp, ref20(m[7:0]), m == 255);
                    end
                    if (sprm14 !== ref14(a[0], s[1:0])) begin
                        e2++; if (e2 < 6) $display("  FAIL [P2] analog=%0d aa=%0d -> sprm14=%04x (want %04x)",
                                                   a, s, sprm14, ref14(a[0], s[1:0]));
                    end
                    if (sprm15 !== (B_AC3 | B_MPEG | ((p || d) ? B_DTS : 16'h0))) begin
                        e3++; if (e3 < 6) $display("  FAIL [P3] pass=%0d dts=%0d -> sprm15=%04x", p, d, sprm15);
                    end
                end
        errors = e1 + e2 + e3;
        if (e1 == 0) $display("  [P1] OK: SPRM20 over all 256 masks (lowest allowed region; all-prohibited flagged)");
        if (e2 == 0) $display("  [P2] OK: SPRM14 over every output path x Analog Aspect");
        if (e3 == 0) $display("  [P3] OK: SPRM15 AC-3 + MPEG, DTS iff Passthru or codebooks, no SDDS/karaoke");

        // [P4] the decided values, written out
        rmask = 8'h00; aa_live = 0; aa_sel = 0; pass_mode = 0; dts_ok = 1; #1;
        if (sprm20 !== 16'h0001 || sprm14 !== 16'h0C00 || sprm15 !== 16'h5800) begin
            errors++; $display("  FAIL [P4] HDMI default: %04x %04x %04x (want 0001 0C00 5800)", sprm20, sprm14, sprm15);
        end
        rmask = 8'hFD; #1;                                     // region 2 only
        if (sprm20 !== 16'h0002) begin errors++; $display("  FAIL [P4] region-2-only mask -> %04x", sprm20); end
        rmask = 8'hF5; #1;                                     // region 1 prohibited, 2 allowed (and 4)
        if (sprm20 !== 16'h0002) begin errors++; $display("  FAIL [P4] mask F5 -> %04x (want 0002)", sprm20); end
        rmask = 8'h7E; #1;                                     // regions 1 and 8 allowed -> the lowest
        if (sprm20 !== 16'h0001) begin errors++; $display("  FAIL [P4] mask 7E -> %04x (want 0001)", sprm20); end
        aa_live = 1; aa_sel = 2; dts_ok = 0; #1;
        if (sprm14 !== 16'h0200 || sprm15 !== 16'h5000) begin
            errors++; $display("  FAIL [P4] analog letterbox, no DTS tables: %04x %04x (want 0200 5000)", sprm14, sprm15);
        end
        if (errors == 0) $display("  [P4] OK: decided spot values");

        if (errors == 0) $display("RESULT: PASS (player_regs)");
        else $fatal(1, "RESULT: FAIL (%0d errors)", errors);
        $finish;
    end
endmodule
