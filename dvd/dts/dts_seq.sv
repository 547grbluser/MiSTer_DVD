// dvd/dts/dts_seq.sv -- the DTS core decoder's microcoded sequencer
//
// Runs dvd/dts/dts.uasm (assembled into dts_ucode.mem by tools/dts_isa.py --asm): the
// frame header, the coding header, every subframe's side information and the
// per-subsubframe control of the vector engine (dts_vec.sv). The instruction set, the
// memory map and every instruction's effect are defined by tools/dts_isa.py's
// emulator; bench/dvd/run_dts_seq.sh scores this module's trace (every register write,
// store and XQ code, in program order) against it event by event.
// docs/dts_decoder.md sec 10.
//
// Timing: one instruction a cycle for ALU operations, stores, branches, calls and
// returns (the next instruction's address goes to the program ROM while the current
// one executes); a load takes two; GET n takes n + 1 (the reader is bit-serial: one bit
// a cycle, no barrel shifter); a VLC two plus one a code bit; a vector op waits for
// its engine.
//
// Input: a frame is a descriptor (fr_len, its byte length, taken by the FRAME
// instruction) and then exactly that many bytes. Bits past fr_len read 0 and pulse
// overrun_bit; they never come from the next frame. FEND, and ERR, drain the frame's
// untaken bytes; ERR then restarts the program at FRAME (the emulator's `err`). A
// taken branch to an error vector (pc[10:5] == UC_ERRV, the ROM's top 32 words) is ERR
// with code pc[4:0]: the program carries no one-word stub per error code.
//
// TWO PROGRAMS, ONE ROM (docs/ac3_engine.md A2): DTS's at 0 and AC-3's after it
// (engine_ucode.mem, 2K deep). `codec` picks the entry point at reset and the
// restart point after a refusal; it must only change while the engine is reset.
//
// The Huffman unit walks a binary tree, one code bit a cycle (53 of the 62 core books
// are not canonical, docs/dts_decoder.md D2): a node is {right, left}, an entry
// {leaf, value[7:0]}; a leaf's value is the signed symbol, else the child's offset
// forward from this node (18-bit nodes, in two ROMs of 2,048 and 1,024 words: 6 M10K
// and a 2:1 mux; tools/dts_isa.py huff_mem_words). Every book is complete (asserted
// there), so a walk always ends in a leaf. The book roots are absolute (dts_hroot.mem).
//
// XQ's code reader lives here, beside the bit reader it shares: on `vop XQ` the vector
// engine is started and this module streams the band's 8 quantiser codes to it
// (xq_code / xq_valid / xq_ready), as the emulator's Machine.extract reads them:
//   Huffman  (abits <= 10, selector below the book count): 8 tree walks;
//   block    (abits <= 7 otherwise): 2 codes of nb bits, each 4 base-`levels` digits,
//            low digit first, offset by (levels - 1) / 2; a code of levels^4 or more is
//            decoded leniently (the four low digits kept) and pulses `lenient` (D5);
//   raw      (otherwise): 8 signed fields of abits - 3 bits (up to 23).
// Block-code division is restoring, one quotient bit a cycle. The first digit's
// division runs while the code's bits arrive (restoring division consumes the dividend
// MSB first, the bitstream's own order); each later digit divides the quotient in
// place, nb cycles. A code costs 4 nb cycles (nb 7..19), the emulator's model 24.
//
// Quartus 17 (CLAUDE.md): no `function`s and no N'(expr) size casts; the program,
// constant, Huffman and root ROMs and the record RAM are inferred memories.

