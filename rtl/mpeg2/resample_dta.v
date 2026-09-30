/* 
 * resample_dta.v
 * 
 * Copyright (c) 2007 Koen De Vleeschauwer. 
 * 
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND 
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE 
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE 
 * ARE DISCLAIMED. IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE 
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL 
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS 
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) 
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT 
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY 
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF 
 * SUCH DAMAGE.
 */

/*
 * resample_dta - chroma resampling: read pixel data from memory fifo.
 */

`include "timescale.v"

`undef DEBUG
//`define DEBUG 1

module resample_dta (
  clk, clk_en, rst, 
  fifo_read, fifo_valid,
  disp_rd_dta_empty, disp_rd_dta_en, disp_rd_dta_valid, disp_rd_dta,
  resample_rd_en, resample_rd_dta, resample_rd_valid,
  fifo_osd, fifo_y, fifo_u_upper, fifo_u_lower, fifo_v_upper, fifo_v_lower, fifo_position
  );

  input            clk;                      // clock
  input            clk_en;                   // clock enable
  input            rst;                      // synchronous active low reset

  input            fifo_read;                // resample_bilinear asserts 'fifo_read' when resample_bilineear clocks in fifo_* data
  output reg       fifo_valid;               // resample_dta asserts 'fifo_valid' when fifo_* valid

  /* chroma resampling: reading reconstructed frame data */
  input            disp_rd_dta_empty;
  output           disp_rd_dta_en;
  input            disp_rd_dta_valid;
  input      [63:0]disp_rd_dta;

  output           resample_rd_en;
  input       [9:0]resample_rd_dta;   // DVD-FORK FIX (F2): [2:0] position code, [9:3] chroma reuse flags
  input            resample_rd_valid;

  /* registers to read disp_rd_dta fifo in. 
     16 pixels - a macroblock - wide. 
     Two rows of chrominance information are stored, as we will have to interpolate between these two rows. */

  output reg [127:0]fifo_osd;          /* osd data */
  output reg [127:0]fifo_y;            /* lumi */
  output reg  [63:0]fifo_u_upper;      /* chromi, upper row */
  output reg  [63:0]fifo_u_lower;      /* chromi, lower row */
  output reg  [63:0]fifo_v_upper;      /* chromi, upper row */
  output reg  [63:0]fifo_v_lower;      /* chromi, lower row */
  output reg   [2:0]fifo_position;     /* position of pixels, as in  resample_codes */

  /* DVD-FORK FIX (F1, docs/decode_pacing.md §7): with OSD_READS = 0 resample_addrgen no
   * longer requests the OSD words (the OSD layer is tied off in this fork), so the OSD
   * read is skipped here in step and fifo_osd stays 0 -- which osd.v ignores while
   * osd_enable is 0. resample.v passes one parameter to both modules. */
  parameter OSD_READS = 0;
  /* DVD-FORK FIX (F2, docs/decode_pacing.md §7): chroma row reuse. resample.v passes the
   * same value to resample_addrgen; 0 keeps the F1 structure below unchanged. */
  parameter CHROMA_REUSE = 1;

