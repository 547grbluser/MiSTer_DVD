// dvd/dts/dts_vec.sv -- the DTS core decoder's vector engine
//
// The hardwired loops, started by the sequencer's `vop` (dts_seq.sv) with their
// arguments in r8..r15 (a0..a7 here). Every op is defined by tools/dts_isa.py
// (Machine.vop), i.e. by tools/dts_fixed.py's arithmetic; bench/dvd/run_dts.sh scores
// the engine buffers after every op by checksum and every PCM pair bit-exact.
// docs/dts_decoder.md sec 10.
//
//   op 0 XCLR    zero the subsubframe's subband buffer X (1,280 words)
//   op 1 XQ      a0 ch, a1 band, a2 abits, a3 selector, a4 scale index, a5 adjustment
//                index, a6 lossless: dequantise the 8 codes dts_seq streams in
//   op 2 XVQ     a0 ch, a1 band, a2 VQ index, a3 ssf, a4 scale index
//   op 3 ADPCM   a0 ch, a1 band, a2 pvq index, a3 predicted?
//   op 4 JOINT   a0 ch, a1 band, a2 source ch, a3 joint scale index
//   op 5 BFLY    a0 ch p, a1 ch q: X[p], X[q] = X[p] + X[q], X[p] - X[q], every band
//   op 6 MIXSYN  a0 j, a1 AMODE, a2..a6 nmix (unused: see V_MX), a7 perfect: for each side, mix
//                column j into 32 subband samples, the half IMDCT into that side's
//                ring, the window; then the 32 PCM pairs out
//   op 7 HCLR    a0 ch, a1 from band: clear the ADPCM history from that band up
//   op 8 CNT     a0 counter (dts_top counts it)
//
// ONE DATAPATH, two stages: a previously built decoder of this kind closed timing only
// once its multiply and its round / clip chain had a cycle each. Issue (the per-state
// wiring below): the operands, the accumulator's preset, the rounding, the addend and
// the write addresses of one element; the product is registered (in the DSP block)
// with all of them. Stage Y, a cycle later: sum = (clear ? preset : acc) + product;
// round half up (or floor) by a variable shift; optionally clip23 before the addend
// (ADPCM's two clips); negate (the IMDCT's x - round(c y)); add; shift left (the
// IMDCT's pre-shift undone); clip23; write. PCM takes one more registered stage
// (round to s16, saturate) into a 32-pair buffer per side.
//
// THE HALF IMDCT is a ROM program of 597 multiply-accumulate terms
// (tools/dts_vecrom.py, which proves it equal to FFmpeg's imdct_half_32), executed
// one term a cycle through a three-deep pipeline: fetch the term, read its scratch
// word and coefficient, issue. Each of its seven stages writes the scratch region the
// next one reads, so the first term of a stage is held two cycles at the boundary
// until the previous stage's last write has landed.
//
// Codebooks (D4) are off-chip: a request for one 64-bit row (cb_sel 0: an ADPCM
// vector, 4 x int16 at [16i +: 16]; 1: a VQ vector's slice for subsubframe ssf,
// 8 x int8 at [8k +: 8], cb_addr = {index, ssf}), answered by cb_valid any number of
// cycles later. P1b fetches on demand: docs/dts_decoder.md "P1b" records the cost.
//
// Quartus 17 (CLAUDE.md): no `function`s and no N'(expr) size casts; sign extension
// by replication; every RAM a power-of-two depth or a ROM.

