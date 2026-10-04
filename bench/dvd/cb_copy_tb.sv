// bench/dvd/cb_copy_tb.sv -- the DTS codebooks: copied out of the three host FIFOs into DDR3,
// then fetched (dvd/dts/dts_cb_mem.sv; docs/dts_decoder.md D4 rule 3)
//
// The REAL hosts, with the core's init files: audio_ring (32 KB, the VQ codebook),
// lpcm_unpack and cb_host_ram (4096 x 32 each, the ADPCM codebook's halves; cb_host_ram
// is mp2_decode's old FIFO, kept when MP2 moved onto the engine), the copier,
// and a DDR3 model whose waitrequest and read latency are random (+busy=N %, +lat=N).
// Scores:
//   [copy]  after the copy, every one of the 8,192 rows in DDR3 equals the codebook
//           tables (CB/cb_adpcm.mem, CB/cb_vq.mem: tools/dts_golden.py --codebooks, the
//           engine's own row format), and tables_ok is high
//   [fetch] 2,000 random rows fetched through the engine's port equal the tables
//   [after] the copy ends with the hosts reset: every read pointer back at 0, so the
//           FIFOs start empty and aligned (the copy moved the pointers)
//   [once]  a reset after the copy, with the hosts then written as FIFOs, does not
//           copy again: DDR3 is unchanged (D4 rule 1, the flag with no reset term)
//   [hang]  the copy does not finish
// +expect_bad: the arm whose init is deliberately wrong; then tables_ok must be LOW.

`default_nettype none
`timescale 1ns/1ps

