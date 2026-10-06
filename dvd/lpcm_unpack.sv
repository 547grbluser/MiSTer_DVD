//============================================================================
//  lpcm_unpack.sv — DVD LPCM frame bytes -> s16 L/R sample pairs (fabric audio).
//
//  Consumes the LPCM payload bytes that ps_demux has already stripped of the
//  6-byte private_stream_1 LPCM sub-header, so the byte stream here is raw
//  interleaved PCM samples.  DVD LPCM is BIG-ENDIAN; the MiSTer framework wants
//  signed little-endian s16 on AUDIO_L/R, so each 16-bit sample is assembled
//  hi-byte-first (b0<<8 | b1).  Samples are interleaved L,R,L,R...; we pack
//  {L,R} pairs into a small FIFO drained one pair per `aud_ce` (~48 kHz).
//
//  SCOPE: every DVD-Video LPCM format (docs/lpcm_full.md): 48 or 96 kHz, 16 / 20 /
//  24-bit (top-16-bit truncation for the 16-bit HDMI path), 1 to 8 channels. The
//  `quant` input carries the LPCM sub-header word length that ps_demux captures
//  (0=16, 1=20, 2=24), `nch_m1` its channel count - 1, and `dec` asks for 96 kHz to
//  be decimated to 48 (the HDMI link is 48 kHz; dvd_audio_decode decides).
//
//  TWO PATHS. Stereo at the output rate (nch_m1 == 1, dec == 0) is the original
//  assembler below, cycle for cycle: DVD LPCM stereo, CD-DA/WAV and the engine's
//  serialised DTS/MP2 pairs all take it. Anything else takes the NEW path:
//  the same group walk (mono's groups are 2 samples, every other count's 4) emits
//  one s16 sample at a time with its channel; a downmix multiply-accumulates each
//  sample-time into a stereo pair (FFmpeg's channel order, the AC-3 path's law:
//  tools/lpcm_model.py DMX_Q); then either the pair FIFO, or lpcm_hb's half-band
//  2:1 for 96 kHz on a 48 kHz link. The FIFO always receives stereo pairs.
//  The path is chosen at a group start with the channel count at 0, so it can only
//  change between sample-times (in practice a track change resets this module).
//
//  20/24-bit DVD LPCM packing (per FFmpeg libavcodec/pcm-dvd.c): a group spans
//  2 sample-times per channel.  For STEREO the group is the four high 16-bit
//  words in stream order `L0 R0 L1 R1` (8 bytes) — the SAME layout as plain
//  16-bit interleave — followed by the low bits: 20-bit adds 2 trailing
//  nibble-bytes, 24-bit adds 4 trailing bytes.  Since AUDIO_L/R is 16-bit we
//  keep only the four high words and DISCARD the trailing low bytes.  So the
//  unpacker assembles two {L,R} pairs exactly as at 16-bit, then skips
//  `skip_n` bytes (0 / 2 / 4) before the next group.  quant=16 => skip_n=0 =>
//  behaviour identical to the original 16-bit-only assembler (bit-for-bit).
//  For any other channel count the grouping is the same flat sequence of 4-sample
//  groups in stream order (pcm-dvd.c), so only which channel a sample belongs to
//  changes. MONO is the exception: its groups are 2 samples (two high words, then
//  1 or 2 low bytes), checked against FFmpeg's decoder by tools/lpcm_model.py.
//
//  Back-pressure: `full` is asserted when the pair FIFO can't take another pair
//  (on the new path: within 4 pairs of full, or lpcm_hb's ring is backed up);
//  the caller (dvd_audio_decode dispatch) must hold its read side off the audio
//  ring while full, which lets audio_ring drop a whole frame on its own overflow
//  rather than ever stalling video.
//============================================================================

