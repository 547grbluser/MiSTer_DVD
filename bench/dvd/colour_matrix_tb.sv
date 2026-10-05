/*
 * colour_matrix_tb.sv -- the colour-matrix gate (docs/status_log.md "BT.601 default
 * colour matrix"). Run it through bench/dvd/run_colour_matrix.sh.
 *
 * The REAL getbits_fifo + vld parse a header-only stream from tools/colour_matrix_es.py
 * (sequence / extension / GOP / picture headers, no slices). The vld's
 * matrix_coefficients drives five REAL yuv2rgb instances, each converting one fixed
 * test colour, and a REAL pgc_palette converts four of the same colours the way
 * subtitles are converted. Every coded picture has an expectation word (see the
 * generator's docstring); the bench scores each arm separately and prints one
 * "ARM n:" line per arm, so a mutation can be scored on WHICH arms it breaks.
 *
 *   [1] MPEG-2 with no sequence_display_extension decodes BT.601 (RGB scored against
 *       the textbook matrix, not against yuv2rgb's own table).
 *   [2] colour_description=0, or no display extension, straight after a 709 sequence
 *       commits 0: the previous sequence's matrix is NOT inherited.
 *   [3] an explicit tag is honoured: 6 decodes 601, 1 decodes 709 (RGB scored).
 *   [4] an MPEG-1 sequence after a 709 MPEG-2 one commits 0.
 *   [5] repeated sequence headers carrying the same tag: matrix_coefficients stays
 *       === the tag on EVERY cycle between pictures (no clear-at-header streak).
 *   [6] subtitles and untagged video agree: pgc_palette's RGB is within 1 code of
 *       yuv2rgb's at matrix 0 (1 = the measured worst case over the whole legal
 *       range: pgc_palette truncates a x256 product, yuv2rgb rounds a x32768 one).
 *
 * Arms 2 and 4 score the matrix VALUE only, and arms 1/3/6 score RGB, so a yuv2rgb
 * table mutation and a vld inheritance mutation fail disjoint arms.
 * Comparisons use !== so an X can never pass (CLAUDE.md "Cross-cutting lessons").
 *
 *   +ES=<prefix>   reads <prefix>_es.hex and <prefix>_exp.hex
 */
