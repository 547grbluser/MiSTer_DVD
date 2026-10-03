//============================================================================
//  audio_engine.sv -- the shared microcoded audio engine (dvd/dts/dts_top.sv) as the
//  AC-3 decoder's front end, feeding the unchanged imdct_512. docs/ac3_engine.md
//  "W1" (the in-core wiring).
//
//  It replaces ac3_front (sync_crc, bsi_parse, audblk_parse, exponent_decode,
//  bit_allocation, mantissa_dequant, bit_reader, bit_fifo) with the engine running
//  dvd/dts/ac3.uasm, whose coefficients, LFE slot and side information are bit-exact
//  against ac3_front's on every gate stream (bench/dvd/run_ac3.sh). Downstream
//  everything is as before: imdct_512 transforms each block, pcm_out drains it.
//
//  INPUT: one frame at a time -- a descriptor (fr_len: its byte length) and then
//  exactly that many bytes (dvd_audio_decode's dispatcher, one audio_ring frame;
//  ac3_reframer makes each one AC-3 syncframe). A frame the program refuses (no
//  sync, an out-of-scope field, a bad exponent or grouped code) is drained and the
//  engine waits for the next: no halt and no self-heal reset, unlike ac3_front.
//
//  THE IMDCT HANDSHAKE. The engine raises imdct_req with the block's coefficients in
//  its X buffer and waits; imdct_512 is started once the PREVIOUS block's pcm_mem
//  has been drained by pcm_out (pcm_done): the next transform overwrites pcm_mem.
//  The block's side information is LATCHED at that start (blksw, dynrng, acmod,
//  cmixlev, surmixlev) and held until the next start, because the engine runs ahead
//  -- it parses block k+1 while pcm_out still drains block k -- and imdct_512's
//  lvl_q, which pcm_out applies during the drain, is combinational in acmod and the
//  mix levels. (ac3_front never ran ahead: it waited for pcm_done before parsing on.)
//  imdct_512 reads the coefficients through the engine's X port, a registered read
//  like mantissa_dequant's coeff_mem; the engine holds X until imdct_done.
//
//  DTS (docs/dts_decoder.md P3): the same engine runs dvd/dts/dts.uasm when codec_req
//  is 0. A change of program waits until the engine is idle at FRAME (no frame in
//  flight, no IMDCT pending), then holds it in reset for a few cycles with the new
//  codec: codec_busy is high from the request until then, and the caller must not
//  present a descriptor meanwhile (the old program would take it). DTS's stereo PCM
//  leaves on dts_l/dts_r/dts_valid with dts_ready back-pressure (the engine's MIXSYN
//  waits); its codebook port (cb_*) is answered from DDR3 by dvd/dts/dts_cb_mem.sv.
//
//  rst is synchronous, active-high (as ac3_front's).
//============================================================================

`timescale 1ns/1ps
`default_nettype none