`include "resample_codes.v"

  generate if (CHROMA_REUSE == 0) begin : g_orig

  /* first-word fall-through fifo readers */

  wire             disp_fwft_valid; 
  wire      [127:0]disp_fwft_dout; 
  reg              disp_fwft_rd_en;

  wire             resample_fwft_valid; 
  wire        [2:0]resample_fwft_dout; 
  reg              resample_fwft_rd_en;

  parameter [3:0]
    STATE_INIT        = 4'h0,
    STATE_RD_OSD      = 4'h1,
    STATE_RD_Y        = 4'h2,
    STATE_RD_U        = 4'h3,
    STATE_RD_V        = 4'h4,
    STATE_RD_POS      = 4'h5,
    STATE_READY       = 4'h6,
    STATE_WAIT        = 4'h7;
  
  reg          [3:0]state;
  reg          [3:0]next;

  localparam [3:0] FIRST_RD = OSD_READS ? STATE_RD_OSD : STATE_RD_Y;

  /* next state logic */
  always @*
    case (state)
      STATE_INIT:         next = FIRST_RD;   // DVD-FORK FIX (F1)

      STATE_RD_OSD:       if (disp_fwft_valid) next = STATE_RD_Y;
                          else next = STATE_RD_OSD;

      STATE_RD_Y:         if (disp_fwft_valid) next = STATE_RD_U;
                          else next = STATE_RD_Y;

      STATE_RD_U:         if (disp_fwft_valid) next = STATE_RD_V;
                          else next = STATE_RD_U;

      STATE_RD_V:         if (disp_fwft_valid) next = STATE_RD_POS;
                          else next = STATE_RD_V;

      STATE_RD_POS:       if (resample_fwft_valid) next = STATE_READY;
                          else next = STATE_RD_POS;

                          /* raise fifo_valid; wait for fifo_read to go high */
      STATE_READY:        if (fifo_read) next = STATE_WAIT;
                          else next = STATE_READY;

                          /* wait for fifo_read to drop, then lower fifo_valid */
      STATE_WAIT:         if (~fifo_read) next = FIRST_RD;   // DVD-FORK FIX (F1)
                          else next = STATE_WAIT;

      default             next = STATE_INIT;
    endcase

  /* state */
  always @(posedge clk)
    if(~rst) state <= STATE_INIT;
    else if (clk_en) state <= next;
    else state <= state;

  /* inform resample_bilinear data is valid */
  always @(posedge clk)
    if(~rst) fifo_valid <= 1'b0;
    else if (clk_en && (state == STATE_READY)) fifo_valid <= 1'b1;
    else if (clk_en && (state == STATE_WAIT)) fifo_valid <= fifo_read; // drop fifo_valid when fifo_read is lowered
    else if (clk_en) fifo_valid <= 1'b0;
    else fifo_valid <= fifo_valid;

  /* disp fifo read enable */
  always @(posedge clk)
    if(~rst) disp_fwft_rd_en <= 1'b0;
    else if (clk_en) disp_fwft_rd_en <= (next == STATE_RD_OSD) || (next == STATE_RD_Y) || (next == STATE_RD_U) ||(next == STATE_RD_V);
    else disp_fwft_rd_en <= disp_fwft_rd_en;

  /* resample fifo read enable */
  always @(posedge clk)
    if(~rst) resample_fwft_rd_en <= 1'b0;
    else if (clk_en) resample_fwft_rd_en <= (next == STATE_RD_POS);
    else resample_fwft_rd_en <= resample_fwft_rd_en;

  /* read data from disp fifo */

  always @(posedge clk)
    if (~rst) fifo_osd <= 128'b0;
    else if (clk_en && (state == STATE_RD_OSD) && disp_fwft_valid) fifo_osd <= disp_fwft_dout;
    else fifo_osd <= fifo_osd;

  always @(posedge clk)
    if (~rst) fifo_y <= 128'b0;
    else if (clk_en && (state == STATE_RD_Y) && disp_fwft_valid) fifo_y <= disp_fwft_dout;
    else fifo_y <= fifo_y;

  always @(posedge clk)
    if (~rst) {fifo_u_upper, fifo_u_lower} <= 64'b0;
    else if (clk_en && (state == STATE_RD_U) && disp_fwft_valid) {fifo_u_upper, fifo_u_lower} <= disp_fwft_dout;
    else {fifo_u_upper, fifo_u_lower} <= {fifo_u_upper, fifo_u_lower};

  always @(posedge clk)
    if (~rst) {fifo_v_upper, fifo_v_lower} <= 64'b0;
    else if (clk_en && (state == STATE_RD_V) && disp_fwft_valid) {fifo_v_upper, fifo_v_lower} <= disp_fwft_dout;
    else {fifo_v_upper, fifo_v_lower} <= {fifo_v_upper, fifo_v_lower};

  /* read data from resample fifo */

  always @(posedge clk)
    if (~rst) fifo_position <= 2'b0;
    else if (clk_en && (state == STATE_RD_POS) && resample_fwft_valid) fifo_position <= resample_fwft_dout;
    else fifo_position <= fifo_position;

  /* fifo readers */

  fwft2_reader 
    #(.dta_width(9'd64))
  disp_fwft_reader (
    .rst(rst), 
    .clk(clk), 
    .clk_en(clk_en), 
    .fifo_rd_en(disp_rd_dta_en), 
    .fifo_valid(disp_rd_dta_valid), 
    .fifo_dout(disp_rd_dta), 
    .valid(disp_fwft_valid), 
    .dout(disp_fwft_dout), 
    .rd_en(disp_fwft_rd_en)
    );

  fwft_reader 
    #(.dta_width(9'd3))
  resample_fwft_reader (
    .rst(rst), 
    .clk(clk), 
    .clk_en(clk_en), 
    .fifo_rd_en(resample_rd_en), 
    .fifo_valid(resample_rd_valid), 
    .fifo_dout(resample_rd_dta[2:0]), 
    .valid(resample_fwft_valid), 
    .dout(resample_fwft_dout), 
    .rd_en(resample_fwft_rd_en)
    );

`ifdef DEBUG
  always @(posedge clk)
    if (clk_en) 
        case (state)
        STATE_INIT:                   #0 $display("%m\tSTATE_INIT");
        STATE_RD_OSD:                 #0 $display("%m\tSTATE_RD_OSD");
        STATE_RD_Y:                   #0 $display("%m\tSTATE_RD_Y");
        STATE_RD_U:                   #0 $display("%m\tSTATE_RD_U");
        STATE_RD_V:                   #0 $display("%m\tSTATE_RD_V");
        STATE_RD_POS:                 #0 $display("%m\tSTATE_RD_POS");
        STATE_READY:                  #0 $display("%m\tSTATE_READY");
        STATE_WAIT:                   #0 $display("%m\tSTATE_WAIT");
        default                       #0 $display("%m\t*** Error: unknown state %d", state);
      endcase

  always @(posedge clk)
    $strobe("%m\tstate: %d fifo_read: %d fifo_valid: %d fifo_osd: %32h fifo_y: %32h fifo_u_upper: %16h fifo_u_lower: %16h fifo_v_upper: %16h fifo_v_lower: %16h fifo_position: %d disp_rd_dta_en: %d disp_rd_dta_valid: %d disp_rd_dta: %16h resample_rd_en: %d resample_rd_valid: %d resample_rd_dta: %d", state, fifo_read, fifo_valid, fifo_osd, fifo_y, fifo_u_upper, fifo_u_lower, fifo_v_upper, fifo_v_lower, fifo_position, disp_rd_dta_en, disp_rd_dta_valid, disp_rd_dta, resample_rd_en, resample_rd_valid, resample_rd_dta);

