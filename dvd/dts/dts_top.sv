// dvd/dts/dts_top.sv -- the DTS core decoder: frames in, s16 stereo PCM out
//
// dts_seq runs the microcode (the frame header, the side information, the control of
// every subsubframe); dts_vec the hardwired loops on one multiplier. A frame is a
// descriptor (fr_len, its byte length) and then exactly that many bytes, as
// dvd_audio_decode's dispatcher routes a frame from audio_ring. PCM pairs leave
// through pcm_* with full back-pressure (a MIXSYN stalls on its pairs). The codebook
// port (cb_*: one 64-bit row a request, answered any number of cycles later) is a
// top-level port: the codebooks are off-chip (docs/dts_decoder.md D4), answered by
// the bench in P1b and by ram2 from P2. clk_sys, 27 MHz. docs/dts_decoder.md sec 10.
//
// Telemetry (16-bit counters, saturating): frames decoded; frames refused, with the
// last refusal's code (dts.uasm E_*) and a sticky mask of every code seen; bits read
// past a frame's end (as 0); overflowed block codes decoded leniently (D5); frames
// whose embedded downmix coefficients were ignored (D3).

`default_nettype none

module dts_top (
    input  wire          clk,
    input  wire          rst_n,

    input  wire   [15:0] fr_len,
    input  wire          fr_valid,
    output logic         fr_ready,
    input  wire    [7:0] in_byte,
    input  wire          in_valid,
    output logic         in_ready,

    output logic         cb_req,
    output logic         cb_sel,
    output logic  [11:0] cb_addr,
    input  wire          cb_valid,
    input  wire   [63:0] cb_data,

    output logic  [15:0] pcm_l,
    output logic  [15:0] pcm_r,
    output logic         pcm_valid,
    input  wire          pcm_ready,

    output logic  [15:0] frames,
    output logic  [15:0] refused,
    output logic   [4:0] last_err,
    output logic  [31:0] err_seen,
    output logic  [15:0] overrun_bits,
    output logic  [15:0] lenient_codes,
    output logic  [15:0] dmix_ignored,

    // benches
    output logic         vop_start,
    output logic   [3:0] vop_op,
    output logic         vop_done,
    output logic         tr_valid,
    output logic   [9:0] tr_pc,
    output logic   [1:0] tr_kind,
    output logic  [10:0] tr_addr,
    output logic  [23:0] tr_val
);

    logic [127:0] vop_args;
    logic  [23:0] xq_code;
    logic         xq_valid, xq_ready;
    logic         err_valid, frame_done, overrun_bit, lenient;
    logic   [4:0] err_code;

    dts_seq u_seq (
        .clk, .rst_n, .fr_len, .fr_valid, .fr_ready, .in_byte, .in_valid, .in_ready,
        .vop_start, .vop_op, .vop_args, .vop_done, .xq_code, .xq_valid, .xq_ready,
        .err_valid, .err_code, .frame_done, .overrun_bit, .lenient,
        .tr_valid, .tr_pc, .tr_kind, .tr_addr, .tr_val);

    dts_vec u_vec (
        .clk, .rst_n, .start(vop_start), .op(vop_op), .args(vop_args), .done(vop_done),
        .xq_code, .xq_valid, .xq_ready,
        .cb_req, .cb_sel, .cb_addr, .cb_valid, .cb_data,
        .pcm_l, .pcm_r, .pcm_valid, .pcm_ready);

    // CNT's counter id is r8 (dts_isa.CNT_DMIX_IGNORED = 0)
    wire cnt_dmix = vop_start && (vop_op == 4'd8) && (vop_args[15:0] == 16'd0);

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            frames <= 16'd0; refused <= 16'd0; last_err <= 5'd0; err_seen <= 32'd0;
            overrun_bits <= 16'd0; lenient_codes <= 16'd0; dmix_ignored <= 16'd0;
        end else begin
            if (frame_done && frames != 16'hFFFF) frames <= frames + 16'd1;
            if (err_valid) begin
                if (refused != 16'hFFFF) refused <= refused + 16'd1;
                last_err <= err_code;
                err_seen[err_code] <= 1'b1;
            end
            if (overrun_bit && overrun_bits != 16'hFFFF) overrun_bits <= overrun_bits + 16'd1;
            if (lenient && lenient_codes != 16'hFFFF) lenient_codes <= lenient_codes + 16'd1;
            if (cnt_dmix && dmix_ignored != 16'hFFFF) dmix_ignored <= dmix_ignored + 16'd1;
        end
    end

endmodule