`timescale 1ns/1ps

module lpcm_unpack #(
    parameter int FIFO_AW = 9,           // pair-FIFO depth = 2^AW (>= one frame)
    // D4 (docs/dts_decoder.md): the pair FIFO's power-up contents -- in the core, half
    // the DTS ADPCM codebook, copied out once by dts_cb_mem. "" = none (every bench).
    parameter     CB_INIT = ""
) (
    input  logic        clk,
    input  logic        rst,             // synchronous, active-high

    // LPCM word length from ps_demux (sub-header byte +5 bits[7:6]):
    // 0=16-bit, 1=20-bit, 2=24-bit. Stable per track. Sampled at group
    // boundaries so a mid-group change can't desync the assembler.
    input  logic [1:0]  quant,

    // Byte order: 0 = big-endian (DVD LPCM, the original behaviour, bit-for-bit
    // unchanged), 1 = little-endian (WAV / CD-DA raw PCM: L.lo L.hi R.lo R.hi).
    // Static per source; only meaningful with quant=0 (16-bit).
    input  logic        le,

    // Channels - 1 (sub-header byte +5 bits[2:0]; 1 = stereo) and 96 -> 48 kHz
    // decimation. nch_m1 == 1 && !dec is the original stereo path, bit for bit; tie
    // them so wherever the source is not DVD LPCM.
    input  logic [2:0]  nch_m1,
    input  logic        dec,

    // byte input (raw PCM, sub-header already stripped)
    input  logic        wr_en,
    input  logic [7:0]  wr_data,
    output logic        full,            // pair FIFO can't accept another pair
    output logic        afull,           // level within 64 pairs of full — the
                                         // CD-DA reader backpressure tap (stalls
                                         // sd block requests ahead of hard-full)

    // audio-domain pop (single clock here: aud_ce is a clk-domain enable)
    input  logic        aud_ce,          // ~48 kHz sample tick (1-cycle enable)
    output logic signed [15:0] audio_l,
    output logic signed [15:0] audio_r,
    output logic        aud_valid,       // 1 = a real pair was popped this aud_ce

    // D4 codebook copy: while cp_mode the FIFO's read register follows mem[rptr] every
    // cycle (the SAME read: one port) onto audio_l/audio_r, and cp_step advances rptr;
    // aud_valid stays low. Tie both 1'b0 everywhere else.
    input  logic        cp_mode,
    input  logic        cp_step
);

    // ---- group-aware byte assembler ----------------------------------------
    // A DVD LPCM group = 2 stereo sample-pairs.  The first 8 bytes hold the four
    // high 16-bit words (L0 R0 L1 R1) and assemble two {L,R} pairs exactly like
    // plain 16-bit.  20-bit adds 2 trailing low-nibble bytes, 24-bit adds 4 —
    // discarded (top-16 truncation).  `gbyte` walks 0..glen-1; bytes 0-7 are data
    // (phase = gbyte[1:0], pair = gbyte[2]), bytes >=8 are skipped.
    logic [3:0]  gbyte;                   // byte index within the current group
    logic [3:0]  glen;                    // group length: 8 (16b) / 10 (20b) / 12 (24b)
    logic [7:0]  lhi, llo, rhi;           // held bytes
    logic [15:0] samp_l, samp_r;
    logic        pair_wr;

    // Group length selected by quant (latched at each group start so a mid-group
    // change can't shorten/lengthen the group in flight).
    wire [3:0] glen_leg  = (quant == 2'd1) ? 4'd10 :   // 20-bit: 8 + 2 low bytes
                           (quant == 2'd2) ? 4'd12 :   // 24-bit: 8 + 4 low bytes
                                             4'd8;      // 16-bit: no low bytes
    // mono (new path only): a group is 2 samples, 4 data bytes + 0 / 1 / 2 low bytes
    wire [3:0] glen_mono = (quant == 2'd1) ? 4'd5 : (quant == 2'd2) ? 4'd6 : 4'd4;

    // ---- the path, latched at a sample-time boundary (group start, channel 0) ----
    logic [2:0]  ch;                      // new path: the channel of the next sample
    logic        npath;                   // latched: the new path is active
    logic [2:0]  nch_l;
    logic        dec_l;
    wire         at_st   = (gbyte == 4'd0) && (ch == 3'd0);
    wire         np_cur  = at_st ? !((nch_m1 == 3'd1) && !dec) : npath;
    wire  [2:0]  nch_cur = at_st ? nch_m1 : nch_l;
    wire         mono    = np_cur && (nch_cur == 3'd0);
    wire [3:0] glen_next = mono ? glen_mono : glen_leg;
    wire [3:0] dlim      = mono ? 4'd4 : 4'd8;  // data bytes in a group (new path)

    // ---- pair FIFO (depth 2^FIFO_AW), entry = {L[15:0], R[15:0]} ------------
    localparam int DEPTH = (1 << FIFO_AW);
    logic [31:0] mem [0:DEPTH-1];
    initial if (CB_INIT != "") $readmemh(CB_INIT, mem);   // D4
    logic [FIFO_AW:0] wptr, rptr;         // extra MSB for full/empty disambiguation
    wire  [FIFO_AW:0] level = wptr - rptr;
    wire        empty = (wptr == rptr);
    wire        fifo_full = (level == DEPTH[FIFO_AW:0]);

    // We must be able to take a full pair; `full` warns the producer one slot out.
    // The new path keeps up to 3 pairs in flight (downmix, half-band): 4 pairs early.
    wire        hb_hold;
    assign full  = npath ? ((level >= (DEPTH[FIFO_AW:0] - 4)) || (dec_l && hb_hold)) : fifo_full;
    assign afull = (level >= (DEPTH[FIFO_AW:0] - 64));

    // new path: one s16 sample at a time, with its channel
    logic [7:0]  shi;                     // the sample's high byte
    logic        smp_v;
    logic signed [15:0] smp;
    logic [2:0]  smp_ch;
    logic        smp_last;                // the sample-time's last channel

    // assemble bytes -> pair_wr (original path) and smp_v (new path)
    always_ff @(posedge clk) begin
        if (rst) begin
            gbyte   <= 4'd0;
            glen    <= 4'd8;               // 16-bit default until first group start
            pair_wr <= 1'b0;
            ch      <= 3'd0;
            npath   <= 1'b0;
            nch_l   <= 3'd1;
            dec_l   <= 1'b0;
            smp_v   <= 1'b0;
        end else begin
            pair_wr <= 1'b0;
            smp_v   <= 1'b0;
            if (wr_en) begin
                // new path: latch the path at a sample-time boundary; take a sample
                // per two data bytes (big-endian; LE is CD-DA's, original path only)
                if (at_st) begin
                    npath <= np_cur;
                    nch_l <= nch_m1;
                    dec_l <= dec;
                end
                if (np_cur && (gbyte < dlim)) begin
                    if (!gbyte[0]) shi <= wr_data;
                    else begin
                        smp      <= {shi, wr_data};
                        smp_ch   <= ch;
                        smp_last <= (ch == nch_cur);
                        smp_v    <= 1'b1;
                        ch       <= (ch == nch_cur) ? 3'd0 : ch + 3'd1;
                    end
                end
                // Latch the group length at the start of each group.
                if (gbyte == 4'd0) glen <= glen_next;
                // Data bytes 0-7: assemble two {L,R} pairs (phase gbyte[1:0]).
                // Bytes >=8 are trailing low bits — consumed and discarded.
                // le=1 swaps the within-sample byte order: phases 0/1 deliver
                // L.lo then L.hi (held cross-wise so samp_l = {lhi,llo} stays
                // right for both orders), phase 2 holds R's FIRST byte in rhi
                // (its hi byte in BE, its LO byte in LE), phase 3 completes R.
                if (gbyte < 4'd8) begin
                    case (gbyte[1:0])
                        2'd0: if (le) llo <= wr_data; else lhi <= wr_data;
                        2'd1: if (le) lhi <= wr_data; else llo <= wr_data;
                        2'd2: rhi <= wr_data;
                        2'd3: begin
                            samp_l  <= {lhi, llo};
                            samp_r  <= le ? {wr_data, rhi} : {rhi, wr_data};
                            pair_wr <= 1'b1;   // push assembled pair next cycle
                        end
                    endcase
                end
                // Advance within the group; wrap at glen (uses the freshly latched
                // value on the group's first byte, else the held one).
                if (gbyte == (((gbyte == 4'd0) ? glen_next : glen) - 4'd1))
                    gbyte <= 4'd0;
                else
                    gbyte <= gbyte + 4'd1;
            end
        end
    end

    // ---- new path: the downmix (tools/lpcm_model.py downmix) ----------------
    // acc = sum over a sample-time of s x g, Q1.17 gains; the pair = sat16((acc +
    // 2^16) >>> 17). Gains from DMX_Q (FFmpeg's order; each side's gains sum to 1.0,
    // so only rounding can reach the rails). Two multipliers, one sample a cycle.
    // ⚠ 19 bits, not 18: unity is 131072 = 2^17, which an 18-bit signed constant
    // wraps to -131072 without a warning (it negated every mono and 2.1 sample in
    // the first bench run). 16 x 19 is still one DSP mode (18 x 19).
    logic signed [18:0] gl, gr;
    always_comb begin
        case ({nch_l, smp_ch})
            // 1: mono, to both at unity
            6'o00: begin gl = 19'sd131072; gr = 19'sd131072; end
            // 2: stereo (only with dec: 96 kHz stereo on a 48 kHz link)
            6'o10: begin gl = 19'sd131072; gr = 19'sd0;      end
            6'o11: begin gl = 19'sd0;      gr = 19'sd131072; end
            // 3: 2.1 FL FR LFE
            6'o20: begin gl = 19'sd131072; gr = 19'sd0;      end
            6'o21: begin gl = 19'sd0;      gr = 19'sd131072; end
            // 4: 4.0 FL FR FC BC
            6'o30: begin gl = 19'sd59386;  gr = 19'sd0;      end
            6'o31: begin gl = 19'sd0;      gr = 19'sd59386;  end
            6'o32: begin gl = 19'sd41993;  gr = 19'sd41993;  end
            6'o33: begin gl = 19'sd29693;  gr = 19'sd29693;  end
            // 5: 5.0 FL FR FC BL BR
            6'o40: begin gl = 19'sd54292;  gr = 19'sd0;      end
            6'o41: begin gl = 19'sd0;      gr = 19'sd54292;  end
            6'o42: begin gl = 19'sd38390;  gr = 19'sd38390;  end
            6'o43: begin gl = 19'sd38390;  gr = 19'sd0;      end
            6'o44: begin gl = 19'sd0;      gr = 19'sd38390;  end
            // 6: 5.1 FL FR FC LFE BL BR
            6'o50: begin gl = 19'sd54292;  gr = 19'sd0;      end
            6'o51: begin gl = 19'sd0;      gr = 19'sd54292;  end
            6'o52: begin gl = 19'sd38390;  gr = 19'sd38390;  end
            6'o54: begin gl = 19'sd38390;  gr = 19'sd0;      end
            6'o55: begin gl = 19'sd0;      gr = 19'sd38390;  end
            // 7: 6.1 FL FR FC LFE BC SL SR
            6'o60: begin gl = 19'sd44977;  gr = 19'sd0;      end
            6'o61: begin gl = 19'sd0;      gr = 19'sd44977;  end
            6'o62: begin gl = 19'sd31803;  gr = 19'sd31803;  end
            6'o64: begin gl = 19'sd22488;  gr = 19'sd22488;  end
            6'o65: begin gl = 19'sd31803;  gr = 19'sd0;      end
            6'o66: begin gl = 19'sd0;      gr = 19'sd31803;  end
            // 8: 7.1 FL FR FC LFE BL BR SL SR
            6'o70: begin gl = 19'sd41992;  gr = 19'sd0;      end
            6'o71: begin gl = 19'sd0;      gr = 19'sd41992;  end
            6'o72: begin gl = 19'sd29693;  gr = 19'sd29693;  end
            6'o74: begin gl = 19'sd29693;  gr = 19'sd0;      end
            6'o75: begin gl = 19'sd0;      gr = 19'sd29693;  end
            6'o76: begin gl = 19'sd29693;  gr = 19'sd0;      end
            6'o77: begin gl = 19'sd0;      gr = 19'sd29693;  end
            default: begin gl = 19'sd0;    gr = 19'sd0;      end   // LFE, and unused
        endcase
    end

    logic        p_v, p_first, p_last;
    logic signed [34:0] p_l, p_r;
    logic signed [37:0] acc_l, acc_r;
    logic        m_v;                     // a downmixed pair
    logic signed [15:0] m_l, m_r;
    wire  signed [37:0] sum_l = (p_first ? 38'sd0 : acc_l) + p_l;
    wire  signed [37:0] sum_r = (p_first ? 38'sd0 : acc_r) + p_r;
    wire  signed [37:0] rnd_l = sum_l + 38'sd65536;
    wire  signed [37:0] rnd_r = sum_r + 38'sd65536;
    wire  signed [20:0] dm_l  = rnd_l[37:17];
    wire  signed [20:0] dm_r  = rnd_r[37:17];
    always_ff @(posedge clk) begin
        if (rst) begin
            p_v <= 1'b0; m_v <= 1'b0;
        end else begin
            p_v <= smp_v;
            if (smp_v) begin
                p_l <= smp * gl;
                p_r <= smp * gr;
                p_first <= (smp_ch == 3'd0);
                p_last  <= smp_last;
            end
            m_v <= 1'b0;
            if (p_v) begin
                acc_l <= sum_l;
                acc_r <= sum_r;
                if (p_last) begin
                    m_v <= 1'b1;
                    m_l <= (dm_l > 21'sd32767) ? 16'sh7FFF : (dm_l < -21'sd32768) ? 16'sh8000 : dm_l[15:0];
                    m_r <= (dm_r > 21'sd32767) ? 16'sh7FFF : (dm_r < -21'sd32768) ? 16'sh8000 : dm_r[15:0];
                end
            end
        end
    end

    // ---- new path: 96 kHz on a 48 kHz link -> the half-band, 2:1 -------------
    wire               hb_v;
    wire signed [15:0] hb_l, hb_r;
    lpcm_hb u_hb (
        .clk      (clk),
        .rst      (rst),
        .in_v     (npath && dec_l && m_v),
        .in_l     (m_l),
        .in_r     (m_r),
        .hold     (hb_hold),
        .out_room (!fifo_full),
        .out_v    (hb_v),
        .out_l    (hb_l),
        .out_r    (hb_r)
    );

    // FIFO write: the original pair, or the new path's (downmix, or half-band)
    wire        f_wr = npath ? (dec_l ? hb_v : m_v) : pair_wr;
    wire [31:0] f_d  = npath ? (dec_l ? {hb_l, hb_r} : {m_l, m_r}) : {samp_l, samp_r};
    always_ff @(posedge clk) begin
        if (rst) begin
            wptr <= '0;
        end else if (f_wr && !fifo_full) begin
            mem[wptr[FIFO_AW-1:0]] <= f_d;
            wptr <= wptr + 1'b1;
        end
    end

    // FIFO read — one pair per aud_ce; hold last sample + drop aud_valid on empty
    always_ff @(posedge clk) begin
        if (rst) begin
            rptr      <= '0;
            audio_l   <= '0;
            audio_r   <= '0;
            aud_valid <= 1'b0;
        end else begin
            aud_valid <= 1'b0;
            if (cp_mode) begin                       // D4 codebook copy
                audio_l <= mem[rptr[FIFO_AW-1:0]][31:16];
                audio_r <= mem[rptr[FIFO_AW-1:0]][15:0];
                if (cp_step) rptr <= rptr + 1'b1;
            end else if (aud_ce) begin
                if (!empty) begin
                    audio_l   <= mem[rptr[FIFO_AW-1:0]][31:16];
                    audio_r   <= mem[rptr[FIFO_AW-1:0]][15:0];
                    aud_valid <= 1'b1;
                    rptr      <= rptr + 1'b1;
                end
                // empty: hold audio_l/r, aud_valid stays low (silence/hold)
            end
        end
    end

endmodule