`default_nettype none

module dts_vec (
    input  wire          clk,
    input  wire          rst_n,

    input  wire          start,
    input  wire    [5:0] op,
    input  wire  [127:0] args,
    output logic         done,

    input  wire   [23:0] xq_code,
    input  wire          xq_valid,
    output logic         xq_ready,

    output logic         cb_req,
    output logic         cb_sel,
    output logic  [11:0] cb_addr,
    input  wire          cb_valid,
    input  wire   [63:0] cb_data,

    output logic  [15:0] pcm_l,
    output logic  [15:0] pcm_r,
    output logic         pcm_valid,
    input  wire          pcm_ready
);

`include "dvd/dts/dts_ucode.svh"
`include "dvd/dts/dts_vec.svh"

    localparam [5:0] OP_XCLR = 6'd0, OP_XQ = 6'd1, OP_XVQ = 6'd2, OP_ADPCM = 6'd3,
                     OP_JOINT = 6'd4, OP_BFLY = 6'd5, OP_MIXSYN = 6'd6, OP_HCLR = 6'd7;

    // ------------------------------------------------------------------ ROMs
    logic [23:0] vk    [0:VK_WORDS-1];
    logic [23:0] win   [0:1023];
    logic [19:0] iprog [0:IPROG_WORDS-1];
    logic [26:0] icoef [0:ICOEF_WORDS-1];
    initial begin
        $readmemh("dvd/dts/dts_vconst.mem", vk);
        $readmemh("dvd/dts/dts_win.mem", win);
        $readmemh("dvd/dts/dts_iprog.mem", iprog);
        $readmemh("dvd/dts/dts_icoef.mem", icoef);
    end

    // ------------------------------------------------------------------ RAMs
    (* ramstyle = "M10K" *) logic [24:0] xb   [0:2047];   // X {ch, band, j}: 25 bits (a butterflied band)
    (* ramstyle = "M10K" *) logic [23:0] hb   [0:1023];   // ADPCM history {ch, band, k}, k 0 the oldest
    (* ramstyle = "M10K" *) logic [23:0] ring [0:1023];   // IMDCT rings {side, i}
    // the four small buffers share one M10K (each alone took a block): their phases
    // never overlap -- the IMDCT scratch (mix, IMDCT), the window's carried sums
    // (window) and the PCM pairs (written by the window's outputs, read by the emit):
    //   0..63    sc  {region, i}   the IMDCT scratch, 24 bits
    //   64..127  b2  {side, i}     the window's carried partial sums, 29 bits
    //   128..191 pcm {side, i}     the 32 PCM pairs, 16 bits (L then R)
    (* ramstyle = "M10K" *) logic [28:0] sm [0:255];
    initial begin                    // as the emulator: every buffer starts at zero
        for (int n = 0; n < 2048; n++) xb[n] = 25'd0;
        for (int n = 0; n < 1024; n++) begin hb[n] = 24'd0; ring[n] = 24'd0; end
        for (int n = 0; n < 256; n++) sm[n] = 29'd0;
    end

    logic [10:0] xb_ra;  logic [24:0] xb_q;
    logic  [9:0] hb_ra;  logic [23:0] hb_q;
    logic  [9:0] rg_ra;  logic [23:0] rg_q;
    logic  [5:0] b2_ra, sc_ra;
    logic  [7:0] sm_ra;  logic [28:0] sm_q;
    logic  [4:0] pb_ra;
    logic        pb_r;                                    // the emit reads R (else L)
    wire  [28:0] b2_q = sm_q;
    wire  [23:0] sc_q = sm_q[23:0];
    logic [15:0] pl_hold;                                 // the emit's L, while R is read
    logic  [8:0] vk_ra;  logic [23:0] vk_q;
    logic  [9:0] wn_ra;  logic [23:0] wn_q;
    logic  [9:0] ip_ra;  logic [19:0] ip_q;
    logic  [6:0] ic_ra;  logic [26:0] ic_q;

    // stage-Y write controls (the issue-side controls, registered)
    logic        x_xb_we, x_hb_we, x_rg_we, x_b2_we, x_sc_we, x_p_we, x_mag_en;
    logic [10:0] x_xb_wa;
    logic  [9:0] x_hb_wa, x_rg_wa;
    logic  [5:0] x_b2_wa, x_sc_wa, x_p_wa;
    logic signed [55:0] res;
    // the PCM stage: res registered, then rounded to s16 and saturated
    logic        p_we;
    logic  [5:0] p_wa;                                    // {side, idx}
    logic signed [23:0] p_val;
    wire  signed [24:0] p_rnd = ($signed({p_val[23], p_val}) + 25'sd128) >>> 8;
    wire  signed [15:0] p_s16 = (p_rnd > 25'sd32767) ? 16'sh7FFF :
                                (p_rnd < -25'sd32768) ? 16'sh8000 : p_rnd[15:0];

    always_ff @(posedge clk) begin
        xb_q <= xb[xb_ra];    if (x_xb_we) xb[x_xb_wa] <= res[24:0];
        hb_q <= hb[hb_ra];    if (x_hb_we) hb[x_hb_wa] <= res[23:0];
        rg_q <= ring[rg_ra];  if (x_rg_we) ring[x_rg_wa] <= res[23:0];
        sm_q <= sm[sm_ra];
        if (x_sc_we)      sm[{2'b00, x_sc_wa}] <= res[28:0];
        else if (x_b2_we) sm[{2'b01, x_b2_wa}] <= res[28:0];
        else if (p_we)    sm[{2'b10, p_wa}]    <= {13'd0, p_s16};
        vk_q <= vk[vk_ra];
        wn_q <= win[wn_ra];
        ip_q <= iprog[ip_ra];
        ic_q <= icoef[ic_ra];
    end

    // ------------------------------------------------------------------ state
    typedef enum logic [4:0] {
        V_IDLE, V_LOOP, V_DRAIN, V_DONE,
        V_XQ0, V_XQ1, V_XQ2, V_XQ2W, V_XQ3, V_XQ4, V_XQ4W, V_XQ5, V_XQ6, V_XQ6W, V_XQ7,
        V_VQ0, V_VQ1, V_JO0, V_JO1,
        V_AD0, V_AD1, V_AD2, V_AD3,
        V_BF,
        V_MX, V_IPS, V_IP, V_WIN, V_WINW, V_EMIT, V_EMITL, V_EMITW
    } vstate_t;
    vstate_t st;
    logic  [5:0] vop;
    logic [15:0] a0, a1, a2, a3, a4, a5, a6, a7;
    logic [11:0] k, k_d, lcnt;
    logic        dv;
    logic [23:0] sreg;                       // a scale (XQ, XVQ), a joint scale
    logic [46:0] ss;                         // XQ: step x scale
    logic [23:0] ss2;                        // ... >> shift (<= 2^23)
    logic  [4:0] shreg, qsh;
    logic [63:0] cb_row;
    logic        cb_got;
    logic [23:0] h0, h1, h2, h3;             // ADPCM history, h0 the oldest
    logic  [2:0] aj, as;                     // ADPCM sample, issue step (0..4)
    logic [24:0] bf_a, bf_c;                 // BFLY: X[p], X[q] of the element
    logic        side;
    logic  [8:0] off0, off1;                 // the rings' offsets (FFmpeg's synth offset)
    logic [28:0] mag;                        // the mixed column's sum of |v|
    logic        pshift;                     // the IMDCT pre-shift is 2 (else 0)
    logic  [2:0] nch;
    logic  [4:0] mb_b, mb_bd;                // mix: band read, band issued
    logic  [2:0] mb_c, mb_cd;                // mix: channel read, channel issued
    logic        mx_go;
    logic  [9:0] ip_d;                       // IMDCT: the term in ip_q (the read stage)
    logic        ip_dv;
    logic [19:0] ip_i;                       // the term being issued
    logic        ip_iv, ip_bub;
    logic  [7:0] outn;                       // outputs issued: {stage, index}
    logic        fresh;                      // the next MAC term starts a sum
    logic signed [55:0] ahold;               // the output's addend
    logic  [8:0] w, w_d;                     // window element {q, i, tap}
    logic        w_go;

    wire  [8:0] off = side ? off1 : off0;

    // sign extensions (named, no casts)
    wire signed [26:0] xq27   = {{3{xq_code[23]}}, xq_code};
    wire signed [26:0] xb27   = {{2{xb_q[24]}}, xb_q};
    wire signed [26:0] sc27   = {{3{sc_q[23]}}, sc_q};
    wire signed [26:0] rg27   = {{3{rg_q[23]}}, rg_q};
    wire signed [26:0] wn27   = {{3{wn_q[23]}}, wn_q};
    wire signed [26:0] vk27   = {3'd0, vk_q};
    wire signed [26:0] sreg27 = {3'd0, sreg};
    wire signed [26:0] ss27   = {3'd0, ss2};
    wire signed [26:0] hs0 = {{3{h0[23]}}, h0}, hs1 = {{3{h1[23]}}, h1};
    wire signed [26:0] hs2 = {{3{h2[23]}}, h2}, hs3 = {{3{h3[23]}}, h3};
    wire signed [26:0] cs0 = {{11{cb_row[15]}}, cb_row[15:0]};
    wire signed [26:0] cs1 = {{11{cb_row[31]}}, cb_row[31:16]};
    wire signed [26:0] cs2 = {{11{cb_row[47]}}, cb_row[47:32]};
    wire signed [26:0] cs3 = {{11{cb_row[63]}}, cb_row[63:48]};
    logic [7:0] vq_b;
    always_comb vq_b = cb_row[{k_d[2:0], 3'd0} +: 8];
    wire signed [26:0] vq27 = {{19{vq_b[7]}}, vq_b};

    // XQ: Huffman-coded bands take the adjusted scale (the sequencer's own rule)
    wire  [3:0] q_abm1  = a2[3:0] - 4'd1;
    wire  [3:0] q_gsize = XQ_GSIZE[4 * q_abm1 +: 4];
    wire        q_huff  = (a2[4:0] <= 5'd10) && ({1'b0, a3[2:0]} < q_gsize);
    wire  [8:0] q_sidx  = a4[8] ? (VK_SCALE7 + {2'd0, a4[6:0]}) : (VK_SCALE6 + {3'd0, a4[5:0]});
    // XQ: shift = ss > 2^23 ? the bit length of ss >> 23 : 0
    logic [4:0] ss_len;
    always_comb begin
        ss_len = 5'd0;
        for (int b = 0; b < 24; b++) if (ss[23 + b]) ss_len = b + 1;
    end
    wire ss_gt = (ss[46:23] > 24'd1) || (ss[46:23] == 24'd1 && ss[22:0] != 23'd0);

    // the IMDCT term being issued, and its output's place
    wire        ti_last = ip_i[14];
    wire        ti_add  = ip_i[15];
    wire  [1:0] ti_rsh  = ip_i[17:16];
    wire        ti_neg  = ip_i[18];
    wire        ti_shl  = ip_i[19];
    wire  [2:0] o_stage = outn[7:5];
    wire  [4:0] o_idx   = outn[4:0];
    wire        ip_final = ip_iv && ti_last && (o_idx == 5'd31) && (o_stage != 3'd6);

    // the window: element {q, i, tap}; ring index (q*16 +- i) + 64 tap + offset
    wire  [1:0] w_q  = w[8:7];
    wire  [3:0] w_i  = w[6:3];
    wire  [2:0] w_t  = w[2:0];
    wire  [4:0] w_k  = {w_q[1], w_q[0] ? ~w_i : w_i};
    wire  [8:0] w_rg = off + {w_t, 6'd0} + {4'd0, w_k};
    wire  [1:0] wd_q = w_d[8:7];
    wire  [3:0] wd_i = w_d[6:3];
    wire  [2:0] wd_t = w_d[2:0];

    // the mix: gain index AMODE*10 + ch*2 + side
    wire  [8:0] mx_g = VK_GAIN + {2'd0, a1[3:0], 3'd0} + {4'd0, a1[3:0], 1'b0} +
                       {5'd0, mb_c, 1'b0} + {8'd0, side};

    // ------------------------------------------------------------------ the datapath
    logic signed [26:0] ma, mb;
    logic               acc_clr, acc_en, direct, trunc, preclip, neg, sat24, mag_en;
    logic         [1:0] shl;
    logic signed [55:0] init, addend, dsrc;
    logic         [5:0] rsh;
    logic               xb_we, hb_we, rg_we, b2_we, sc_we, p_wi;
    logic        [10:0] xb_wa;
    logic         [9:0] hb_wa, rg_wa;
    logic         [5:0] b2_wa, sc_wa, p_wai;
    logic signed [53:0] x_prod;
    logic               x_acc_clr, x_acc_en, x_direct, x_trunc, x_preclip, x_neg, x_sat24;
    logic         [1:0] x_shl;
    logic signed [55:0] x_init, x_addend, x_dsrc, acc;
    logic         [5:0] x_rsh;
    logic signed [55:0] sum, rsrc, rnd, pcl, pn, post, shv;
    always_comb begin
        sum  = (x_acc_clr ? x_init : acc) + {{2{x_prod[53]}}, x_prod};
        rsrc = x_direct ? x_dsrc : sum;
        rnd  = (x_rsh == 6'd0) ? rsrc
             : ((rsrc + (x_trunc ? 56'sd0 : (56'sd1 <<< (x_rsh - 6'd1)))) >>> x_rsh);
        pcl  = rnd;
        if (x_preclip) begin
            if (rnd > 56'sd8388607) pcl = 56'sd8388607;
            else if (rnd < -56'sd8388608) pcl = -56'sd8388608;
        end
        pn   = x_neg ? -pcl : pcl;
        post = pn + x_addend;
        shv  = post <<< x_shl;
        res  = shv;
        if (x_sat24) begin
            if (shv > 56'sd8388607) res = 56'sd8388607;
            else if (shv < -56'sd8388608) res = -56'sd8388608;
        end
    end
    wire [23:0] res_abs = res[23] ? (24'd0 - res[23:0]) : res[23:0];

    // synthesis translate_off
    always @(posedge clk)
        if ((x_sc_we || x_b2_we) && p_we)
            $fatal(1, "dts_vec: the small-buffer M10K took two writes in one cycle");
    // synthesis translate_on

    always_ff @(posedge clk) begin
        x_prod <= ma * mb;
        x_acc_clr <= acc_clr; x_acc_en <= acc_en; x_direct <= direct; x_trunc <= trunc;
        x_preclip <= preclip; x_neg <= neg; x_sat24 <= sat24; x_shl <= shl;
        x_init <= init; x_addend <= addend; x_dsrc <= dsrc; x_rsh <= rsh;
        x_xb_we <= xb_we; x_hb_we <= hb_we; x_rg_we <= rg_we; x_b2_we <= b2_we;
        x_sc_we <= sc_we; x_p_we <= p_wi; x_mag_en <= mag_en;
        x_xb_wa <= xb_wa; x_hb_wa <= hb_wa; x_rg_wa <= rg_wa; x_b2_wa <= b2_wa;
        x_sc_wa <= sc_wa; x_p_wa <= p_wai;
        p_we <= x_p_we; p_wa <= x_p_wa; p_val <= res[23:0];
        if (x_acc_en) acc <= sum;
        if (!rst_n) begin
            x_acc_en <= 1'b0; x_xb_we <= 1'b0; x_hb_we <= 1'b0; x_rg_we <= 1'b0;
            x_b2_we <= 1'b0; x_sc_we <= 1'b0; x_p_we <= 1'b0; x_mag_en <= 1'b0; p_we <= 1'b0;
        end
    end

    // ------------------------------------------------------------------ per-state wiring
    always_comb begin
        ma = 27'sd0; mb = 27'sd0; acc_clr = 1'b1; acc_en = 1'b0; direct = 1'b0;
        trunc = 1'b0; preclip = 1'b0; neg = 1'b0; sat24 = 1'b0; shl = 2'd0; mag_en = 1'b0;
        init = 56'sd0; addend = 56'sd0; dsrc = 56'sd0; rsh = 6'd0;
        xb_we = 1'b0; hb_we = 1'b0; rg_we = 1'b0; b2_we = 1'b0; sc_we = 1'b0; p_wi = 1'b0;
        xb_wa = {a0[2:0], a1[4:0], k_d[2:0]};
        hb_wa = {a0[2:0], a1[4:0], k_d[1:0]};
        rg_wa = {side, off + {4'd0, o_idx}};
        b2_wa = {side, wd_q[0], wd_i};
        sc_wa = {!o_stage[0], o_idx};
        p_wai = {side, wd_q[0], wd_i};
        xb_ra = {a0[2:0], a1[4:0], k[2:0]};
        hb_ra = {a0[2:0], a1[4:0], k[1:0]};
        rg_ra = {side, w_rg};
        b2_ra = {side, w_q[0], w_i};
        sc_ra = ip_q[5:0];
        pb_ra = k[4:0];
        // the shared small-buffer RAM's one read: the scratch (IMDCT), the carried
        // sums (window) or a PCM word (emit)
        pb_r = (st == V_EMITL) || (st == V_EMITW);
        sm_ra = (st == V_WIN) ? {2'b01, b2_ra} :
                (st == V_EMIT || st == V_EMITL || st == V_EMITW) ? {2'b10, pb_r, pb_ra} :
                {2'b00, sc_ra};
        vk_ra = q_sidx;
        wn_ra = {a7[0], w_t, w_q, w_i};
        ip_ra = (st == V_IPS) ? 10'd0 : (ip_bub || ip_final) ? ip_d : ip_d + 10'd1;
        ic_ra = ip_q[12:6];
        xq_ready = (st == V_XQ7);
        case (st)
            // ---- the plain loops: XCLR, HCLR (zero writes), XVQ, JOINT (8 samples)
            V_LOOP: begin
                if (vop == OP_JOINT) xb_ra = {a2[2:0], a1[4:0], k[2:0]};   // the source
                if (dv) case (vop)
                    OP_XCLR: begin direct = 1'b1; xb_we = 1'b1; xb_wa = k_d[10:0]; end
                    OP_HCLR: begin direct = 1'b1; hb_we = 1'b1; hb_wa = {a0[2:0], k_d[6:0]}; end
                    OP_XVQ: begin                       // clip23((v x scale + 8) >> 4)
                        ma = vq27; mb = sreg27; rsh = 6'd4; sat24 = 1'b1; xb_we = 1'b1;
                    end
                    OP_JOINT: begin                     // clip23(norm(x x scale, 17)), and history
                        ma = xb27; mb = sreg27; rsh = 6'd17; sat24 = 1'b1; xb_we = 1'b1;
                        hb_we = k_d[2];
                    end
                    default: ;
                endcase
            end
            // ---- XQ
            V_JO0: vk_ra = VK_JOINT + {1'b0, a3[7:0]};               // the joint scale
            V_XQ1: vk_ra = VK_ADJ + {7'd0, a5[1:0]};
            V_XQ2: begin                        // the Huffman scale: (adj x scale) >> 22, floor
                ma = sreg27; mb = vk27; rsh = 6'd22; trunc = 1'b1; sat24 = 1'b1;
            end
            V_XQ3: vk_ra = VK_STEP + {3'd0, a6[0], a2[4:0]};
            V_XQ4: begin ma = sreg27; mb = vk27; end                 // step x scale
            V_XQ6: begin direct = 1'b1; dsrc = {9'd0, ss}; rsh = {1'b0, shreg}; trunc = 1'b1; end
            V_XQ7: if (xq_valid) begin          // clip23(norm(q x ss, 22 - shift))
                ma = xq27; mb = ss27; rsh = {1'b0, qsh}; sat24 = 1'b1;
                xb_we = 1'b1; xb_wa = {a0[2:0], a1[4:0], k[2:0]};
            end
            // ---- ADPCM
            V_AD1: xb_ra = {a0[2:0], a1[4:0], 1'b1, k[1:0]};       // unpredicted: x[4..7]
            V_AD2: begin
                xb_ra = {a0[2:0], a1[4:0], aj};
                acc_clr = (as == 3'd0); acc_en = (as <= 3'd3);
                case (as)
                    3'd0: begin ma = hs0; mb = cs3; end
                    3'd1: begin ma = hs1; mb = cs2; end
                    3'd2: begin ma = hs2; mb = cs1; end
                    3'd3: begin                 // clip23(x + clip23(norm(pred, 13)))
                        ma = hs3; mb = cs0; rsh = 6'd13; preclip = 1'b1; sat24 = 1'b1;
                        addend = {{31{xb_q[24]}}, xb_q};
                        xb_we = 1'b1; xb_wa = {a0[2:0], a1[4:0], aj};
                    end
                    default: ;
                endcase
            end
            V_AD3: begin                        // the history out
                direct = 1'b1; hb_we = 1'b1; hb_wa = {a0[2:0], a1[4:0], k[1:0]};
                case (k[1:0])
                    2'd0: dsrc = {{32{h0[23]}}, h0};
                    2'd1: dsrc = {{32{h1[23]}}, h1};
                    2'd2: dsrc = {{32{h2[23]}}, h2};
                    default: dsrc = {{32{h3[23]}}, h3};
                endcase
            end
            // ---- BFLY: an element in 4 cycles (read p, read q, write p + q, write p - q)
            V_BF: begin
                xb_ra = k[0] ? {a1[2:0], k[9:2]} : {a0[2:0], k[9:2]};
                direct = 1'b1;
                if (k[1:0] == 2'd2) begin
                    dsrc = {{31{bf_a[24]}}, bf_a} + {{31{xb_q[24]}}, xb_q};
                    xb_we = 1'b1; xb_wa = {a0[2:0], k[9:2]};
                end else if (k[1:0] == 2'd3) begin
                    dsrc = {{31{bf_a[24]}}, bf_a} - {{31{bf_c[24]}}, bf_c};
                    xb_we = 1'b1; xb_wa = {a1[2:0], k[9:2]};
                end
            end
            // ---- MIXSYN: the mix of column j, clip23(norm(sum x g, 15)), and sum |v|.
            //      Every band of every channel: the model's nmix bound (a2..a6) is not
            //      applied, because X above a channel's mixed count is zero by
            //      construction -- XCLR clears X every subsubframe, every op writes only
            //      below the channel's active count, and BFLY's sums stay below the
            //      pair's larger count, which is what nmix is. The bound saved the
            //      model's cycles only; without it the pair-bound bug (D3, mix_own_bound)
            //      cannot occur here.
            V_MX: begin
                xb_ra = {mb_c, mb_b, a0[2:0]};
                vk_ra = mx_g;
                if (dv) begin
                    ma = xb27; mb = vk27;
                    acc_clr = (mb_cd == 3'd0); acc_en = 1'b1;
                    if (mb_cd == nch - 3'd1) begin
                        rsh = 6'd15; sat24 = 1'b1; sc_we = 1'b1; sc_wa = {1'b0, mb_bd};
                        mag_en = 1'b1;
                    end
                end
            end
            // ---- MIXSYN: the IMDCT program
            V_IP: if (ip_iv) begin
                if (!ti_add) begin
                    ma = sc27; mb = ic_q; acc_clr = fresh; acc_en = 1'b1;
                end
                if (ti_last) begin
                    case (ti_rsh)
                        2'd0: rsh = 6'd0;
                        2'd1: rsh = pshift ? 6'd2 : 6'd0;
                        2'd2: rsh = 6'd22;
                        default: rsh = 6'd23;
                    endcase
                    neg = ti_neg; shl = (ti_shl && pshift) ? 2'd2 : 2'd0; sat24 = 1'b1;
                    addend = ahold;
                    if (o_stage == 3'd6) rg_we = 1'b1; else sc_we = 1'b1;
                end
            end
            // ---- MIXSYN: the window, 8 taps an output, q-major
            V_WIN: if (dv) begin
                ma = wn27; mb = rg27;
                acc_clr = (wd_t == 3'd0); acc_en = 1'b1;
                init = wd_q[1] ? 56'sd0 : ($signed({{27{b2_q[28]}}, b2_q}) <<< 21);
                if (wd_t == 3'd7) begin
                    rsh = 6'd21;
                    if (wd_q[1]) b2_we = 1'b1;                       // c, d: the carried sums
                    else begin sat24 = 1'b1; p_wi = 1'b1; end        // a, b: PCM
                end
            end
            default: ;
        endcase
    end

    // ------------------------------------------------------------------ sequencing
    always_ff @(posedge clk) begin
        done <= 1'b0;
        cb_req <= 1'b0;
        if (cb_valid) begin cb_row <= cb_data; cb_got <= 1'b1; end
        if (pcm_valid && pcm_ready) pcm_valid <= 1'b0;
        if (x_mag_en) mag <= mag + {5'd0, res_abs};
        if (!rst_n) begin
            st <= V_IDLE; pcm_valid <= 1'b0; cb_got <= 1'b0;
            off0 <= 9'd0; off1 <= 9'd0;
        end else case (st)
            V_IDLE: if (start) begin
                vop <= op;
                a0 <= args[15:0];   a1 <= args[31:16];  a2 <= args[47:32];
                a3 <= args[63:48];  a4 <= args[79:64];  a5 <= args[95:80];
                a6 <= args[111:96]; a7 <= args[127:112];
                k <= 12'd0; dv <= 1'b0;
                case (op)
                    OP_XCLR: begin lcnt <= 12'd1280; st <= V_LOOP; end
                    OP_HCLR: begin        // {band, k} from 4 x the band to 127
                        k <= args[31] ? 12'd0 : (args[31:16] >= 16'd32) ? 12'd128
                                                : {3'd0, args[22:16], 2'd0};
                        lcnt <= 12'd128; st <= V_LOOP;
                    end
                    OP_XQ: st <= V_XQ0;
                    OP_XVQ: begin
                        cb_req <= 1'b1; cb_sel <= 1'b1; cb_got <= 1'b0;
                        cb_addr <= {args[41:32], args[49:48]};
                        st <= V_VQ0;
                    end
                    OP_ADPCM: begin
                        aj <= 3'd0; as <= 3'd0;
                        if (args[48]) begin
                            cb_req <= 1'b1; cb_sel <= 1'b0; cb_got <= 1'b0;
                            cb_addr <= args[43:32];
                            st <= V_AD0;
                        end else st <= V_AD1;
                    end
                    OP_JOINT: st <= V_JO0;
                    OP_BFLY: st <= V_BF;
                    OP_MIXSYN: begin
                        side <= 1'b0; mb_b <= 5'd0; mb_c <= 3'd0; mx_go <= 1'b1; mag <= 29'd0;
                        nch <= VK_NCH[3 * args[19:16] +: 3];
                        st <= V_MX;
                    end
                    default: st <= V_DONE;          // CNT
                endcase
            end
            // ---- a plain loop: k issues reads, k_d the data's use
            V_LOOP: begin
                dv <= (k < lcnt);
                k_d <= k;
                if (k < lcnt) k <= k + 12'd1;
                else if (!dv) st <= V_DONE;
            end
            V_DRAIN: st <= V_DONE;                  // the last stage-Y write lands
            V_DONE: if (!pcm_valid) begin done <= 1'b1; st <= V_IDLE; end
            // ---- XQ
            V_XQ0: st <= V_XQ1;                     // the scale is read
            V_XQ1: begin sreg <= vk_q; if (q_huff) st <= V_XQ2; else st <= V_XQ3; end
            V_XQ2: st <= V_XQ2W;
            V_XQ2W: begin sreg <= res[23:0]; st <= V_XQ3; end
            V_XQ3: st <= V_XQ4;                     // the step is read
            V_XQ4: st <= V_XQ4W;
            V_XQ4W: begin ss <= res[46:0]; st <= V_XQ5; end
            V_XQ5: begin shreg <= ss_gt ? ss_len : 5'd0; st <= V_XQ6; end
            V_XQ6: begin qsh <= (shreg >= 5'd22) ? 5'd0 : 5'd22 - shreg; st <= V_XQ6W; end
            V_XQ6W: begin ss2 <= res[23:0]; st <= V_XQ7; end
            V_XQ7: if (xq_valid) begin
                k <= k + 12'd1;
                if (k[2:0] == 3'd7) st <= V_DRAIN;
            end
            // ---- XVQ, JOINT: the scale, then the plain loop
            V_VQ0: st <= V_VQ1;
            V_VQ1: if (cb_got) begin sreg <= vk_q; lcnt <= 12'd8; st <= V_LOOP; end
            V_JO0: st <= V_JO1;
            V_JO1: begin sreg <= vk_q; lcnt <= 12'd8; st <= V_LOOP; end
            // ---- ADPCM: predicted -- the history in, 8 samples, the history out
            V_AD0: begin
                dv <= (k < 12'd4);
                k_d <= k;
                if (k < 12'd4) k <= k + 12'd1;
                if (dv) case (k_d[1:0])
                    2'd0: h0 <= hb_q;  2'd1: h1 <= hb_q;  2'd2: h2 <= hb_q;  default: h3 <= hb_q;
                endcase
                if (k == 12'd4 && !dv && cb_got) st <= V_AD2;
            end
            // ---- ADPCM: unpredicted -- the history is x[4..7]
            V_AD1: begin
                dv <= (k < 12'd4);
                k_d <= k;
                if (k < 12'd4) k <= k + 12'd1;
                if (dv) case (k_d[1:0])
                    2'd0: h0 <= xb_q[23:0];  2'd1: h1 <= xb_q[23:0];
                    2'd2: h2 <= xb_q[23:0];  default: h3 <= xb_q[23:0];
                endcase
                if (k == 12'd4 && !dv) begin k <= 12'd0; st <= V_AD3; end
            end
            V_AD2: begin                            // 4 terms, then the result lands (as 4)
                if (as == 3'd4) begin
                    as <= 3'd0;
                    h0 <= h1; h1 <= h2; h2 <= h3; h3 <= res[23:0];
                    aj <= aj + 3'd1;
                    if (aj == 3'd7) begin k <= 12'd0; st <= V_AD3; end
                end else as <= as + 3'd1;
            end
            V_AD3: begin
                k <= k + 12'd1;
                if (k[1:0] == 2'd3) st <= V_DRAIN;
            end
            // ---- BFLY
            V_BF: begin
                if (k[1:0] == 2'd1) bf_a <= xb_q;   // X[p], read in phase 0
                if (k[1:0] == 2'd2) bf_c <= xb_q;   // X[q], read in phase 1
                k <= k + 12'd1;
                if (k == 12'd1023) st <= V_DRAIN;
            end
            // ---- MIXSYN: mix one side's column
            V_MX: begin
                dv <= mx_go;
                mb_bd <= mb_b; mb_cd <= mb_c;
                if (mx_go) begin
                    if (mb_c == nch - 3'd1) begin
                        mb_c <= 3'd0;
                        if (mb_b == 5'd31) mx_go <= 1'b0;
                        mb_b <= mb_b + 5'd1;
                    end else mb_c <= mb_c + 3'd1;
                end else if (!dv) st <= V_IPS;      // the last write and |v| land now
            end
            V_IPS: begin                            // term 0 is read; the pipeline starts
                pshift <= (mag > 29'h400000);
                ip_d <= 10'd0; ip_dv <= 1'b1; ip_iv <= 1'b0; ip_bub <= 1'b0;
                outn <= 8'd0; fresh <= 1'b1; ahold <= 56'sd0;
                st <= V_IP;
            end
            V_IP: begin
                if (ip_iv) begin
                    if (ti_add) ahold <= {{32{sc_q[23]}}, sc_q};
                    else fresh <= 1'b0;
                    if (ti_last) begin outn <= outn + 8'd1; ahold <= 56'sd0; fresh <= 1'b1; end
                end
                if (ip_bub || ip_final) begin        // a stage boundary: hold the read stage
                    ip_iv <= 1'b0;
                    ip_bub <= ip_final;
                end else begin
                    ip_i <= ip_q; ip_iv <= ip_dv;
                    ip_d <= ip_d + 10'd1;
                    ip_dv <= (ip_d + 10'd1 < IPROG_WORDS);
                end
                if (outn == 8'd224) begin           // the last ring write lands now
                    w <= 9'd0; w_go <= 1'b1; dv <= 1'b0;
                    st <= V_WIN;
                end
            end
            V_WIN: begin
                dv <= w_go;
                w_d <= w;
                if (w_go) begin
                    w <= w + 9'd1;
                    if (w == 9'd511) w_go <= 1'b0;
                end else if (!dv) begin k <= 12'd0; st <= V_WINW; end
            end
            V_WINW: begin                           // the last PCM word lands
                k <= k + 12'd1;
                if (k == 12'd1) begin
                    if (!side) begin
                        off0 <= off0 - 9'd32;
                        side <= 1'b1; mb_b <= 5'd0; mb_c <= 3'd0; mx_go <= 1'b1; mag <= 29'd0;
                        dv <= 1'b0;
                        st <= V_MX;
                    end else begin
                        off1 <= off1 - 9'd32;
                        k <= 12'd0;
                        st <= V_EMIT;
                    end
                end
            end
            V_EMIT: st <= V_EMITL;                  // pair k's L is read
            V_EMITL: begin pl_hold <= sm_q[15:0]; st <= V_EMITW; end   // ... then its R
            V_EMITW: if (!pcm_valid || pcm_ready) begin
                pcm_l <= pl_hold; pcm_r <= sm_q[15:0]; pcm_valid <= 1'b1;
                k <= k + 12'd1;
                if (k == 12'd31) st <= V_DONE; else st <= V_EMIT;
            end
            default: st <= V_IDLE;
        endcase
    end

endmodule