`endif

  end else begin : g_reuse
    /* ============ DVD-FORK FIX (F2): CHROMA ROW REUSE, the data half ==================
     * resample_addrgen decides, per macroblock, which chroma words it requested; the flags
     * arrive with the position code (resample_rd_dta[9:3] = {lcp, sl[1:0], fl, su[1:0], fu},
     * a slot id being {bank, slot}, the bank the row's parity -- see resample_addrgen):
     *   fu / fl : the upper / lower row was fetched -- pop it from the display fifo and
     *             store it in slot su / sl of its plane
     *   else    : read it from slot su / sl (a row an earlier line fetched)
     *   lcp     : the lower row is the upper row, fetched this macroblock: copy it.
     * Macroblocks are processed in exactly the order they were requested, so the RAM here
     * always holds what the address generator's tags say it holds, however far ahead of
     * the display it runs. The column is counted from each line's COL_0 code.
     * Words per macroblock arrive as Y, Y, then the fetched chroma rows in U-upper,
     * U-lower, V-upper, V-lower order: a single-word reader replaces fwft2_reader because
     * the chroma count is no longer even. */
    localparam [2:0]
      R_INIT  = 3'd0,
      R_POS   = 3'd1,
      R_Y0    = 3'd2,
      R_Y1    = 3'd3,
      R_C     = 3'd4,
      R_READY = 3'd5,
      R_WAIT  = 3'd6;

    reg          [2:0]rs;
    reg          [1:0]q;               // chroma word: 0 U upper, 1 U lower, 2 V upper, 3 V lower
    reg          [5:0]col;             // column within the line (cache index)
    reg          [6:0]flg;             // {lcp, sl[1:0], fl, su[1:0], fu}

    wire             d_valid;
    wire       [63:0]d_dout;
    wire             p_valid;
    wire        [9:0]p_dout;

    wire             q_low   = q[0];
    wire             q_fetch = q_low ? flg[3] : flg[0];
    wire        [1:0]q_slot  = q_low ? flg[5:4] : flg[2:1];
    wire             q_copy  = q_low & flg[6];
    wire        [8:0]c_addr  = {q[1], q_slot, col};
    wire             c_step  = (rs == R_C) && (~q_fetch || d_valid);

    wire             d_rd_en = (rs == R_Y0) || (rs == R_Y1) || ((rs == R_C) && q_fetch);
    wire             p_rd_en = (rs == R_POS);

    /* the cache: 2 planes x 2 banks x 2 slots x 64 columns x 8 chroma samples */
    reg        [63:0]cram [0:511];
    reg        [63:0]cram_q;
    wire             cram_we = (rs == R_C) && q_fetch && d_valid;
    always @(posedge clk)
      if (clk_en && cram_we) cram[c_addr] <= d_dout;
    always @(posedge clk)
      if (clk_en) cram_q <= cram[c_addr];

    /* a slot read presented in R_C lands one cycle later */
    reg              pend;
    reg          [1:0]pend_q;
    always @(posedge clk)
      if (~rst) begin pend <= 1'b0; pend_q <= 2'd0; end
      else if (clk_en) begin
        pend   <= c_step && ~q_fetch && ~q_copy;
        pend_q <= q;
      end

    always @(posedge clk)
      if (~rst) rs <= R_INIT;
      else if (clk_en)
        case (rs)
          R_INIT:  rs <= R_POS;
          R_POS:   if (p_valid) rs <= R_Y0;
          R_Y0:    if (d_valid) rs <= R_Y1;
          R_Y1:    if (d_valid) rs <= R_C;
          R_C:     if (c_step && (q == 2'd3)) rs <= R_READY;
          R_READY: if (fifo_read) rs <= R_WAIT;
          R_WAIT:  if (~fifo_read) rs <= R_POS;
          default  rs <= R_INIT;
        endcase

    always @(posedge clk)
      if (~rst) q <= 2'd0;
      else if (clk_en && (rs == R_Y1)) q <= 2'd0;
      else if (clk_en && c_step) q <= q + 2'd1;

    always @(posedge clk)
      if (~rst) begin flg <= 7'd0; col <= 6'd0; fifo_position <= 3'd0; end
      else if (clk_en && (rs == R_POS) && p_valid) begin
        flg           <= p_dout[9:3];
        fifo_position <= p_dout[2:0];
        col           <= ((p_dout[2:0] == ROW_0_COL_0) || (p_dout[2:0] == ROW_1_COL_0) ||
                          (p_dout[2:0] == ROW_X_COL_0)) ? 6'd0 : col + 6'd1;
      end

    /* inform resample_bilinear data is valid (as the original) */
    always @(posedge clk)
      if (~rst) fifo_valid <= 1'b0;
      else if (clk_en && (rs == R_READY)) fifo_valid <= 1'b1;
      else if (clk_en && (rs == R_WAIT)) fifo_valid <= fifo_read;
      else if (clk_en) fifo_valid <= 1'b0;

    always @(posedge clk)
      if (~rst) fifo_osd <= 128'b0;       // no OSD reads (OSD_READS must be 0)

    always @(posedge clk)
      if (~rst) fifo_y <= 128'b0;
      else if (clk_en && (rs == R_Y0) && d_valid) fifo_y[127:64] <= d_dout;
      else if (clk_en && (rs == R_Y1) && d_valid) fifo_y[63:0]   <= d_dout;

    /* each chroma word from the display fifo, from the upper word just fetched, or from
     * the slot read issued the cycle before */
    wire             fetch_w = (rs == R_C) && q_fetch && d_valid;
    wire             copy_w  = (rs == R_C) && q_copy;
    always @(posedge clk)
      if (~rst) begin
        fifo_u_upper <= 64'b0; fifo_u_lower <= 64'b0;
        fifo_v_upper <= 64'b0; fifo_v_lower <= 64'b0;
      end else if (clk_en) begin
        if (fetch_w && (q == 2'd0)) fifo_u_upper <= d_dout;
        if (fetch_w && (q == 2'd1)) fifo_u_lower <= d_dout;
        if (fetch_w && (q == 2'd2)) fifo_v_upper <= d_dout;
        if (fetch_w && (q == 2'd3)) fifo_v_lower <= d_dout;
        if (copy_w  && (q == 2'd1)) fifo_u_lower <= fifo_u_upper;
        if (copy_w  && (q == 2'd3)) fifo_v_lower <= fifo_v_upper;
        if (pend && (pend_q == 2'd0)) fifo_u_upper <= cram_q;
        if (pend && (pend_q == 2'd1)) fifo_u_lower <= cram_q;
        if (pend && (pend_q == 2'd2)) fifo_v_upper <= cram_q;
        if (pend && (pend_q == 2'd3)) fifo_v_lower <= cram_q;
      end

    fwft_reader
      #(.dta_width(9'd64))
    disp_fwft_reader (
      .rst(rst),
      .clk(clk),
      .clk_en(clk_en),
      .fifo_rd_en(disp_rd_dta_en),
      .fifo_valid(disp_rd_dta_valid),
      .fifo_dout(disp_rd_dta),
      .valid(d_valid),
      .dout(d_dout),
      .rd_en(d_rd_en)
      );

    fwft_reader
      #(.dta_width(9'd10))
    resample_fwft_reader (
      .rst(rst),
      .clk(clk),
      .clk_en(clk_en),
      .fifo_rd_en(resample_rd_en),
      .fifo_valid(resample_rd_valid),
      .fifo_dout(resample_rd_dta),
      .valid(p_valid),
      .dout(p_dout),
      .rd_en(p_rd_en)
      );

`ifdef __IVERILOG__
    initial if (OSD_READS != 0) $fatal(1, "%m: CHROMA_REUSE requires OSD_READS = 0");
`endif
  end endgenerate
endmodule
/* not truncated */