module cb_copy_tb;
    logic clk = 1'b0, rst_n = 1'b0;
    always #5 clk = ~clk;

    string cbdir, ring_init, lpcm_init, mp2_init;
    int busy_pct, lat, expect_bad;
    logic [63:0] t_adpcm [0:4095];
    logic [63:0] t_vq    [0:4095];
    initial begin
        if (!$value$plusargs("cb=%s", cbdir)) $fatal(1, "FAIL [setup] no +cb");
        if (!$value$plusargs("busy=%d", busy_pct)) busy_pct = 30;
        if (!$value$plusargs("lat=%d", lat)) lat = 6;
        expect_bad = $test$plusargs("expect_bad");
        $readmemh({cbdir, "/cb_adpcm.mem"}, t_adpcm);
        $readmemh({cbdir, "/cb_vq.mem"}, t_vq);
    end

    // ---- the hosts (their init files are the core's, unless a mutation swaps them)
    logic        busy, host_rst;
    logic        ring_step, lpcm_step, mp2_step, cp_mode;
    logic [7:0]  ring_q;
    logic [31:0] mp2_q;
    logic signed [15:0] lpcm_l, lpcm_r;
    logic        lpcm_wr;
    logic [7:0]  lpcm_wd;

    audio_ring #(.BYTE_DEPTH(32768), .FRAME_DEPTH(128),
                 .CB_INIT("dvd/dts/cb_host_ring.mem")) u_ring (
        .cp_step(ring_step),
        .clk, .rst_n(rst_n && !host_rst), .aud_byte(8'd0), .aud_valid(1'b0), .aud_type(2'd0),
        .aud_frame_start(1'b0), .aud_frame_pts(33'd0), .aud_frame_pts_valid(1'b0),
        .aud_frame_seamless(1'b0), .aud_ready(), .out_byte(ring_q), .out_valid(),
        .out_ready(1'b0), .frame_valid(), .frame_len(), .frame_type(), .frame_pts(),
        .frame_pts_valid(), .frame_seamless(), .frame_pop(1'b0), .frames_available(),
        .bytes_available(), .overflow_count(), .almost_full(), .drop_pulse(1'b0));

    lpcm_unpack #(.FIFO_AW(12), .CB_INIT("dvd/dts/cb_host_lpcm.mem")) u_lpcm (
        .clk, .rst(!rst_n || host_rst), .quant(2'd0), .le(1'b0), .wr_en(lpcm_wr), .wr_data(lpcm_wd),
        .full(), .afull(), .aud_ce(1'b0), .audio_l(lpcm_l), .audio_r(lpcm_r), .aud_valid(),
        .cp_mode, .cp_step(lpcm_step));

    cb_host_ram #(.AW(12), .CB_INIT("dvd/dts/cb_host_mp2.mem")) u_mp2 (
        .clk, .rst(!rst_n || host_rst), .cp_step(mp2_step), .cp_q(mp2_q));

    // ---- the copier
    logic        tables_ok, cb_req, cb_sel, cb_valid;
    logic [11:0] cb_addr;
    logic [63:0] cb_data;
    logic [31:0] sum_seen;
    logic [28:0] d_addr;
    logic [7:0]  d_bc, d_be;
    logic        d_rd, d_wr, d_busy, d_rv;
    logic [63:0] d_wd, d_rdata;
    dts_cb_mem u_cb (
        .clk, .rst_n, .hosts_ready(rst_n),
        .cp_lpcm_step(lpcm_step), .cp_lpcm_q({lpcm_l, lpcm_r}),
        .cp_mp2_step(mp2_step), .cp_mp2_q(mp2_q),
        .cp_ring_step(ring_step), .cp_ring_q(ring_q),
        .busy, .host_rst, .tables_ok, .sum_seen,
        .cb_req, .cb_sel, .cb_addr, .cb_valid, .cb_data,
        .ddr_addr(d_addr), .ddr_burstcnt(d_bc), .ddr_read(d_rd), .ddr_write(d_wr),
        .ddr_wdata(d_wd), .ddr_be(d_be), .ddr_busy(d_busy), .ddr_rdata(d_rdata),
        .ddr_rvalid(d_rv));
    assign cp_mode = busy;

    // ---- DDR3: the codebook region, random waitrequest, random read latency
    localparam [28:0] BASE = 29'h6100000;
    logic [63:0] ddr [0:8191];
    logic [7:0]  ddr_wr_hits [0:8191];
    int rd_q_t [$]; logic [63:0] rd_q_d [$];
    int now = 0, writes = 0, stray = 0;
    always @(posedge clk) begin
        now++;
        d_busy <= ($urandom % 100) < busy_pct;
        d_rv <= 1'b0;
        if (d_wr && !d_busy) begin
            if (d_addr < BASE || d_addr >= BASE + 8192 || d_bc != 8'd1 || d_be != 8'hFF) stray++;
            else begin ddr[d_addr - BASE] = d_wd; ddr_wr_hits[d_addr - BASE]++; end
            writes++;
        end
        if (d_rd && !d_busy) begin
            rd_q_t.push_back(now + 1 + ($urandom % (lat + 1)));
            rd_q_d.push_back((d_addr >= BASE && d_addr < BASE + 8192) ? ddr[d_addr - BASE] : 64'hDEAD);
        end
        if (rd_q_t.size() > 0 && rd_q_t[0] <= now) begin
            d_rv <= 1'b1; d_rdata <= rd_q_d[0];
            void'(rd_q_t.pop_front()); void'(rd_q_d.pop_front());
        end
    end

    // ---- the score
    logic [63:0] snap [0:8191];
    initial begin
        int t, bad, k, sel, row;
        logic [63:0] exp_row;
        cb_req = 1'b0; cb_sel = 1'b0; cb_addr = 12'd0; lpcm_wr = 1'b0; lpcm_wd = 8'd0;
        for (int i = 0; i < 8192; i++) begin ddr[i] = 64'd0; ddr_wr_hits[i] = 8'd0; end
        repeat (8) @(posedge clk);
        rst_n = 1'b1;
        t = 0;
        while (busy && t < 2000000) begin @(posedge clk); t++; end
        if (busy) $fatal(1, "FAIL [hang] the copy did not finish (%0d writes)", writes);
        repeat (4) @(posedge clk);
        if (expect_bad) begin
            if (tables_ok) $fatal(1, "FAIL [copy] a wrong host image was accepted (tables_ok high, sum %08x)", sum_seen);
            $display("cb_copy_tb: a wrong host image is refused (sum %08x)", sum_seen);
            $display("PASS: cb_copy_tb");
            $finish;
        end
        bad = 0;
        for (int i = 0; i < 4096; i++) begin
            if (ddr[i] !== t_adpcm[i]) begin
                if (bad < 3) $display("  ADPCM row %0d: ddr %016x, table %016x", i, ddr[i], t_adpcm[i]);
                bad++;
            end
            if (ddr[4096 + i] !== t_vq[i]) begin
                if (bad < 3) $display("  VQ row %0d: ddr %016x, table %016x", i, ddr[4096 + i], t_vq[i]);
                bad++;
            end
        end
        for (int i = 0; i < 8192; i++) if (ddr_wr_hits[i] != 8'd1) bad++;
        if (bad || stray || writes != 8192)
            $fatal(1, "FAIL [copy] %0d rows differ or were not written exactly once; %0d writes, %0d outside the region",
                   bad, writes, stray);
        if (!tables_ok) $fatal(1, "FAIL [copy] the rows are right but tables_ok is low (sum %08x)", sum_seen);
        if (u_ring.rd_ptr !== '0 || u_lpcm.rptr !== '0 || u_mp2.rp !== '0)
            $fatal(1, "FAIL [after] the hosts' read pointers after the copy: ring %0d lpcm %0d mp2 %0d",
                   u_ring.rd_ptr, u_lpcm.rptr, u_mp2.rp);
        $display("cb_copy_tb: 8192 rows copied in %0d cycles, checksum %08x, tables_ok", t, sum_seen);

        // [fetch]
        for (k = 0; k < 2000; k++) begin
            sel = $urandom % 2; row = $urandom % 4096;
            @(posedge clk); cb_req <= 1'b1; cb_sel <= sel; cb_addr <= row;
            @(posedge clk); cb_req <= 1'b0;
            t = 0;
            while (!cb_valid && t < 1000) begin @(posedge clk); t++; end
            exp_row = sel ? t_vq[row] : t_adpcm[row];
            if (!cb_valid || cb_data !== exp_row)
                $fatal(1, "FAIL [fetch] request %0d (sel %0d row %0d): %016x, the table %016x", k, sel, row, cb_data, exp_row);
            repeat ($urandom % 3) @(posedge clk);
        end
        $display("cb_copy_tb: 2000 random rows fetched, all equal");

        // [once]: the hosts are now FIFOs; a reset must not copy again
        for (int i = 0; i < 8192; i++) snap[i] = ddr[i];
        rst_n = 1'b0; repeat (6) @(posedge clk); rst_n = 1'b1;
        for (int i = 0; i < 400; i++) begin
            @(posedge clk); lpcm_wr <= 1'b1; lpcm_wd <= $urandom;
        end
        lpcm_wr <= 1'b0;
        repeat (20000) @(posedge clk);
        for (int i = 0; i < 8192; i++)
            if (ddr[i] !== snap[i]) $fatal(1, "FAIL [once] row %0d changed after a reset: the copy ran again", i);
        if (busy || !tables_ok) $fatal(1, "FAIL [once] a reset re-armed the copy (busy %0d tables_ok %0d)", busy, tables_ok);
        $display("cb_copy_tb: a reset after the copy does not copy again");
        $display("PASS: cb_copy_tb");
        $finish;
    end
endmodule
