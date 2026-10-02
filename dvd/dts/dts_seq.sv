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
// untaken bytes; ERR then restarts the program at FRAME (the emulator's `err`).
//
// The Huffman unit walks a binary tree, one code bit a cycle (53 of the 62 core books
// are not canonical, docs/dts_decoder.md D2): a node is {right, left}, an entry
// {leaf, value[11:0]}; a leaf's value is the signed symbol, else the child's index.
// Every book is complete (tools/dts_isa.py asserts it), so a walk always ends in a leaf.
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

    // frames: a descriptor, then fr_len bytes
    input  wire   [15:0] fr_len,
    input  wire          fr_valid,
    output logic         fr_ready,
    input  wire    [7:0] in_byte,
    input  wire          in_valid,
    output logic         in_ready,

    // vector ops: a start pulse with the op and r8..r15; the sequencer waits for done
    output logic         vop_start,
    output logic   [3:0] vop_op,
    output logic [127:0] vop_args,           // {r15, r14, ..., r8}
    input  wire          vop_done,

    // XQ's codes, one at a time, to the vector engine
    output logic  [23:0] xq_code,
    output logic         xq_valid,
    input  wire          xq_ready,

    // status
    output logic         err_valid,          // an ERR: the frame is refused
    output logic   [4:0] err_code,
    output logic         frame_done,         // FEND: a frame decoded to its end
    output logic         overrun_bit,        // a bit read past the frame's end (as 0)
    output logic         lenient,            // an overflowed block code (D5)

    // the trace (benches): kind 0 a register write (addr = the register), 1 a store
    // (addr = the address), 2 an XQ code (addr = its index), in program order
    output logic         tr_valid,
    output logic   [9:0] tr_pc,
    output logic   [1:0] tr_kind,
    output logic  [10:0] tr_addr,
    output logic  [23:0] tr_val
);

