// dvd/dts/dts_cb_mem.sv -- the DTS codebooks: copied once into DDR3, then served to the engine
//
// docs/dts_decoder.md D4 (P2). The two codebooks (ADPCM predictor vectors, 4096 x 64-bit
// rows; high-frequency VQ, 4096 x 64-bit rows {index, ssf}: tools/dts_golden.py
// write_codebooks' row format) ship in the bitstream as the POWER-UP CONTENTS of three
// FIFOs whose first read always follows a write, so their initial contents are otherwise
// unused:
//   lpcm_unpack's pair FIFO   4096 x 32   ADPCM rows    0..2047 (low word first)
//   mp2_decode's PCM FIFO     4096 x 32   ADPCM rows 2048..4095
//   audio_ring's byte memory  32768 x 8   VQ rows 0..4095 (byte k of row r at 8r + k)
// After configuration this module reads each host through its OWN read path -- it steps
// the host's read pointer (cp_*_step) and takes the host's existing read output, so no
// host gains a read port (a second port would duplicate its M10K) -- and writes the rows
// to DDR3 over ram2, one 64-bit word each:
//   word CB_BASE + row          ADPCM row (cb_sel 0)
//   word CB_BASE + 4096 + row   VQ row    (cb_sel 1)
// The audio path is held (`busy`) until the copy is done. From then on the hosts are
// ordinary FIFOs and the engine's codebook port (cb_req / cb_sel / cb_addr -> cb_valid /
// cb_data) is answered from DDR3, a one-word read each.
//
// RULES (D4): the copy runs ONCE PER CONFIGURATION, never per reset -- `copied` has no
// reset term (power-up 0), because after the first audio the hosts hold PCM and a second
// copy would write garbage over the tables. It is never silent: a Fletcher-style checksum
// of every row copied is compared with the tables' own (CB_SUM, generated with them);
// `tables_ok` is low until the copy is done and stays low if it mismatched, and the
// caller refuses DTS while it is low.
//
// The read latency of each host after a step: audio_ring's out_byte reads mem[rd_ptr]
// (0 cycles: the pointer IS the RAM's address register); lpcm_unpack and mp2_decode read
// into a register (1). The copier waits CP_WAIT cycles after every step, which covers
// both, so it does not depend on either.
//
// Quartus 17 (CLAUDE.md): no functions, no N'(expr) casts.