module audio_engine (
    input  wire         clk,
    input  wire         rst,
    input  wire         codec_req,           // 1 AC-3, 0 DTS: the program to run
    output logic        codec_busy,          // a change of program is pending: hold
                                             // the descriptor

    // one frame: a descriptor, then fr_len bytes
    input  wire  [15:0] fr_len,
    input  wire         fr_valid,
    output logic        fr_ready,
    input  wire   [7:0] in_byte,
    input  wire         in_valid,
    output logic        in_ready,

    // a block's PCM for pcm_out (as ac3_front's)
    output logic        imdct_done,          // pulse: pcm_mem holds a block
    input  wire  [10:0] pcm_rd_addr,         // {ch[2:0], idx[7:0]}
    output logic signed [31:0] pcm_rd_data,
    output logic [15:0] lvl_q,
    output logic  [2:0] pcm_acmod,           // the drained block's acmod (pcm_out's mono)
    input  wire         pcm_done,            // pcm_out has drained the block

    // DTS: stereo s16 pairs, and the codebook port
    output logic [15:0] dts_l,
    output logic [15:0] dts_r,
    output logic        dts_valid,
    input  wire         dts_ready,
    output logic        cb_req,
    output logic        cb_sel,
    output logic [11:0] cb_addr,
    input  wire         cb_valid,
    input  wire  [63:0] cb_data,

    // status
    output logic        synced,              // a frame decoded since reset
    output logic        frame_ok,            // pulse: a frame decoded to its end
    output logic        refused,             // pulse: a frame refused
    output logic  [4:0] err_code,            // that refusal's code (ac3.uasm E_*)
    output logic [15:0] n_frames,
    output logic [15:0] n_refused,
    output logic [31:0] err_seen,
    output logic [15:0] n_overrun
);

    // ---- the engine ----
    logic        e_rst_n;
    logic        imdct_req, e_imdct_done;
    logic [10:0] coef_ra;
    logic [24:0] coef_q;
    logic  [4:0] blk_blksw;
    logic  [7:0] blk_dynrng;
    logic  [2:0] blk_acmod;
    logic        blk_lfeon;
    logic  [1:0] blk_cmix, blk_surmix;
    logic [15:0] e_frames, e_refused, e_ovr;
    logic  [4:0] e_last_err;
    logic        vop_start, vop_done, e_fend, e_ref;
    logic  [4:0] e_rcode;
    logic  [5:0] vop_op;
    logic        im_busy;                    // this request's transform is running/done
    // the program: a change waits for the engine to idle at FRAME, then resets it
    logic        eng_codec;
    logic  [2:0] sw_cnt;
    logic        e_fr_ready;
    wire         idle_at_frame = e_fr_ready && !imdct_req && !im_busy;
    always_ff @(posedge clk) begin
        if (rst) begin
            eng_codec <= 1'b1; sw_cnt <= 3'd0;
        end else if (sw_cnt != 3'd0) sw_cnt <= sw_cnt - 3'd1;
        else if (codec_req != eng_codec && idle_at_frame) begin
            eng_codec <= codec_req; sw_cnt <= 3'd7;
        end
    end
    assign codec_busy = (codec_req != eng_codec) || (sw_cnt != 3'd0);
    always_comb e_rst_n = !rst && (sw_cnt == 3'd0);
    // a descriptor reaches the engine only when it runs the program it is for
    wire e_fr_valid = fr_valid && !codec_busy;
    assign fr_ready = e_fr_ready && !codec_busy;

    dts_top u_eng (
        .clk, .rst_n(e_rst_n), .codec({1'b0, eng_codec}),
        .fr_len, .fr_valid(e_fr_valid), .fr_ready(e_fr_ready), .in_byte, .in_valid, .in_ready,
        .cb_req, .cb_sel, .cb_addr, .cb_valid, .cb_data,
        .pcm_l(dts_l), .pcm_r(dts_r), .pcm_valid(dts_valid), .pcm_ready(dts_ready),
        .frames(e_frames), .refused(e_refused), .last_err(e_last_err), .err_seen(err_seen),
        .overrun_bits(e_ovr), .lenient_codes(), .dmix_ignored(),
        .frame_end(e_fend), .refuse(e_ref), .refuse_code(e_rcode),
        .imdct_req, .imdct_done(e_imdct_done), .coef_ra, .coef_q,
        .blk_blksw, .blk_dynrng, .blk_acmod, .blk_lfeon, .blk_cmix, .blk_surmix,
        .vop_start, .vop_op, .vop_done,
        .tr_valid(), .tr_pc(), .tr_kind(), .tr_addr(), .tr_val());

    assign n_frames  = e_frames;
    assign n_refused = e_refused;
    assign n_overrun = e_ovr;

    // ---- the IMDCT handshake ----
    logic        pcm_free;                   // pcm_mem drained (or never written)
    logic        im_go;
    logic  [4:0] i_blksw;
    logic  [7:0] i_dynrng;
    logic  [2:0] i_acmod;
    logic  [1:0] i_cmix, i_surmix;
    always_comb im_go = imdct_req && !im_busy && pcm_free;

    always_ff @(posedge clk) begin
        if (rst) begin
            pcm_free <= 1'b1;
            im_busy  <= 1'b0;
            i_blksw <= 5'd0; i_dynrng <= 8'd0; i_acmod <= 3'd2; i_cmix <= 2'd0; i_surmix <= 2'd0;
        end else begin
            if (im_go) begin
                im_busy  <= 1'b1;
                pcm_free <= 1'b0;
                i_blksw <= blk_blksw; i_dynrng <= blk_dynrng; i_acmod <= blk_acmod;
                i_cmix  <= blk_cmix;  i_surmix <= blk_surmix;
            end
            if (!imdct_req) im_busy <= 1'b0;     // the engine took done: next request
            if (pcm_done) pcm_free <= 1'b1;
        end
    end

    // nfchans by acmod (A/52 {2,1,2,3,3,4,4,5}; an inline ternary, as ac3_parse's)
    wire [2:0] i_nf = (i_acmod == 3'd1)                    ? 3'd1 :
                      (i_acmod == 3'd3 || i_acmod == 3'd4) ? 3'd3 :
                      (i_acmod == 3'd5 || i_acmod == 3'd6) ? 3'd4 :
                      (i_acmod == 3'd7)                    ? 3'd5 : 3'd2;

    logic [10:0] im_coeff_ra;
    imdct_512 u_imdct (
        .clk, .rst,
        .start(im_go),
        .nfchans(i_nf), .blksw(i_blksw), .dynrng(i_dynrng),
        .cmixlev(i_cmix), .surmixlev(i_surmix), .acmod(i_acmod),
        .coeff_rd_addr(im_coeff_ra), .coeff_rd_data(coef_q[23:0]),
        .pcm_rd_addr, .pcm_rd_data, .lvl_q,
        .done(imdct_done));
    assign coef_ra      = im_coeff_ra;
    assign e_imdct_done = imdct_done;
    assign pcm_acmod    = i_acmod;

    // ---- status ----
    always_ff @(posedge clk) begin
        if (rst) begin
            synced <= 1'b0; frame_ok <= 1'b0; refused <= 1'b0; err_code <= 5'd0;
        end else begin
            frame_ok <= e_fend;
            refused  <= e_ref;
            if (e_ref) err_code <= e_rcode;
            if (e_fend) synced <= 1'b1;
        end
    end

endmodule