`include "dvd/dts/dts_ucode.svh"

    localparam [5:0] O_NOP = 6'd0, O_ALU = 6'd1, O_ALUI = 6'd2, O_LD = 6'd3, O_ST = 6'd4,
                     O_GET = 6'd5, O_GETR = 6'd6, O_VLC = 6'd7, O_BR = 6'd8, O_BRI = 6'd9,
                     O_JMP = 6'd10, O_CALL = 6'd11, O_RET = 6'd12, O_ERR = 6'd13,
                     O_VOP = 6'd14, O_FRAME = 6'd15, O_FEND = 6'd16, O_BPOS = 6'd17;
    localparam [3:0] V_XQ = 4'd1;

    // ------------------------------------------------------------------ memories
    (* ramstyle = "M10K" *) logic [39:0] prog  [0:UC_WORDS-1];
    logic [15:0] crom  [0:CONST_WORDS-1];
    (* ramstyle = "M10K" *) logic [25:0] huff  [0:HUFF_NODES-1];
    logic [11:0] hroot [0:HUFF_BOOKS-1];
    (* ramstyle = "M10K" *) logic [15:0] rec   [0:2047];
    initial begin
        $readmemh("dvd/dts/dts_ucode.mem", prog);
        $readmemh("dvd/dts/dts_const.mem", crom);
        $readmemh("dvd/dts/dts_huff.mem", huff);
        $readmemh("dvd/dts/dts_hroot.mem", hroot);
        for (int i = 0; i < 2048; i++) rec[i] = 16'd0;   // as the emulator's record
    end

    // ------------------------------------------------------------------ state
    typedef enum logic [3:0] {S_RESET, S_DEC, S_LD, S_BITS, S_VLC_R, S_VLC_B, S_VLC_W,
                              S_VOP, S_FRAME, S_DRAIN, S_XQ_R, S_XQ_H, S_XQ_B, S_XQ_D,
                              S_XQ_W} state_t;
    state_t state;

    logic  [9:0] pc, npc, rom_addr;
    logic [39:0] ir;
    logic [15:0] rf [0:15];
    logic  [9:0] stack [0:7];
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
        npc = pc + 10'd1;
        case (op)
            O_BR, O_BRI: if (taken) npc = imm[9:0];
            O_JMP, O_CALL: npc = imm[9:0];
            O_RET: npc = stack[sp - 3'd1];
            default: ;
        endcase
    end
    wire multi = (op == O_LD) || (op == O_GET) || (op == O_GETR) || (op == O_VLC) ||
                 (op == O_VOP) || (op == O_FRAME) || (op == O_FEND) || (op == O_ERR);

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
        want_bit = (state == S_BITS && get_left != 5'd0) || state == S_VLC_B ||
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
    logic [25:0] hnode_q;
    logic  [5:0] hroot_ra;
    wire  [12:0] h_ent   = bit_val ? hnode_q[25:13] : hnode_q[12:0];
    wire         h_leaf  = h_ent[12];
    logic [11:0] v_sym;
    always_ff @(posedge clk) begin
        rec_q   <= rec[maddr[10:0]];
        crom_q  <= crom[maddr[6:0]];
        hroot_q <= hroot[hroot_ra];
        hnode_q <= huff[h_ra];
    end

    always_comb for (int i = 0; i < 8; i++) vop_args[16*i +: 16] = rf[8 + i];

    // the program ROM address: what the next S_DEC executes
    logic drain_err;
    always_comb begin
        if (state == S_RESET)                 rom_addr = UC_RESET;
        else if (state == S_DEC)              rom_addr = multi ? pc + 10'd1 : npc;
        else if (state == S_DRAIN && drain_err) rom_addr = UC_FRAME;
        else                                  rom_addr = pc + 10'd1;
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
            S_VLC_W: begin w_en = 1'b1; w_val = {{4{v_sym[11]}}, v_sym}; end
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
            S_VLC_B: if (bit_ok && !h_leaf) begin h_step = 1'b1; h_ra = h_ent[11:0]; end
            S_XQ_H:  if (bit_ok) begin
                         h_step = 1'b1;
                         h_ra = h_leaf ? h_root : h_ent[11:0];   // a leaf: back to the root
                     end
            default: ;
        endcase
    end

    // ------------------------------------------------------------------ sequencing
    logic x_emit;                           // a code into xq_code this cycle
    logic [23:0] x_emit_v;
    always_comb begin
        x_emit = 1'b0; x_emit_v = 24'd0;
        case (state)
            S_XQ_H: if (bit_ok && h_leaf) begin
                x_emit = 1'b1; x_emit_v = {{12{h_ent[11]}}, h_ent[11:0]};
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
        tr_valid   <= w_en || (state == S_DEC && op == O_ST) || x_emit;
        tr_pc      <= pc;
        tr_kind    <= w_en ? 2'd0 : x_emit ? 2'd2 : 2'd1;
        tr_addr    <= w_en ? {7'd0, w_rd} : x_emit ? {8'd0, x_k} : maddr[10:0];
        tr_val     <= w_en ? {8'd0, w_val} : x_emit ? x_emit_v : {8'd0, vrd};
        if (w_en) rf[w_rd] <= w_val;
        if (xq_valid && xq_ready) xq_valid <= 1'b0;
        if (x_emit) begin xq_valid <= 1'b1; xq_code <= x_emit_v; end
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
            pc <= UC_RESET;
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
                    O_ST: rec[maddr[10:0]] <= vrd;
                    O_GET, O_GETR: begin
                        get_acc <= 16'd0;
                        get_left <= (op == O_GET) ? imm[4:0] : vrt[4:0];
                        state <= S_BITS;
                    end
                    O_VLC: state <= S_VLC_R;          // the root ROM reads the book now
                    O_CALL: begin stack[sp] <= pc + 10'd1; sp <= sp + 3'd1; end
                    O_RET: sp <= sp - 3'd1;
                    O_ERR: begin
                        err_valid <= 1'b1; err_code <= imm[4:0]; sp <= 3'd0;
                        cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b1;
                        state <= S_DRAIN;
                    end
                    O_VOP: begin
                        vop_start <= 1'b1; vop_op <= aux[3:0];
                        if (aux[3:0] == V_XQ) begin
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
                        end else state <= S_VOP;
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
            S_LD: begin pc <= pc + 10'd1; state <= S_DEC; end
            S_BITS: begin
                if (get_left == 5'd0) begin
                    pc <= pc + 10'd1; state <= S_DEC;
                end else if (bit_ok) begin
                    get_acc <= {get_acc[14:0], bit_val};
                    get_left <= get_left - 5'd1;
                end
            end
            S_VLC_R: begin h_cur <= hroot_q; state <= S_VLC_B; end
            S_VLC_B: if (bit_ok && h_leaf) begin v_sym <= h_ent[11:0]; state <= S_VLC_W; end
            S_VLC_W: begin pc <= pc + 10'd1; state <= S_DEC; end
            S_FRAME: if (fr_valid) begin
                in_frame <= 1'b1; fbytes <= fr_len; ftaken <= 16'd0; fbits <= 18'd0;
                pc <= pc + 10'd1; state <= S_DEC;
            end
            S_VOP: if (vop_done) begin pc <= pc + 10'd1; state <= S_DEC; end
            S_DRAIN: begin
                if (ftaken == fbytes) begin
                    pc <= drain_err ? UC_FRAME : pc + 10'd1;
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
            // ---- XQ: the engine writes the last code's sample
            S_XQ_W: if (vop_done) begin pc <= pc + 10'd1; state <= S_DEC; end
            default: state <= S_RESET;
        endcase
    end

endmodule