`timescale 1ns/1ps

module colour_matrix_tb;

  reg clk = 0; always #5 clk = ~clk;
  reg rst = 0;

  // ---- ES feed (vld_mpeg1_tb's behavioural vbuf-read fifo) ----
  localparam MAXW = 4096;
  reg [63:0] es [0:MAXW-1];
  integer    es_words = 0;
  integer    rd_ptr = 0;
  localparam MAXP = 64;
  reg [15:0] expw [0:MAXP-1];
  integer    n_exp = 0;

  wire        vid_in_rd_en;
  reg         vid_in_rd_valid = 0;
  reg  [63:0] vid_in = 64'h0;

  always @(posedge clk) begin
    vid_in_rd_valid <= 1'b0;
    if (rst && vid_in_rd_en && (rd_ptr < es_words)) begin
      vid_in          <= es[rd_ptr];
      vid_in_rd_valid <= 1'b1;
      rd_ptr          <= rd_ptr + 1;
    end
  end

  // ---- DUT: getbits_fifo + vld ----
  wire  [4:0] advance;
  wire        align, wait_state;
  wire [23:0] getbits;
  wire        signbit, getbits_valid, vld_en;
  wire  [7:0] matrix_coefficients;
  wire        vld_err;

  getbits_fifo getbits_fifo (
    .clk(clk), .clk_en(1'b1), .rst(rst),
    .vid_in(vid_in), .vid_in_rd_en(vid_in_rd_en), .vid_in_rd_valid(vid_in_rd_valid),
    .advance(advance), .align(align), .wait_state(wait_state),
    .rld_wr_almost_full(1'b0),
    .mvec_wr_almost_full(1'b0),
    .motcomp_busy(1'b0),
    .getbits(getbits), .signbit(signbit),
    .getbits_valid(getbits_valid), .vld_en(vld_en),
    .pos_clr(1'b0), .bitpos()
  );

  vld vld (
    .clk(clk), .clk_en(vld_en), .rst(rst),
    .getbits(getbits), .signbit(signbit),
    .advance(advance), .align(align), .wait_state(wait_state),
    .quant_wr_data(), .quant_wr_addr(), .quant_rst(),
    .wr_intra_quant(), .wr_non_intra_quant(),
    .wr_chroma_intra_quant(), .wr_chroma_non_intra_quant(),
    .rld_wr_en(), .rld_cmd(), .dct_coeff_run(), .dct_coeff_signed_level(),
    .dct_coeff_end(), .alternate_scan(), .q_scale_type(), .quantiser_scale_code(),
    .macroblock_intra(), .intra_dc_precision(), .matrix_coefficients(matrix_coefficients),
    .horizontal_size(), .vertical_size(),
    .display_horizontal_size(), .display_vertical_size(),
    .aspect_ratio_information(), .frame_rate_code(),
    .frame_rate_extension_n(), .frame_rate_extension_d(),
    .picture_coding_type(), .picture_structure(),
    .motion_type(), .dct_type(), .macroblock_address(),
    .macroblock_motion_forward(), .macroblock_motion_backward(),
    .mb_width(), .mb_height(),
    .motion_vert_field_select_0_0(), .motion_vert_field_select_0_1(),
    .motion_vert_field_select_1_0(), .motion_vert_field_select_1_1(),
    .second_field(), .update_picture_buffers(),
    .last_frame(), .chroma_format(), .motion_vector_valid(),
    .pmv_0_0_0(), .pmv_0_0_1(), .pmv_1_0_0(), .pmv_1_0_1(),
    .pmv_0_1_0(), .pmv_0_1_1(), .pmv_1_1_0(), .pmv_1_1_1(),
    .dmv_0_0(), .dmv_0_1(), .dmv_1_0(), .dmv_1_1(),
    .progressive_sequence(), .progressive_frame(),
    .top_field_first(), .repeat_first_field(),
    .vld_err(vld_err),
    .drop_pic_req(1'b0),
    .drop_pic_ack(), .drop_pic_rff(), .drop_pic_field(),
    .flags_commit(),
    .mpeg1(),
    .vbuf_flush(1'b0),
    .bitpos(32'd0), .pic_hdr_pulse(), .pic_hdr_bitpos(), .pic_hdr_upd(), .pic_hdr_second()
  );

  // ---- five yuv2rgb instances, one test colour each ----
  // 0: Y=Cb=Cr=0, the never-written framestore slot (docs/mgl_launch.md's green)
  // 1: skin  2: red  3: blue  4: orange   (1..4 are legal-range: also scored vs palette)
  localparam NC = 5;
  reg [7:0] ty [0:NC-1];
  reg [7:0] tu [0:NC-1];
  reg [7:0] tv [0:NC-1];
  initial begin
    ty[0] = 8'd0;   tu[0] = 8'd0;   tv[0] = 8'd0;
    ty[1] = 8'd150; tu[1] = 8'd110; tv[1] = 8'd160;
    ty[2] = 8'd81;  tu[2] = 8'd90;  tv[2] = 8'd240;
    ty[3] = 8'd41;  tu[3] = 8'd240; tv[3] = 8'd110;
    ty[4] = 8'd180; tu[4] = 8'd60;  tv[4] = 8'd200;
  end
  wire [7:0] yr [0:NC-1];
  wire [7:0] yg [0:NC-1];
  wire [7:0] yb [0:NC-1];

  genvar gi;
  generate for (gi = 0; gi < NC; gi = gi + 1) begin : y2r
    yuv2rgb cv (
      .clk(clk), .clk_en(1'b1), .rst(rst), .hard_rst(rst),
      .matrix_coefficients(matrix_coefficients),
      .y(ty[gi]), .u(tu[gi]), .v(tv[gi]),
      .h_sync_in(1'b0), .v_sync_in(1'b0), .pixel_en_in(1'b1),
      .r(yr[gi]), .g(yg[gi]), .b(yb[gi]),
      .y_out(), .u_out(), .v_out(),
      .h_sync_out(), .v_sync_out(), .c_sync_out(), .pixel_en_out()
    );
  end endgenerate

  // ---- pgc_palette: the subtitle path, entries 1..4 = test colours 1..4 ----
  reg        pal_we = 1'b0;
  reg  [3:0] pal_waddr = 4'd0;
  reg [31:0] pal_wdata = 32'd0;
  reg  [3:0] pal_idx = 4'd0;
  wire [7:0] pr, pg, pb;
  pgc_palette pal (
    .clk(clk), .rst_n(rst),
    .pal_we(pal_we), .pal_waddr(pal_waddr), .pal_wdata(pal_wdata),
    .idx(pal_idx), .rgb_r(pr), .rgb_g(pg), .rgb_b(pb)
  );

  // ---- textbook YCbCr -> RGB (studio swing), the independent reference ----
  function automatic integer clip_round(input real x);
    integer i;
    begin
      i = $rtoi(x + 0.5 + 1000.0) - 1000;   // round half up, also for negatives
      clip_round = (i < 0) ? 0 : (i > 255) ? 255 : i;
    end
  endfunction

  task automatic ref_rgb(input integer bt709, input integer c,
                         output integer r, output integer g, output integer b);
    real kr, kb, kg, yy, uu, vv;
    begin
      kr = bt709 ? 0.2126 : 0.299;
      kb = bt709 ? 0.0722 : 0.114;
      kg = 1.0 - kr - kb;
      yy = (255.0 / 219.0) * (ty[c] - 16.0);
      uu = (255.0 / 224.0) * (tu[c] - 128.0);
      vv = (255.0 / 224.0) * (tv[c] - 128.0);
      r = clip_round(yy + 2.0 * (1.0 - kr) * vv);
      g = clip_round(yy - 2.0 * (1.0 - kb) * kb / kg * uu - 2.0 * (1.0 - kr) * kr / kg * vv);
      b = clip_round(yy + 2.0 * (1.0 - kb) * uu);
    end
  endtask

  function automatic integer absd(input integer a, input integer b);
    absd = (a > b) ? a - b : b - a;
  endfunction

  // ---- per-arm scoring ----
  integer arm_checks [1:6];
  integer arm_fails  [1:6];
  integer k;
  initial for (k = 1; k <= 6; k = k + 1) begin arm_checks[k] = 0; arm_fails[k] = 0; end

  task automatic score(input integer arm, input integer ok, input string what);
    begin
      arm_checks[arm] = arm_checks[arm] + 1;
      if (!ok) begin
        arm_fails[arm] = arm_fails[arm] + 1;
        if (arm_fails[arm] <= 4) $display("   MISMATCH arm %0d: %0s", arm, what);
      end
    end
  endtask

  // ---- picture tracking ----
  localparam [7:0] STATE_PICTURE_HEADER = 8'h02;
  integer npic = 0;             // pictures seen so far
  integer since = -1;           // cycles since the latest picture's commit
  reg     in_pic_hdr = 1'b0;
  integer errs = 0;
  reg [15:0] cur;               // expectation word of the latest picture

  // The commit is registered on the START_CODE edge that also moves the FSM to
  // STATE_PICTURE_HEADER, so the first PICTURE_HEADER cycle already shows the new value.
  wire enter_pic = vld_en && (vld.state == STATE_PICTURE_HEADER) && !in_pic_hdr;

  always @(posedge clk) if (rst) begin
    if (vld_en) in_pic_hdr <= (vld.state == STATE_PICTURE_HEADER);
    if (vld_en && vld_err) errs = errs + 1;

    // [5] streak guard: from the previous commit up to this picture's, the output
    // must already equal the value both pictures carry, on every single cycle.
    if (npic < n_exp && expw[npic][11] && npic > 0)
      if (matrix_coefficients !== expw[npic][7:0] && !enter_pic)
        score(5, 0, $sformatf("cycle before picture %0d: matrix %0d, want %0d held",
                              npic, matrix_coefficients, expw[npic][7:0]));

    if (enter_pic) begin
      if (since >= 0 && since < 24)
        $fatal(1, "BENCH: picture %0d committed %0d cycles after the last -- the +16 sample window is too long",
               npic, since);
      if (npic >= n_exp)
        $fatal(1, "BENCH: more pictures (%0d) than expectations (%0d)", npic + 1, n_exp);
      cur = expw[npic];
      if (cur[11]) score(5, 1, "");          // count the held interval as a check
      npic = npic + 1;
      since = 0;
    end else if (since >= 0)
      since = since + 1;

    // Settled sample, 16 cycles after the commit (yuv2rgb is ~7 deep).
    if (since == 16) check_picture(npic - 1, cur);
  end

  task automatic check_picture(input integer p, input [15:0] e);
    integer arm, c, r, g, b, bt709;
    begin
      arm = e[15:12];
      // matrix VALUE, for every picture's own arm (an arm-5 picture's value is already
      // covered by the per-cycle hold)
      if (arm != 5)
        score(arm, matrix_coefficients === e[7:0],
              $sformatf("picture %0d: matrix_coefficients %0d, want %0d", p, matrix_coefficients, e[7:0]));
      // RGB against the textbook matrix (arms 1 and 3)
      if (e[10]) begin
        bt709 = (e[7:0] == 8'd1);
        for (c = 0; c < NC; c = c + 1) begin
          ref_rgb(bt709, c, r, g, b);
          score(arm, (yr[c] !== 8'bx) && (yg[c] !== 8'bx) && (yb[c] !== 8'bx) &&
                     absd(yr[c], r) <= 1 && absd(yg[c], g) <= 1 && absd(yb[c], b) <= 1,
                $sformatf("picture %0d (mc %0d) colour %0d: rgb %0d,%0d,%0d want %0d,%0d,%0d (%0s +-1)",
                          p, e[7:0], c, yr[c], yg[c], yb[c], r, g, b, bt709 ? "709" : "601"));
        end
      end
      // [6] subtitle palette vs untagged video, legal-range colours 1..4
      if (e[7:0] == 8'd0 && arm == 1)
        for (c = 1; c < NC; c = c + 1) begin
          pal_idx = c[3:0];
          #1;
          score(6, (pr !== 8'bx) && (pg !== 8'bx) && (pb !== 8'bx) &&
                   absd(pr, yr[c]) <= 1 && absd(pg, yg[c]) <= 1 && absd(pb, yb[c]) <= 1,
                $sformatf("colour %0d: palette %0d,%0d,%0d vs video(mc 0) %0d,%0d,%0d (+-1)",
                          c, pr, pg, pb, yr[c], yg[c], yb[c]));
        end
    end
  endtask

  task finish_run;
    integer a, bad;
    begin
      $display("SUMMARY: pictures %0d/%0d, vld_err cycles %0d", npic, n_exp, errs);
      bad = 0;
      if (npic != n_exp) begin
        $display("VACUOUS: only %0d of %0d pictures parsed", npic, n_exp);
        bad = 1;
      end
      for (a = 1; a <= 6; a = a + 1) begin
        if (arm_checks[a] == 0) begin
          $display("ARM %0d: VACUOUS (no checks)", a); bad = 1;
        end else if (arm_fails[a] != 0) begin
          $display("ARM %0d: FAIL (%0d/%0d checks)", a, arm_fails[a], arm_checks[a]); bad = 1;
        end else
          $display("ARM %0d: PASS (%0d checks)", a, arm_checks[a]);
      end
      if (errs != 0) begin $display("vld_err asserted on a header-only stream"); bad = 1; end
      if (bad) begin
        $display("RESULT: FAIL");
        $fatal(1, "colour_matrix_tb: FAIL");
      end
      $display("RESULT: PASS");
      $finish;
    end
  endtask

  // ---- drive ----
  string pfx;
  integer w;
  initial begin
    if (!$value$plusargs("ES=%s", pfx)) $fatal(1, "need +ES=<prefix>");
    for (w = 0; w < MAXW; w = w + 1) es[w] = 64'hx;
    for (w = 0; w < MAXP; w = w + 1) expw[w] = 16'hx;
    $readmemh({pfx, "_es.hex"}, es);
    $readmemh({pfx, "_exp.hex"}, expw);
    while (es_words < MAXW && es[es_words] !== 64'hx) es_words = es_words + 1;
    while (n_exp < MAXP && expw[n_exp] !== 16'hx) n_exp = n_exp + 1;
    if (es_words == 0 || n_exp == 0) $fatal(1, "empty fixture: %0d words, %0d pictures", es_words, n_exp);
    $display("ES: %0d words, %0d pictures expected", es_words, n_exp);

    rst = 0;
    repeat (8) @(posedge clk);
    rst = 1;
    // palette: write test colours 1..4 as {0, Y, Cr, Cb}
    for (w = 1; w < NC; w = w + 1) begin
      @(posedge clk);
      pal_we <= 1'b1; pal_waddr <= w[3:0]; pal_wdata <= {8'd0, ty[w], tv[w], tu[w]};
    end
    @(posedge clk) pal_we <= 1'b0;

    // run until the stream is consumed and the last picture has been sampled
    wait (rd_ptr >= es_words);
    wait (npic == n_exp && since > 24);
    repeat (200) @(posedge clk);
    finish_run;
  end

  initial begin
    #20_000_000;
    $display("TIMEOUT: pictures %0d/%0d rd_ptr %0d/%0d", npic, n_exp, rd_ptr, es_words);
    finish_run;
  end
endmodule
