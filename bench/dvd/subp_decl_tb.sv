// bench/dvd/subp_decl_tb.sv -- dvd/subp_decl.sv: the declared-stream bookkeeping
// behind the Subtitle button's 32-stream cycle (feature/subp-32).
//
// The reference is written differently from the RTL on purpose (a scan with early
// exit instead of last-write-wins loops, and an explicit index compare instead of
// a shifted mask), so a shared mistake cannot pass on both.
//
//   [D1] 3000 random tables x random cur: any / first / last / next / next_ok
//   [D2] edges: empty table, only stream 0, only stream 31, all 32, cur = 31
//   [D3] the table is WRITTEN through the bus: words 32..39 (audio_control) never
//        touch it, and bit 31 of each subp word lands at its own index
//   [D4] the Subtitle-button walk from OFF visits exactly the declared streams in
//        order, then returns to OFF (the cycle the user sees)
`timescale 1ns/1ps
module subp_decl_tb;
    reg        clk = 0;
    always #5 clk = ~clk;

    reg        ctl_we = 0;
    reg  [5:0] ctl_waddr = 0;
    reg        ctl_wbit31 = 0;
    reg  [4:0] cur = 0;
    wire [31:0] declared;
    wire        any, next_ok;
    wire [4:0]  first, last, next;

    subp_decl dut (.clk(clk), .ctl_we(ctl_we), .ctl_waddr(ctl_waddr),
                   .ctl_wbit31(ctl_wbit31), .cur(cur), .declared(declared),
                   .any(any), .first(first), .last(last),
                   .next_ok(next_ok), .next(next));

    integer errors = 0;

    task automatic load(input [31:0] tbl);
        for (int k = 0; k < 32; k++) begin
            @(negedge clk); ctl_we = 1; ctl_waddr = k[5:0]; ctl_wbit31 = tbl[k];
        end
        @(negedge clk); ctl_we = 0;
        @(posedge clk); #1;
    endtask

    task automatic check(input string arm, input [31:0] tbl, input [4:0] c);
        int  rf, rl, rn; bit rany, rnok;
        cur = c; #1;
        rany = (tbl != 0);
        rf = 0; for (int i = 0; i < 32; i++) if (tbl[i]) begin rf = i; break; end
        rl = 0; for (int i = 31; i >= 0; i--) if (tbl[i]) begin rl = i; break; end
        rnok = 0; rn = 0;
        for (int i = 0; i < 32; i++) if (tbl[i] && i > c) begin rnok = 1; rn = i; break; end
        if (declared !== tbl || any !== rany || first !== rf[4:0] || last !== rl[4:0] ||
            next_ok !== rnok || (rnok && next !== rn[4:0])) begin
            errors++;
            if (errors < 12)
                $display("  FAIL [%s] tbl=%08x cur=%0d: decl=%08x any=%b first=%0d last=%0d next_ok=%b next=%0d (want %b %0d %0d %b %0d)",
                         arm, tbl, c, declared, any, first, last, next_ok, next,
                         rany, rf, rl, rnok, rn);
        end
    endtask

    initial begin
        int seed;
        bit [31:0] t;
        seed = 32'h5B32;
        repeat (3) @(posedge clk);

        // [D2] edges
        load(32'h0000_0000); for (int c = 0; c < 32; c++) check("D2 empty", 32'h0, c[4:0]);
        load(32'h0000_0001); for (int c = 0; c < 32; c++) check("D2 only0", 32'h1, c[4:0]);
        load(32'h8000_0000); for (int c = 0; c < 32; c++) check("D2 only31", 32'h8000_0000, c[4:0]);
        load(32'hFFFF_FFFF); for (int c = 0; c < 32; c++) check("D2 all32", 32'hFFFF_FFFF, c[4:0]);
        if (errors == 0) $display("  [D2] OK: empty / only 0 / only 31 / all 32, every cur");

        // [D1] random tables (biased toward sparse, like real discs)
        for (int n = 0; n < 3000; n++) begin
            t = $random(seed);
            if (n % 3 == 0) t = t & $random(seed) & $random(seed);   // sparse
            if (n % 3 == 1) t = t & 32'h0000_0FFF;                   // the common 1..12
            load(t);
            check("D1", t, $random(seed));
        end
        if (errors == 0) $display("  [D1] OK: 3000 random tables");

        // [D3] audio words (32..39) never touch the table
        load(32'h0000_0A05);
        for (int k = 32; k < 40; k++) begin
            @(negedge clk); ctl_we = 1; ctl_waddr = k[5:0]; ctl_wbit31 = 1;
        end
        @(negedge clk); ctl_we = 0; @(posedge clk); #1;
        if (declared !== 32'h0000_0A05) begin
            errors++; $display("  FAIL [D3] an audio_control write changed the table: %08x", declared);
        end else $display("  [D3] OK: audio_control writes leave the subtitle table alone");

        // [D4] the user's walk: OFF -> first -> next ... -> OFF visits declared only
        begin
            bit [31:0] tbl;
            int  seen[$];
            int  c, steps;
            tbl = 32'h0010_0A06;                  // streams 1, 2, 9, 11, 20
            load(tbl);
            c = first; seen.push_back(c); steps = 0;
            cur = c[4:0]; #1;
            while (next_ok && steps < 40) begin c = next; seen.push_back(c); cur = c[4:0]; #1; steps++; end
            if (seen.size() != 5 || seen[0] != 1 || seen[1] != 2 || seen[2] != 9 ||
                seen[3] != 11 || seen[4] != 20) begin
                errors++; $display("  FAIL [D4] walk visited %0d streams (first %0d), want 1 2 9 11 20", seen.size(), seen[0]);
            end else $display("  [D4] OK: the Subtitle walk visits 1 2 9 11 20, then OFF");
        end

        if (errors == 0) $display("RESULT: PASS (subp_decl)");
        else $fatal(1, "RESULT: FAIL (%0d errors)", errors);
        $finish;
    end
endmodule