`default_nettype none

module dts_seq (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          codec,              // 0 DTS, 1 AC-3 (change only in reset)

    // frames: a descriptor, then fr_len bytes
    input  wire   [15:0] fr_len,
    input  wire          fr_valid,
    output logic         fr_ready,
    input  wire    [7:0] in_byte,
    input  wire          in_valid,
    output logic         in_ready,

    // vector ops: a start pulse with the op and r8..r15; the sequencer waits for done
    output logic         vop_start,
    output logic   [5:0] vop_op,
    output logic [127:0] vop_args,           // {r15, r14, ..., r8}
    input  wire          vop_done,

    // XQ's codes, one at a time, to the vector engine; AC-3's AQ / AQC items with
    // their address (docs/ac3_engine.md "the seq->vec stream"; DTS's codes carry their
    // index 0..7 there)
    output logic  [23:0] xq_code,
    output logic  [10:0] xq_addr,
    output logic         xq_valid,
    input  wire          xq_ready,

    // status
    output logic         err_valid,          // an ERR: the frame is refused
    output logic   [4:0] err_code,
    output logic         frame_done,         // FEND: a frame decoded to its end
    output logic         overrun_bit,        // a bit read past the frame's end (as 0)
    output logic         lenient,            // an overflowed block code (D5)

    // the trace (benches): kind 0 a register write (addr = the register), 1 a store
    // (addr = the address), 2 an XQ code or AC-3 item (addr = xq_addr), 3 a record
    // write by an AC-3 unit (addr = the address), in program order
    output logic         tr_valid,
    output logic  [10:0] tr_pc,
    output logic   [1:0] tr_kind,
    output logic  [10:0] tr_addr,
    output logic  [23:0] tr_val
);

`include "dvd/dts/dts_ucode.svh"

    localparam [5:0] O_NOP = 6'd0, O_ALU = 6'd1, O_ALUI = 6'd2, O_LD = 6'd3, O_ST = 6'd4,
                     O_GET = 6'd5, O_GETR = 6'd6, O_VLC = 6'd7, O_BR = 6'd8, O_BRI = 6'd9,
                     O_JMP = 6'd10, O_CALL = 6'd11, O_RET = 6'd12, O_ERR = 6'd13,
                     O_VOP = 6'd14, O_FRAME = 6'd15, O_FEND = 6'd16, O_BPOS = 6'd17;
    localparam [5:0] V_XQ = 6'd1;

    // ------------------------------------------------------------------ memories
    (* ramstyle = "M10K" *) logic [39:0] prog  [0:UC_WORDS-1];
    logic [15:0] crom  [0:CONST_WORDS-1];
    (* ramstyle = "M10K" *) logic [17:0] huff_lo [0:2047];
    (* ramstyle = "M10K" *) logic [17:0] huff_hi [0:1023];          // nodes 2048.. (599 used)
    logic [11:0] hroot [0:HUFF_BOOKS-1];
    (* ramstyle = "M10K" *) logic [15:0] rec   [0:2047];
    initial begin
        $readmemh("dvd/dts/engine_ucode.mem", prog);
        $readmemh("dvd/dts/engine_const.mem", crom);
        $readmemh("dvd/dts/dts_huff_lo.mem", huff_lo);
        for (int i = 0; i < 1024; i++) huff_hi[i] = 18'd0;
        $readmemh("dvd/dts/dts_huff_hi.mem", huff_hi);
        $readmemh("dvd/dts/dts_hroot.mem", hroot);
        for (int i = 0; i < 2048; i++) rec[i] = 16'd0;   // as the emulator's record
    end

    // ------------------------------------------------------------------ state
    typedef enum logic [5:0] {S_RESET, S_DEC, S_LD, S_BITS, S_VLC_R, S_VLC_B, S_VLC_W,
                              S_VOP, S_FRAME, S_DRAIN, S_XQ_R, S_XQ_H, S_XQ_B, S_XQ_D,
                              S_XQ_W,
                              // AC-3's units
                              S_EX_G, S_UV, S_EX_W, S_SD_0, S_SD_1, S_SD_2, S_SD_3, S_BF,
                              S_BZ, S_AQ_R, S_AQ_D, S_AQ_G, S_AQ_Q, S_AQ_L, S_AQ_E,
                              S_AC_0, S_AC_1, S_AC_2, S_AC_3, S_AC_4, S_AC_5, S_AC_6,
                              S_AC_7, S_AC_8, S_AC_9} state_t;
    state_t state;

    logic [10:0] pc, npc, rom_addr;
    wire  [10:0] uc_reset = codec ? UC_AC3_RESET : UC_DTS_RESET;
    wire  [10:0] uc_frame = codec ? UC_AC3_FRAME : UC_DTS_FRAME;
    logic [39:0] ir;
    logic [15:0] rf [0:15];
    logic [10:0] stack [0:7];
    logic  [2:0] sp;

    wire  [5:0] op   = ir[39:34];
    wire  [3:0] rd   = ir[33:30];
    wire  [3:0] rs   = ir[29:26];
    wire  [3:0] rt   = ir[25:22];
    wire [15:0] imm  = ir[21:6];
    wire  [5:0] aux  = ir[5:0];
    wire [15:0] vrs  = (rs == 4'd0) ? 16'd0 : rf[rs];
    wire [15:0] vrt  = (rt == 4'd0) ? 16'd0 : rf[rt];
    wire [15:0] vrd  = (rd == 4'd0) ? 16'd0 : rf[rd];

    always_ff @(posedge clk) ir <= prog[rom_addr];

    // ------------------------------------------------------------------ the ALU
    logic [15:0] alu_b, alu_y;
    always_comb begin
        alu_b = (op == O_ALUI) ? imm : vrt;
        case (aux[3:0])
            4'd0:  alu_y = vrs + alu_b;
            4'd1:  alu_y = vrs - alu_b;
            4'd2:  alu_y = vrs & alu_b;
            4'd3:  alu_y = vrs | alu_b;
            4'd4:  alu_y = vrs ^ alu_b;
            4'd5:  alu_y = vrs << alu_b[3:0];
            4'd6:  alu_y = $signed(vrs) >>> alu_b[3:0];
            4'd7:  alu_y = vrs >> alu_b[3:0];
            4'd8:  alu_y = {15'd0, $signed(vrs) < $signed(alu_b)};
            4'd9:  alu_y = {15'd0, vrs < alu_b};
            4'd10: alu_y = {15'd0, vrs == alu_b};
            4'd11: alu_y = {15'd0, vrs != alu_b};
            4'd12: alu_y = ($signed(vrs) < $signed(alu_b)) ? vrs : alu_b;
            4'd13: alu_y = ($signed(vrs) < $signed(alu_b)) ? alu_b : vrs;
            default: alu_y = 16'd0;
        endcase
    end

    // branches: the condition in rd (eq ne lt ge); bri's constant is {rt, aux}, 10 bits
    wire [15:0] br_b  = (op == O_BRI) ? {{6{rt[3]}}, rt, aux} : vrt;
    wire        br_lt = $signed(vrs) < $signed(br_b);
    wire        br_eq = vrs == br_b;
    wire        taken = (rd[1:0] == 2'd0) ? br_eq : (rd[1:0] == 2'd1) ? !br_eq :
                        (rd[1:0] == 2'd2) ? br_lt : !br_lt;

    wire [15:0] maddr = vrs + vrt + imm;

    always_comb begin
        npc = pc + 11'd1;
        case (op)
            O_BR, O_BRI: if (taken) npc = imm[10:0];
            O_JMP, O_CALL: npc = imm[10:0];
            O_RET: npc = stack[sp - 3'd1];
            default: ;
        endcase
    end
    // a taken branch to an error vector refuses the frame (code = the vector's index)
    wire br_err = ((op == O_BR) || (op == O_BRI)) && taken && (imm[10:5] == UC_ERRV);
    wire multi = (op == O_LD) || (op == O_GET) || (op == O_GETR) || (op == O_VLC) ||
                 (op == O_VOP) || (op == O_FRAME) || (op == O_FEND) || (op == O_ERR) || br_err;

    // ------------------------------------------------------------------ XQ's mode
    // (from the vop's own arguments: r10 abits, r11 the quantiser selector)
    wire  [4:0] x_ab    = rf[10][4:0];
    wire  [2:0] x_sel   = rf[11][2:0];
    wire  [3:0] x_abm1  = x_ab[3:0] - 4'd1;           // abits - 1 (meaningful <= 10)
    wire  [3:0] x_gsize = XQ_GSIZE[4 * x_abm1 +: 4];
    wire        x_huff  = (x_ab <= 5'd10) && ({1'b0, x_sel} < x_gsize);
    wire        x_blk   = !x_huff && (x_ab <= 5'd7);
    wire  [2:0] x_abm1b = x_abm1[2:0];                 // 0..6 for block codes
    wire  [5:0] x_book  = XQ_QBOOK[6 * x_abm1 +: 6] + {3'd0, x_sel};

    // ------------------------------------------------------------------ bit reader
    logic  [7:0] cur;
    logic  [3:0] cur_n;                     // bits left in cur (0..8)
    logic        in_frame;
    logic [15:0] fbytes, ftaken;
    logic [17:0] fbits;                     // bits of the frame consumed
    logic        want_bit, bit_ok, bit_val;
    logic  [4:0] get_left;
    logic        can_out;                   // XQ's code register can take a code
    wire         zero_fill = in_frame && (ftaken == fbytes);
    always_comb begin
        fr_ready = (state == S_FRAME);
        can_out  = !xq_valid || xq_ready;
        want_bit = ((state == S_BITS || state == S_EX_G || state == S_AQ_G) && get_left != 5'd0) ||
                   state == S_VLC_B ||
                   ((state == S_XQ_H || state == S_XQ_B) && can_out);
        bit_ok   = 1'b0;
        bit_val  = 1'b0;
        in_ready = 1'b0;
        if (state == S_DRAIN) begin
            in_ready = ftaken != fbytes;
        end else if (want_bit) begin
            if (cur_n != 4'd0) begin
                bit_ok = 1'b1; bit_val = cur[cur_n - 4'd1];
            end else if (zero_fill) begin
                bit_ok = 1'b1;
            end else begin
                in_ready = 1'b1;
                bit_ok = in_valid; bit_val = in_byte[7];
            end
        end
    end

    // ------------------------------------------------------------------ the ROMs' reads
    logic [15:0] rec_q, crom_q, get_acc;
    logic        ld_const;
    logic [11:0] hroot_q, h_cur, h_root, h_ra;
    logic [17:0] hlo_q, hhi_q;
    logic        h_hi_d;
    wire  [17:0] hnode_q = h_hi_d ? hhi_q : hlo_q;
    logic  [5:0] hroot_ra;
    wire   [8:0] h_ent   = bit_val ? hnode_q[17:9] : hnode_q[8:0];
    wire         h_leaf  = h_ent[8];
    wire  [11:0] h_child = h_cur + {4'd0, h_ent[7:0]};
    logic  [7:0] v_sym;
    // the record RAM (1R1W): the program's loads and stores, and AC-3's units
    logic [10:0] rec_ra, rec_wa;
    logic [15:0] rec_wd;
    logic        rec_we;
    logic  [9:0] crom_ra;
    always_ff @(posedge clk) begin
        if (rec_we) rec[rec_wa] <= rec_wd;
        rec_q <= rec[rec_ra];
    end
    always_ff @(posedge clk) begin
        crom_q  <= crom[crom_ra];
        hroot_q <= hroot[hroot_ra];
        hlo_q   <= huff_lo[h_ra[10:0]];
        hhi_q   <= huff_hi[h_ra[9:0]];
        h_hi_d  <= h_ra[11];
    end

    always_comb for (int i = 0; i < 8; i++) vop_args[16*i +: 16] = rf[8 + i];

    // the program ROM address: what the next S_DEC executes
    logic drain_err;
    always_comb begin
        if (state == S_RESET)                 rom_addr = uc_reset;
        else if (state == S_DEC)              rom_addr = multi ? pc + 11'd1 : npc;
        else if (state == S_DRAIN && drain_err) rom_addr = uc_frame;
        else                                  rom_addr = pc + 11'd1;
    end

    // ------------------------------------------------------------------ XQ's reader
    logic  [1:0] x_mode;                    // 0 Huffman, 1 block, 2 raw
    logic  [2:0] x_k;                       // the code being read (0..7)
    logic  [4:0] x_n, x_it;                 // bits a field / iteration counter
    logic  [4:0] x_lev, x_rem;
    logic  [3:0] x_off;
    logic [18:0] x_dq;                      // a block code, then its quotients
    logic  [1:0] x_dig;                     // the digit being divided (0..3)
    logic        x_half;                    // which of the band's two block codes
    logic [23:0] x_acc;
    // one restoring step: the remainder with the next dividend bit in, against levels
    logic  [5:0] x_r2;
    logic        x_ge;
    logic  [5:0] x_r2s;
    logic        x_dbit;                    // the dividend bit an in-place step reads
    logic [18:0] x_dq_w;                    // x_dq with the in-place quotient bit
    logic  [4:0] x_itm1;
    always_comb begin
        x_itm1 = x_it - 5'd1;
        x_dbit = x_dq[x_itm1];
        x_r2   = {x_rem, (state == S_XQ_B) ? bit_val : x_dbit};
        x_ge   = x_r2 >= {1'b0, x_lev};
        x_r2s  = x_r2 - {1'b0, x_lev};
        x_dq_w = x_dq;
        x_dq_w[x_itm1] = x_ge;
    end
    wire  [4:0] x_rem_n  = x_ge ? x_r2s[4:0] : x_r2[4:0];
    wire  [5:0] x_digit  = {1'b0, x_rem_n} - {2'b0, x_off};   // -12..12
    wire [23:0] x_digit24 = {{18{x_digit[5]}}, x_digit};

    // ------------------------------------------------------------------ AC-3's units
    // (docs/ac3_engine.md A2; tools/ac3_isa.py Machine.run_op is each unit's
    // definition). The arguments are latched at the vop: from here on `ir` is the NEXT
    // instruction, so nothing below reads op / aux / imm or the registers' mux.
    //   EXPD    7-bit group codes; digits code/25, code/5 % 5, code % 5 from two in-place
    //           divisions by 5 (XQ's divider); each repeated rep times; an exponent
    //           outside 0..24 refuses (E_EXP) before it is written
    //   BAPSD   liba52's log-add over [j, eb) -> F_PSD, 2 cycles a bin (latab through
    //           the constant ROM's port, the index clamped at 255 as bit_allocation.sv)
    //   BAPFILL bap = baptab[clamp(156 + mask + 4 exp, 0, 304)]: read, look up, write,
    //           pipelined 1 a bin; BAPZERO bap = 0, read and write, 1 a bin. Both drain
    //           their pipeline before the next instruction (it reads those words)
    //   QRST    the grouped caches empty. ONLY here: they persist across every AQ / AQC
    //           of a block's mantissa stage
    //   AQ/AQC  the mantissa unit: per bin, its record word, its code bits (a grouped
    //           code split by the divider, high digit used first, the rest cached; a
    //           code past levels^n refuses, E_GROUP), its level (MLEV in the constant
    //           ROM), one item to the vector engine. AQC first sends the band's channel
    //           set and each coupled channel's coordinate (Q5.18, a serial shift, ch1's
    //           phase applied). A refusal mid-op pulses err_valid: the vector engine
    //           aborts the op on it.
    logic  [5:0] u_op;
    logic [10:0] u_base, u_cop;              // a channel's bin base; a coordinate word
    logic  [8:0] u_k, u_end;                 // the next bin to read; the end
    logic  [7:0] a_k;                        // the bin being coded
    logic  [7:0] u_n;                        // EXPD: groups left
    logic  [1:0] u_repm1, u_r, u_dig;
    logic  [6:0] u_e;                        // EXPD: the running exponent (seed <= 30)
    logic  [4:0] u_nb;                       // the code's bits (the divider's width)
    logic        u_ndiv2;                    // two divisions (else one)
    logic  [3:0] u_lo, u_mid;                // the divider's remainders: first, second
    logic [15:0] u_psd;
    logic [16:0] u_m156;                     // BAPFILL: 156 + mask
    logic  [1:0] sd_cls;                     // BAPSD: 0 keep, 1 nxt, 2 nxt + la, 3 psd + la
    logic [11:0] sd_nxt;
    logic        p1, p2;                     // the BAPFILL / BAPZERO pipeline
    logic [10:0] p1_a, p2_a;
    logic  [4:0] p2_e;
    logic  [2:0] u_slot, u_nf, u_ch;
    logic  [4:0] u_band, u_chin, u_dm;
    logic        u_cpl, u_dith, u_phs;
    logic  [5:0] a_bap;
    logic  [4:0] a_e;
    logic [15:0] a_m;                        // the bin's 16-bit mantissa value
    logic  [1:0] q1n, q2n;                   // the grouped caches: count, next, last
    logic        q4n;
    logic  [1:0] q1a, q1b;
    logic  [2:0] q2a, q2b;
    logic  [3:0] q4a;

    wire        a_g1 = a_bap == 6'h3F, a_g2 = a_bap == 6'h3E, a_g4 = a_bap == 6'h3D;
    wire        a_grp = a_g1 || a_g2 || a_g4;
    wire        a_hit = (a_g1 && q1n != 2'd0) || (a_g2 && q2n != 2'd0) || (a_g4 && q4n);
    wire  [4:0] a_lvoff = a_g1 ? 5'd0 : a_g2 ? 5'd3 : a_g4 ? 5'd16 : (a_bap == 6'd3) ? 5'd8 : 5'd27;
    wire  [3:0] a_pop = a_g1 ? {2'd0, (q1n == 2'd2) ? q1a : q1b} :
                        a_g2 ? {1'd0, (q2n == 2'd2) ? q2a : q2b} : q4a;
    // the record word just read (S_AQ_D, before a_bap holds it): its class
    wire  [5:0] r_bap = rec_q[13:8];
    wire        r_g1 = r_bap == 6'h3F, r_g2 = r_bap == 6'h3E, r_g4 = r_bap == 6'h3D;
    wire        r_grp = r_g1 || r_g2 || r_g4;
    wire        r_hit = (r_g1 && q1n != 2'd0) || (r_g2 && q2n != 2'd0) || (r_g4 && q4n);
    logic        u_sec;                      // the divider is on its second division

    // EXPD's digit and the exponent it makes
    wire  [3:0] ex_d = (u_dig == 2'd0) ? x_dq[3:0] : (u_dig == 2'd1) ? u_mid : u_lo;
    wire  [6:0] ex_e = u_e + {3'd0, ex_d} - 7'd2;
    wire        ex_bad = ex_e[6] || (ex_e > 7'd24);

    // BAPSD's log-add step
    wire [15:0] sd_nx16 = {4'd0, rec_q[4:0], 7'd0};
    wire [15:0] sd_del  = sd_nx16 - u_psd;
    wire [15:0] sd_sw   = $signed(sd_del) >>> 9;
    wire        sd_m1   = sd_sw == 16'hFFFF;
    wire        sd_z    = sd_sw == 16'h0000;
    wire        sd_rst  = ($signed(sd_sw) >= -16'sd6) && ($signed(sd_sw) <= -16'sd2);
    wire [15:0] sd_neg  = 16'd0 - sd_del;
    wire [15:0] sd_ix   = sd_m1 ? {1'b0, sd_neg[15:1]} : {1'b0, sd_del[15:1]};
    wire  [7:0] sd_la   = (sd_ix > 16'd255) ? 8'd255 : sd_ix[7:0];
    wire [15:0] sd_psdn = (sd_cls == 2'd1) ? {4'd0, sd_nxt} :
                          (sd_cls == 2'd2) ? {4'd0, sd_nxt} + crom_q :
                          (sd_cls == 2'd3) ? u_psd + crom_q : u_psd;

    // BAPFILL's baptab index
    wire [16:0] bf_ix  = u_m156 + {10'd0, rec_q[4:0], 2'd0};
    wire  [8:0] bf_cl  = bf_ix[16] ? 9'd0 : (bf_ix > 17'd304) ? 9'd304 : bf_ix[8:0];

    // a coordinate word {m[9:6], e == 15 [5], e + mstr [4:0]} -> (full << 3) to shift
    wire [17:0] co_full = rec_q[5] ? {rec_q[9:6], 14'd0} : {1'b1, rec_q[9:6], 13'd0};

    wire [10:0] u_ra = u_base + {2'd0, u_k};
    wire        u_more = u_k < u_end;

    // the units' memory ports and refusals
    logic        u_ref;
    logic  [4:0] u_rcode;
    always_comb begin
        case (state)
            S_SD_0, S_SD_1, S_SD_3, S_BF, S_BZ, S_AQ_R, S_AQ_E: rec_ra = u_ra;
            S_AC_0: rec_ra = 11'h001;                          // nf
            S_AC_1: rec_ra = 11'h007;                          // chincpl
            S_AC_2: rec_ra = 11'h088 + {6'd0, u_band};         // the band's phase flag
            S_AC_3: rec_ra = 11'h048;                          // ch 0's dither flag
            S_AC_4: rec_ra = 11'h049 + {8'd0, u_ch};
            S_AC_6: rec_ra = u_cop;                            // a coordinate
            default: rec_ra = maddr[10:0];
        endcase
        case (state)
            S_SD_2: crom_ra = AC_LATAB + {2'd0, sd_la};
            S_BF:   crom_ra = AC_BAPTAB + {1'd0, bf_cl};
            S_AQ_Q: crom_ra = AC_MLEV + {5'd0, a_lvoff} + {6'd0, a_hit ? a_pop : x_dq[3:0]};
            S_AQ_G: crom_ra = AC_MLEV + {5'd0, a_lvoff} + {6'd0, get_acc[3:0]};
            default: crom_ra = maddr[9:0];
        endcase
        rec_we = 1'b0; rec_wa = u_ra; rec_wd = 16'd0;
        u_ref = 1'b0; u_rcode = AC_E_EXP;
        case (state)
            S_DEC: if (op == O_ST) begin rec_we = 1'b1; rec_wa = maddr[10:0]; rec_wd = vrd; end
            S_EX_W: if (u_r != 2'd0) begin
                        rec_we = 1'b1; rec_wd = {9'd0, u_e};
                    end else if (ex_bad) u_ref = 1'b1;
                    else begin rec_we = 1'b1; rec_wd = {9'd0, ex_e}; end
            S_SD_1: if (!u_more) begin rec_we = 1'b1; rec_wa = AC_F_PSD; rec_wd = sd_nx16; end
            S_SD_3: if (!u_more) begin rec_we = 1'b1; rec_wa = AC_F_PSD; rec_wd = sd_psdn; end
            S_BF:   if (p2) begin
                        rec_we = 1'b1; rec_wa = p2_a; rec_wd = {2'd0, crom_q[5:0], 3'd0, p2_e};
                    end
            S_BZ:   if (p1) begin rec_we = 1'b1; rec_wa = p1_a; rec_wd = {11'd0, rec_q[4:0]}; end
            S_AQ_Q: if (!a_hit && (x_dq >= {14'd0, x_lev})) begin
                        u_ref = 1'b1; u_rcode = AC_E_GROUP;
                    end
            default: ;
        endcase
    end
    wire u_we = rec_we && (state != S_DEC);

    // ------------------------------------------------------------------ write port
    logic        w_en;
    logic  [3:0] w_rd, rd_hold;
    logic [15:0] w_val;
    always_comb begin
        w_en = 1'b0; w_rd = (state == S_DEC) ? rd : rd_hold; w_val = alu_y;
        case (state)
            S_DEC: case (op)
                O_ALU, O_ALUI: w_en = 1'b1;
                O_BPOS: begin w_en = 1'b1; w_val = fbits[15:0]; end
                default: ;
            endcase
            S_LD:    begin w_en = 1'b1; w_val = ld_const ? crom_q : rec_q; end
            S_BITS:  if (get_left == 5'd0) begin w_en = 1'b1; w_val = get_acc; end
            S_VLC_W: begin w_en = 1'b1; w_val = {{8{v_sym[7]}}, v_sym}; end
            S_FRAME: if (fr_valid) begin w_en = 1'b1; w_val = fr_len; end
            default: ;
        endcase
        if (w_rd == 4'd0) w_en = 1'b0;
    end

    // the Huffman ROM's address: the node in hnode_q, or the child stepped to now
    logic h_step;
    always_comb begin
        h_step = 1'b0;
        h_ra = h_cur;
        // S_DEC reads the root of the instruction's book: a vlc's, or XQ's for its vop
        hroot_ra = (state == S_DEC && op != O_VOP) ? (vrs[5:0] + imm[5:0]) : x_book;
        case (state)
            S_VLC_R, S_XQ_R: h_ra = hroot_q;
            S_VLC_B: if (bit_ok && !h_leaf) begin h_step = 1'b1; h_ra = h_child; end
            S_XQ_H:  if (bit_ok) begin
                         h_step = 1'b1;
                         h_ra = h_leaf ? h_root : h_child;       // a leaf: back to the root
                     end
            default: ;
        endcase
    end

    // ------------------------------------------------------------------ sequencing
    logic x_emit;                           // a code into xq_code this cycle
    logic [23:0] x_emit_v;
    logic [10:0] x_emit_a;
    always_comb begin
        x_emit = 1'b0; x_emit_v = 24'd0; x_emit_a = {8'd0, x_k};
        case (state)
            // AC-3: a bin {dither [23], bap 0 [22], exp [21:17], m16 [16:0]} at {slot, bin}
            S_AQ_E: if (can_out) begin
                x_emit = 1'b1; x_emit_a = {u_slot, a_k};
                x_emit_v = {!u_cpl && u_dith && (a_bap == 6'd0), a_bap == 6'd0, a_e, a_m[15], a_m};
            end
            // AQC: the band's channel set, then a coupled channel's coordinate
            S_AC_5: if (can_out) begin
                x_emit = 1'b1; x_emit_a = 11'h7F0; x_emit_v = {11'd0, u_nf, u_chin, u_dm};
            end
            S_AC_9: if (can_out) begin
                x_emit = 1'b1; x_emit_a = {8'hE0, u_ch}; x_emit_v = x_acc;
            end
            S_XQ_H: if (bit_ok && h_leaf) begin
                x_emit = 1'b1; x_emit_v = {{16{h_ent[7]}}, h_ent[7:0]};
            end
            S_XQ_B: if (bit_ok && x_it == 5'd1) begin
                x_emit = 1'b1;
                x_emit_v = (x_mode == 2'd1) ? x_digit24 : {x_acc[22:0], bit_val};
            end
            S_XQ_D: if (x_it == 5'd1 && can_out) begin
                x_emit = 1'b1; x_emit_v = x_digit24;
            end
            default: ;
        endcase
    end

    always_ff @(posedge clk) begin
        vop_start  <= 1'b0;
        err_valid  <= 1'b0;
        frame_done <= 1'b0;
        overrun_bit <= 1'b0;
        lenient    <= 1'b0;
        tr_valid   <= w_en || rec_we || x_emit;
        tr_pc      <= pc;
        tr_kind    <= w_en ? 2'd0 : x_emit ? 2'd2 : u_we ? 2'd3 : 2'd1;
        tr_addr    <= w_en ? {7'd0, w_rd} : x_emit ? x_emit_a : rec_wa;
        tr_val     <= w_en ? {8'd0, w_val} : x_emit ? x_emit_v : {8'd0, rec_wd};
        if (w_en) rf[w_rd] <= w_val;
        if (xq_valid && xq_ready) xq_valid <= 1'b0;
        if (x_emit) begin xq_valid <= 1'b1; xq_code <= x_emit_v; xq_addr <= x_emit_a; end
        if (h_step) h_cur <= h_ra;
        if (bit_ok) begin
            if (cur_n != 4'd0) cur_n <= cur_n - 4'd1;
            else if (zero_fill) overrun_bit <= 1'b1;
            else begin
                cur <= in_byte; cur_n <= 4'd7;
                if (in_frame) ftaken <= ftaken + 16'd1;
            end
            if (in_frame) fbits <= fbits + 18'd1;
        end
        if (!rst_n) begin
            state <= S_RESET;
            pc <= uc_reset;
            sp <= 3'd0;
            cur_n <= 4'd0;
            in_frame <= 1'b0;
            fbytes <= 16'd0; ftaken <= 16'd0; fbits <= 18'd0;
            xq_valid <= 1'b0;
            drain_err <= 1'b0;
            for (int i = 0; i < 16; i++) rf[i] <= 16'd0;
        end else case (state)
            S_RESET: state <= S_DEC;
            S_DEC: begin
                rd_hold <= rd;
                case (op)
                    O_LD: begin ld_const <= maddr[12]; state <= S_LD; end
                    O_GET, O_GETR: begin
                        get_acc <= 16'd0;
                        get_left <= (op == O_GET) ? imm[4:0] : vrt[4:0];
                        state <= S_BITS;
                    end
                    O_VLC: state <= S_VLC_R;          // the root ROM reads the book now
                    O_CALL: begin stack[sp] <= pc + 11'd1; sp <= sp + 3'd1; end
                    O_RET: sp <= sp - 3'd1;
                    O_ERR, O_BR, O_BRI: if (op == O_ERR || br_err) begin
                        err_valid <= 1'b1; err_code <= imm[4:0]; sp <= 3'd0;
                        cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b1;
                        state <= S_DRAIN;
                    end
                    O_VOP: begin
                        vop_op <= aux; u_op <= aux;
                        // AC-3's arguments: r8.. (tools/ac3_isa.py run_op)
                        u_base <= rf[8][10:0]; u_k <= rf[9][8:0]; u_end <= rf[10][8:0];
                        if (aux == V_EXPD) begin
                            u_n <= rf[10][7:0];
                            u_repm1 <= (rf[11][1:0] == 2'd1) ? 2'd0 : (rf[11][1:0] == 2'd2) ? 2'd1 : 2'd3;
                            u_e <= rf[12][6:0];
                            u_r <= 2'd0; u_dig <= 2'd0;
                            get_acc <= 16'd0; get_left <= 5'd7;
                            if (rf[10][7:0] == 8'd0) pc <= pc + 11'd1;
                            else state <= S_EX_G;
                        end else if (aux == V_BAPSD) state <= S_SD_0;
                        else if (aux == V_BAPFILL) begin
                            u_m156 <= {rf[11][15], rf[11]} + 17'd156;
                            p1 <= 1'b0; p2 <= 1'b0; state <= S_BF;
                        end else if (aux == V_BAPZERO) begin
                            p1 <= 1'b0; state <= S_BZ;
                        end else if (aux == V_QRST) begin
                            q1n <= 2'd0; q2n <= 2'd0; q4n <= 1'b0;
                            pc <= pc + 11'd1;
                        end else if (aux == V_AQ) begin
                            vop_start <= 1'b1;
                            u_slot <= rf[8][2:0]; u_base <= rf[9][10:0];
                            u_k <= rf[10][8:0]; u_end <= rf[11][8:0];
                            u_dith <= rf[12] != 16'd0; u_cpl <= 1'b0;
                            if (rf[10][8:0] < rf[11][8:0]) state <= S_AQ_R; else state <= S_XQ_W;
                        end else if (aux == V_AQC) begin
                            vop_start <= 1'b1;
                            u_band <= rf[8][4:0]; u_base <= AC_CPLBASE; u_slot <= 3'd5;
                            u_cpl <= 1'b1; u_dith <= 1'b0; u_dm <= 5'd0;
                            state <= S_AC_0;
                        end else if (aux == V_XQ) begin
                            vop_start <= 1'b1;
                            x_k <= 3'd0; x_half <= 1'b0; x_dig <= 2'd0;
                            x_rem <= 5'd0; x_dq <= 19'd0;
                            x_lev <= XQ_LEVELS[5 * x_abm1b +: 5];
                            x_off <= XQ_LEVELS[5 * x_abm1b + 1 +: 4];   // (levels - 1) / 2: levels is odd
                            if (x_huff) begin
                                x_mode <= 2'd0; state <= S_XQ_R;
                            end else if (x_blk) begin
                                x_mode <= 2'd1; x_n <= XQ_BNBITS[5 * x_abm1b +: 5];
                                x_it <= XQ_BNBITS[5 * x_abm1b +: 5]; state <= S_XQ_B;
                            end else begin
                                x_mode <= 2'd2; x_n <= x_ab - 5'd3; x_it <= x_ab - 5'd3;
                                state <= S_XQ_B;
                            end
                        end else begin vop_start <= 1'b1; state <= S_VOP; end
                    end
                    O_FRAME: state <= S_FRAME;
                    O_FEND: begin
                        cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b0;
                        frame_done <= 1'b1; state <= S_DRAIN;
                    end
                    default: ;
                endcase
                if (!multi) pc <= npc;
            end
            S_LD: begin pc <= pc + 11'd1; state <= S_DEC; end
            S_BITS: begin
                if (get_left == 5'd0) begin
                    pc <= pc + 11'd1; state <= S_DEC;
                end else if (bit_ok) begin
                    get_acc <= {get_acc[14:0], bit_val};
                    get_left <= get_left - 5'd1;
                end
            end
            S_VLC_R: begin h_cur <= hroot_q; state <= S_VLC_B; end
            S_VLC_B: if (bit_ok && h_leaf) begin v_sym <= h_ent[7:0]; state <= S_VLC_W; end
            S_VLC_W: begin pc <= pc + 11'd1; state <= S_DEC; end
            S_FRAME: if (fr_valid) begin
                in_frame <= 1'b1; fbytes <= fr_len; ftaken <= 16'd0; fbits <= 18'd0;
                pc <= pc + 11'd1; state <= S_DEC;
            end
            S_VOP: if (vop_done) begin pc <= pc + 11'd1; state <= S_DEC; end
            S_DRAIN: begin
                if (ftaken == fbytes) begin
                    pc <= drain_err ? uc_frame : pc + 11'd1;
                    drain_err <= 1'b0;
                    state <= S_DEC;
                end else if (in_valid) ftaken <= ftaken + 16'd1;
            end
            // ---- XQ: Huffman codes
            S_XQ_R: begin h_cur <= hroot_q; h_root <= hroot_q; state <= S_XQ_H; end
            S_XQ_H: if (bit_ok && h_leaf) begin
                x_k <= x_k + 3'd1;
                if (x_k == 3'd7) state <= S_XQ_W;
            end
            // ---- XQ: a field's bits (raw: sign-extended as read; block: digit 0's
            //      division as the bits arrive)
            S_XQ_B: if (bit_ok) begin
                if (x_mode == 2'd1) begin
                    x_rem <= x_rem_n;
                    x_dq <= {x_dq[17:0], x_ge};
                end else
                    x_acc <= (x_it == x_n) ? {24{bit_val}} : {x_acc[22:0], bit_val};
                x_it <= x_it - 5'd1;
                if (x_it == 5'd1) begin
                    if (x_mode == 2'd1) begin             // digit 0 out; divide its quotient
                        x_k <= x_k + 3'd1;
                        x_dig <= 2'd1; x_rem <= 5'd0; x_it <= x_n;
                        state <= S_XQ_D;
                    end else begin
                        x_k <= x_k + 3'd1; x_it <= x_n;
                        if (x_k == 3'd7) state <= S_XQ_W;
                    end
                end
            end
            // ---- XQ: block-code digits 1..3, in place
            S_XQ_D: if (x_it != 5'd1 || can_out) begin
                x_rem <= x_rem_n;
                x_dq <= x_dq_w;
                x_it <= x_it - 5'd1;
                if (x_it == 5'd1) begin
                    x_k <= x_k + 3'd1;
                    x_rem <= 5'd0; x_it <= x_n;
                    if (x_dig != 2'd3) x_dig <= x_dig + 2'd1;
                    else begin
                        if (x_dq_w != 19'd0) lenient <= 1'b1;   // past levels^4 (D5)
                        x_dig <= 2'd0; x_dq <= 19'd0; x_half <= 1'b1;
                        if (x_half) state <= S_XQ_W; else state <= S_XQ_B;
                    end
                end
            end
            // ---- XQ: the engine writes the last code's sample (AQ / AQC: its last bin)
            S_XQ_W: if (vop_done) begin pc <= pc + 11'd1; state <= S_DEC; end

            // ---- AC-3: a code's bits (EXPD's group, a mantissa)
            S_EX_G, S_AQ_G: if (get_left != 5'd0) begin
                if (bit_ok) begin
                    get_acc <= {get_acc[14:0], bit_val};
                    get_left <= get_left - 5'd1;
                end
            end else if (state == S_EX_G) begin
                x_dq <= {12'd0, get_acc[6:0]}; x_rem <= 5'd0; x_it <= 5'd7; u_nb <= 5'd7;
                x_lev <= 5'd5; u_ndiv2 <= 1'b1; u_sec <= 1'b0; state <= S_UV;
            end else if (a_grp) begin
                x_dq <= {12'd0, get_acc[6:0]}; x_rem <= 5'd0;
                x_it <= a_g1 ? 5'd5 : 5'd7; u_nb <= a_g1 ? 5'd5 : 5'd7;
                x_lev <= a_g1 ? 5'd3 : a_g2 ? 5'd5 : 5'd11;
                u_ndiv2 <= !a_g4; u_sec <= 1'b0; state <= S_UV;
            end else if (a_bap == 6'd3 || a_bap == 6'd4) state <= S_AQ_L;   // MLEV read now
            else begin
                a_m <= get_acc << (5'd16 - a_bap[4:0]);                     // direct, bap 5..16
                state <= S_AQ_E;
            end
            // ---- the divider, in place: x_dq / levels over u_nb bits
            S_UV: begin
                x_rem <= x_rem_n; x_dq <= x_dq_w; x_it <= x_it - 5'd1;
                if (x_it == 5'd1) begin
                    if (u_ndiv2) begin
                        u_lo <= x_rem_n[3:0]; x_rem <= 5'd0; x_it <= u_nb;
                        u_ndiv2 <= 1'b0; u_sec <= 1'b1;
                    end else begin
                        if (u_sec) u_mid <= x_rem_n[3:0]; else u_lo <= x_rem_n[3:0];
                        if (u_op == V_EXPD) state <= S_EX_W; else state <= S_AQ_Q;
                    end
                end
            end
            // ---- EXPD: the group's three digits, each written rep times
            S_EX_W: if (u_r != 2'd0 || !ex_bad) begin
                if (u_r == 2'd0) u_e <= ex_e;
                u_k <= u_k + 9'd1;
                if (u_r == u_repm1) begin
                    u_r <= 2'd0;
                    if (u_dig == 2'd2) begin
                        u_dig <= 2'd0;
                        u_n <= u_n - 8'd1;
                        if (u_n == 8'd1) begin pc <= pc + 11'd1; state <= S_DEC; end
                        else begin get_acc <= 16'd0; get_left <= 5'd7; state <= S_EX_G; end
                    end else u_dig <= u_dig + 2'd1;
                end else u_r <= u_r + 2'd1;
            end
            // ---- BAPSD
            S_SD_0: begin u_k <= u_k + 9'd1; state <= S_SD_1; end
            S_SD_1: begin
                u_psd <= sd_nx16;
                if (u_more) begin u_k <= u_k + 9'd1; state <= S_SD_2; end
                else begin pc <= pc + 11'd1; state <= S_DEC; end
            end
            S_SD_2: begin
                sd_nxt <= sd_nx16[11:0];
                sd_cls <= sd_rst ? 2'd1 : sd_m1 ? 2'd2 : sd_z ? 2'd3 : 2'd0;
                state <= S_SD_3;
            end
            S_SD_3: begin
                u_psd <= sd_psdn;
                if (u_more) begin u_k <= u_k + 9'd1; state <= S_SD_2; end
                else begin pc <= pc + 11'd1; state <= S_DEC; end
            end
            // ---- BAPFILL: read, look up, write; BAPZERO: read, write
            S_BF: begin
                p1 <= u_more; p1_a <= u_ra;
                if (u_more) u_k <= u_k + 9'd1;
                p2 <= p1; p2_a <= p1_a; p2_e <= rec_q[4:0];
                if (!u_more && !p1) begin pc <= pc + 11'd1; state <= S_DEC; end
            end
            S_BZ: begin
                p1 <= u_more; p1_a <= u_ra;
                if (u_more) u_k <= u_k + 9'd1;
                else begin pc <= pc + 11'd1; state <= S_DEC; end
            end
            // ---- AQ / AQC: a bin
            S_AQ_R: state <= S_AQ_D;
            S_AQ_D: begin
                a_bap <= r_bap; a_e <= rec_q[4:0]; a_k <= u_k[7:0]; u_k <= u_k + 9'd1;
                get_acc <= 16'd0;
                get_left <= r_g1 ? 5'd5 : (r_g2 || r_g4) ? 5'd7 : r_bap[4:0];
                if (r_bap == 6'd0) begin a_m <= 16'd0; state <= S_AQ_E; end
                else if (r_grp && r_hit) state <= S_AQ_Q;
                else state <= S_AQ_G;
            end
            S_AQ_Q: if (a_hit) begin                         // a cached digit
                if (a_g1) q1n <= q1n - 2'd1;
                else if (a_g2) q2n <= q2n - 2'd1;
                else q4n <= 1'b0;
                state <= S_AQ_L;
            end else if (x_dq < {14'd0, x_lev}) begin        // a fresh code: cache the rest
                if (a_g1) begin q1a <= u_mid[1:0]; q1b <= u_lo[1:0]; q1n <= 2'd2; end
                else if (a_g2) begin q2a <= u_mid[2:0]; q2b <= u_lo[2:0]; q2n <= 2'd2; end
                else begin q4a <= u_lo; q4n <= 1'b1; end
                state <= S_AQ_L;
            end
            S_AQ_L: begin a_m <= crom_q; state <= S_AQ_E; end
            S_AQ_E: if (can_out) begin                         // the next bin is read now
                if (u_more) state <= S_AQ_D; else state <= S_XQ_W;
            end
            // ---- AQC: the band's header
            S_AC_0: state <= S_AC_1;
            S_AC_1: begin u_nf <= rec_q[2:0]; state <= S_AC_2; end
            S_AC_2: begin u_chin <= rec_q[4:0]; state <= S_AC_3; end
            S_AC_3: begin u_phs <= rec_q[0]; u_ch <= 3'd0; state <= S_AC_4; end
            S_AC_4: begin
                u_dm[u_ch] <= rec_q[0];
                if (u_ch == u_nf - 3'd1) begin u_ch <= 3'd0; state <= S_AC_5; end
                else u_ch <= u_ch + 3'd1;
            end
            S_AC_5: if (can_out) begin u_cop <= 11'h0A0 + {6'd0, u_band}; state <= S_AC_6; end
            S_AC_6: if (u_ch == u_nf) state <= S_AQ_R;
                    else if (!u_chin[u_ch]) begin u_ch <= u_ch + 3'd1; u_cop <= u_cop + 11'd18; end
                    else state <= S_AC_7;
            S_AC_7: begin x_acc <= {3'd0, co_full, 3'd0}; x_it <= rec_q[4:0]; state <= S_AC_8; end
            S_AC_8: if (x_it != 5'd0) begin
                x_acc <= {1'b0, x_acc[23:1]}; x_it <= x_it - 5'd1;
            end else begin
                if (u_ch == 3'd1 && u_phs) x_acc <= 24'd0 - x_acc;
                state <= S_AC_9;
            end
            S_AC_9: if (can_out) begin
                u_ch <= u_ch + 3'd1; u_cop <= u_cop + 11'd18; state <= S_AC_6;
            end
            default: state <= S_RESET;
        endcase
        // a unit's refusal: as ERR (drain the frame, restart at FRAME)
        if (rst_n && u_ref) begin
            err_valid <= 1'b1; err_code <= u_rcode; sp <= 3'd0;
            cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b1; xq_valid <= 1'b0;
            state <= S_DRAIN;
        end
    end

endmodule
