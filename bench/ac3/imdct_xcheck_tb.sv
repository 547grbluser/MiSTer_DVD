// imdct_xcheck_tb.sv -- imdct_512 against tools/imdct_model.py, bit for bit.
//
// Runs NBLK consecutive blocks through ONE imdct_512 (so the overlap delay line and
// first_blk carry across blocks, as in the core) and compares all six pcm_mem slots
// after every `done` with the model's expect.mem: ===, every word, no tolerance.
// Vectors: tools/imdct_model.py vec (params.mem, coeff.mem, expect.mem).
// Runner: bench/ac3/run_imdct_xcheck.sh.
`timescale 1ns/1ps
module imdct_xcheck_tb;
    parameter integer NBLK = 12;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst, start, done;

    logic [23:0] params [0:NBLK-1];
    logic [23:0] coeff  [0:NBLK*2048-1];
    logic [31:0] exp_pcm [0:NBLK*1536-1];

    integer blk;
    logic [10:0] coeff_rd_addr;
    logic signed [23:0] coeff_rd_data;
    always_ff @(posedge clk) coeff_rd_data <= coeff[blk * 2048 + coeff_rd_addr];

    logic [10:0] pcm_rd_addr;
    logic signed [31:0] pcm_rd_data;
    wire [23:0] pw = params[blk];

    imdct_512 dut (
        .clk(clk), .rst(rst), .start(start),
        .nfchans(pw[22:20]), .blksw(pw[19:15]), .dynrng(pw[14:7]), .acmod(pw[6:4]),
        .cmixlev(pw[3:2]), .surmixlev(pw[1:0]),
        .coeff_rd_addr(coeff_rd_addr), .coeff_rd_data(coeff_rd_data),
        .pcm_rd_addr(pcm_rd_addr), .pcm_rd_data(pcm_rd_data),
        .lvl_q(), .done(done)
    );

    integer k, bad, words, live, t;
    initial begin
        $readmemh("params.mem", params);
        $readmemh("coeff.mem", coeff);
        $readmemh("expect.mem", exp_pcm);
        rst = 1; start = 0; pcm_rd_addr = 0; blk = 0; bad = 0; words = 0; live = 0;
        // Cyclone V M10K powers up zero. Without this a 3/0 fold (slev = 0) reads an
        // unwritten surround slot and x * 0 = x in simulation, never on the chip.
        for (k = 0; k < 1536; k = k + 1) dut.pcm_mem[k] = 32'sd0;
        for (k = 0; k < 512; k = k + 1) dut.delay_mem[k] = 64'd0;
        repeat (4) @(posedge clk);
        rst = 0;
        for (blk = 0; blk < NBLK; blk = blk + 1) begin
            @(posedge clk); start = 1; @(posedge clk); start = 0;
            t = 0;
            while (!done && t < 400000) begin @(posedge clk); t = t + 1; end
            if (!done) $fatal(1, "FAIL: block %0d never finished", blk);
            for (k = 0; k < 1536; k = k + 1) begin
                pcm_rd_addr = {k[10:8], k[7:0]};
                #1;
                words = words + 1;
                if (exp_pcm[blk * 1536 + k] != 32'd0) live = live + 1;
                if (pcm_rd_data !== exp_pcm[blk * 1536 + k]) begin
                    bad = bad + 1;
                    if (bad <= 8)
                        $display("  block %0d slot %0d bin %0d: rtl %08h model %08h", blk,
                                 k / 256, k % 256, pcm_rd_data, exp_pcm[blk * 1536 + k]);
                end
            end
        end
        if (bad) $fatal(1, "FAIL: %0d of %0d words differ from the model", bad, words);
        // a silent window compares zeros with zeros and proves nothing
        if (live < 256) $fatal(1, "FAIL: only %0d nonzero words: a vacuous window", live);
        $display("PASS: imdct_512 == tools/imdct_model.py on %0d blocks (%0d words, %0d nonzero)", NBLK, words, live);
        $finish;
    end
endmodule
