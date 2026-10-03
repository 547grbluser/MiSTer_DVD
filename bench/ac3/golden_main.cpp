// bench/ac3/golden_main.cpp -- the AC-3 front end's own output, as exact integers
//
// Feeds an AC-3 elementary stream into the Verilated ac3_front and writes, for every
// audio block, exactly what the parse half of the decoder hands imdct_512: the block's
// coefficients (coeff_mem, signed Q1.23, every fbw channel plus the LFE slot), blksw and
// dynrng, and per frame acmod / lfeon / cmixlev / surmixlev. That interface is the
// contract an engine-based AC-3 parse must reproduce bit for bit (docs/dts_decoder.md
// sec 4 scenario E; docs/ac3_engine.md), so THE RTL IS THE GOLDEN here -- not liba52,
// which the fixed-point datapath can only approximate. No liba52 is linked.
//
//   make -f Makefile.golden && ./obj_golden/ac3_golden STREAM.ac3 > STREAM.gold
//   AC3_MAX_FRAMES=N   stop after N frames (default: the whole stream)
//   AC3_TAP=imdct      capture at imdct_done instead of mant_done (a check that the
//                      capture point sees every write: the two must dump identically)
//
// Output, one record a line:
//   F frame start_byte len_bytes acmod lfeon cmixlev surmixlev
//   B frame blk blksw dynrng
//   C ch v0 v1 ... v255          (hex, 24-bit two's complement; LFE: ch 6, 7 values)
//   E frame                      (err_unsupported rose during this frame)
// Internal signals (blksw, dynrng) are read through Verilator's --public-flat-rw, so
// the RTL is not changed for this tap.

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include "Vac3_front.h"
#include "Vac3_front___024root.h"
#include "verilated.h"

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    if (argc < 2) { std::fprintf(stderr, "usage: ac3_golden STREAM.ac3\n"); return 2; }
    FILE* f = std::fopen(argv[1], "rb");
    if (!f) { std::perror(argv[1]); return 2; }
    std::vector<unsigned char> buf;
    for (int c; (c = std::fgetc(f)) != EOF;) buf.push_back((unsigned char)c);
    std::fclose(f);
    const long n = (long)buf.size();
    const int max_frames = getenv("AC3_MAX_FRAMES") ? atoi(getenv("AC3_MAX_FRAMES")) : 1 << 30;

    Vac3_front* dut = new Vac3_front;
    auto* r = dut->rootp;
    auto eval0   = [&]() { dut->clk = 0; dut->eval(); };
    auto posedge = [&]() { dut->clk = 1; dut->eval(); };
    dut->rst = 1; dut->wr_en = 0; dut->wr_data = 0; dut->pcm_done = 1;
    for (int i = 0; i < 6; i++) { eval0(); posedge(); }
    dut->rst = 0;

    long fed = 0, idle = 0;
    int frame = -1, blk = 0, nf = 2, prev_err = 0, blocks = 0;
    long fstart = 0; int flen = 0;
    const bool at_imdct = getenv("AC3_TAP") && std::string(getenv("AC3_TAP")) == "imdct";
    int pend_blksw = 0, pend_dynrng = 0;
    // the RTL's own timing (stderr): the parse's cycles a block (block start to
    // mant_done) and the IMDCT's (mant_done to imdct_done), which run in series
    long t_blk = -1, t_mant = -1, parse_cyc = 0, imdct_cyc = 0, max_parse = 0, max_imdct = 0;
    for (long cyc = 0; ; cyc++) {
        eval0();
        bool can = (fed < n) && !dut->full;
        dut->wr_en = can;
        dut->wr_data = can ? buf[fed] : 0;
        posedge();
        if (can) fed++;

        if (dut->frame_hdr_valid) {
            if (frame + 1 >= max_frames) break;
            frame++; blk = 0;
            fstart = (long)dut->sync_bitpos / 8 - 2; flen = (int)dut->frame_bytes;
        }
        if (dut->bsi_valid) {
            int ac = (int)dut->acmod;
            nf = (ac == 1) ? 1 : (ac == 0 || ac == 2) ? 2 : (ac == 3 || ac == 4) ? 3 : (ac == 7) ? 5 : 4;
            std::printf("F %d %ld %d %d %d %d %d\n", frame, fstart, flen, ac, (int)dut->lfeon,
                        (int)dut->cmixlev, (int)dut->surmixlev);
        }
        if (dut->block_side_valid && t_blk < 0) t_blk = cyc;
        if (dut->mant_done && t_blk >= 0) {
            long d = cyc - t_blk; parse_cyc += d; if (d > max_parse) max_parse = d;
            t_mant = cyc; t_blk = -1;
        }
        if (dut->imdct_done && t_mant >= 0) {
            long d = cyc - t_mant; imdct_cyc += d; if (d > max_imdct) max_imdct = d;
            t_mant = -1;
        }
        if (dut->mant_done) {        // the block's side info, as imdct_512 starts on it
            pend_blksw = (int)r->ac3_front__DOT__u_parse__DOT__blksw;
            pend_dynrng = (int)r->ac3_front__DOT__u_parse__DOT__dynrng;
        }
        if ((at_imdct ? dut->imdct_done : dut->mant_done) && frame >= 0) {
            std::printf("B %d %d %d %d\n", frame, blk, pend_blksw, pend_dynrng);
            auto dump = [&](int ch, int cnt) {
                std::printf("C %d", ch);
                for (int i = 0; i < cnt; i++) {
                    dut->coeff_rd_addr = (ch << 8) | i;
                    dut->eval();
                    std::printf(" %06x", (unsigned)dut->coeff_rd_data & 0xFFFFFF);
                }
                std::printf("\n");
            };
            for (int ch = 0; ch < nf; ch++) dump(ch, 256);
            if (dut->lfeon) dump(6, 7);
            dut->coeff_rd_addr = 0;
            blk++; blocks++;
            idle = 0;
        }
        if (dut->err_unsupported && !prev_err) std::printf("E %d\n", frame);
        prev_err = (int)dut->err_unsupported;

        if (fed >= n && ++idle > 4'000'000) break;     // input exhausted, decoder quiet
    }
    std::fprintf(stderr, "ac3_golden: %s: %d frames, %d blocks; cycles a block: parse mean %ld max %ld"
                 " (from the block's side info, so the side-info parse itself is not counted), IMDCT mean %ld max %ld\n",
                 argv[1], frame + 1, blocks, blocks ? parse_cyc / blocks : 0, max_parse,
                 blocks ? imdct_cyc / blocks : 0, max_imdct);
    delete dut;
    return 0;
}