`default_nettype none

module dts_cb_mem #(
    parameter [28:0] CB_BASE = 29'h6100000,   // byte 0x30800000: the retired audio window
    parameter int    CP_WAIT = 3
) (
    input  wire          clk,
    input  wire          rst_n,                // the core's reset: NOT the copied flag's
    input  wire          hosts_ready,          // the hosts are out of reset (the copy
                                               // advances only then, and starts over if
                                               // they are reset mid-copy)

    // the hosts' read paths
    output logic         cp_lpcm_step,
    input  wire   [31:0] cp_lpcm_q,
    output logic         cp_mp2_step,
    input  wire   [31:0] cp_mp2_q,
    output logic         cp_ring_step,
    input  wire    [7:0] cp_ring_q,
    output logic         busy,                 // copying: hold the audio path
    output logic         tables_ok,            // copied, and the checksum matched
    output logic  [31:0] sum_seen,             // the checksum copied (telemetry)

    // the engine's codebook port
    input  wire          cb_req,
    input  wire          cb_sel,
    input  wire   [11:0] cb_addr,
    output logic         cb_valid,
    output logic  [63:0] cb_data,

    // DDR3 (ram2: 64-bit words)
    output logic  [28:0] ddr_addr,
    output logic   [7:0] ddr_burstcnt,
    output logic         ddr_read,
    output logic         ddr_write,
    output logic  [63:0] ddr_wdata,
    output logic   [7:0] ddr_be,
    input  wire          ddr_busy,
    input  wire   [63:0] ddr_rdata,
    input  wire          ddr_rvalid
);

`include "dvd/dts/dts_cb.svh"                  // CB_SUM (tools/dts_isa.py --asm)

    // the copied flag: NO reset term (power-up 0 by initialisation; confirm it in the
    // netlist, D4 rule 1)
    logic copied = 1'b0;

    typedef enum logic [2:0] {C_STEP, C_WAIT, C_TAKE, C_WRITE, C_DONE} cstate_t;
    cstate_t cst = C_STEP;
    logic [1:0]  phase = 2'd0;                // 0 lpcm, 1 mp2, 2 ring
    logic [11:0] row = 12'd0;
    logic [2:0]  part = 3'd0;                 // the word (lpcm/mp2: 0..1) or byte (ring: 0..7)
    logic [1:0]  wcnt = 2'd0;
    logic [63:0] acc = 64'd0;
    logic [31:0] s1 = 32'd0, s2 = 32'd0;
    logic        first = 1'b1;                // the host's first word needs no step

    assign busy = !copied;
    assign sum_seen = s2 ^ s1;

    // the fetch service (after the copy)
    logic        f_wait;
    logic        rd_pend;
    logic [28:0] f_addr;

    always_comb begin
        cp_lpcm_step = 1'b0; cp_mp2_step = 1'b0; cp_ring_step = 1'b0;
        if (!copied && cst == C_STEP && !first)
            case (phase)
                2'd0: cp_lpcm_step = 1'b1;
                2'd1: cp_mp2_step  = 1'b1;
                default: cp_ring_step = 1'b1;
            endcase
    end

    wire [63:0] row_w = acc;
    wire [11:0] row_idx = row;
    wire [28:0] w_addr = CB_BASE + ((phase == 2'd1) ? {17'd0, 1'b1, row_idx[10:0]} :
                                    (phase == 2'd2) ? {16'd0, 1'b1, row_idx} :
                                                      {17'd0, 1'b0, row_idx[10:0]});

    always_ff @(posedge clk) begin
        cb_valid <= 1'b0;
        if (!copied && !hosts_ready) begin        // (re)start from the first word
            cst <= C_STEP; phase <= 2'd0; row <= 12'd0; part <= 3'd0; first <= 1'b1;
            s1 <= 32'd0; s2 <= 32'd0; ddr_write <= 1'b0;
        end else if (!copied) begin
            case (cst)
                C_STEP: begin first <= 1'b0; wcnt <= 2'd0; cst <= C_WAIT; end
                C_WAIT: if (wcnt == CP_WAIT[1:0] - 2'd1) cst <= C_TAKE; else wcnt <= wcnt + 2'd1;
                C_TAKE: begin
                    if (phase == 2'd2) acc <= {cp_ring_q, acc[63:8]};        // byte k -> [8k +: 8]
                    else acc <= {(phase == 2'd0) ? cp_lpcm_q : cp_mp2_q, acc[63:32]};
                    if ((phase == 2'd2) ? (part == 3'd7) : (part == 3'd1)) begin
                        part <= 3'd0; cst <= C_WRITE;
                    end else begin part <= part + 3'd1; cst <= C_STEP; end
                end
                C_WRITE: if (!ddr_write || !ddr_busy) begin
                    if (!ddr_write) begin
                        ddr_write <= 1'b1; ddr_addr <= w_addr; ddr_wdata <= row_w;
                        ddr_burstcnt <= 8'd1; ddr_be <= 8'hFF;
                        s1 <= s1 + row_w[31:0] + row_w[63:32];
                        s2 <= s2 + s1 + row_w[31:0] + row_w[63:32];
                    end else begin                       // accepted
                        ddr_write <= 1'b0;
                        if ((phase == 2'd2) ? (row == 12'd4095) : (row == 12'd2047)) begin
                            row <= 12'd0; first <= 1'b1;
                            if (phase == 2'd2) cst <= C_DONE;
                            else begin phase <= phase + 2'd1; cst <= C_STEP; end
                        end else begin row <= row + 12'd1; cst <= C_STEP; end
                    end
                end
                C_DONE: begin copied <= 1'b1; tables_ok <= ((s2 ^ s1) == CB_SUM); end
                default: cst <= C_STEP;
            endcase
        end else begin
            // the fetch service: one request at a time (the engine waits for each row)
            if (ddr_read && !ddr_busy) ddr_read <= 1'b0;
            if (cb_req && !rd_pend) begin
                ddr_read <= 1'b1; ddr_burstcnt <= 8'd1; ddr_be <= 8'hFF;
                ddr_addr <= CB_BASE + {16'd0, cb_sel, cb_addr};
                rd_pend <= 1'b1;
            end
            if (rd_pend && ddr_rvalid) begin
                cb_data <= ddr_rdata; cb_valid <= 1'b1; rd_pend <= 1'b0;
            end
        end
        if (!rst_n) begin
            ddr_read <= 1'b0; rd_pend <= 1'b0;
            if (copied) ddr_write <= 1'b0;
        end
    end

    initial begin
        ddr_write = 1'b0; ddr_read = 1'b0; rd_pend = 1'b0; tables_ok = 1'b0;
        ddr_addr = 29'd0; ddr_burstcnt = 8'd1; ddr_be = 8'hFF; ddr_wdata = 64'd0;
    end

endmodule
