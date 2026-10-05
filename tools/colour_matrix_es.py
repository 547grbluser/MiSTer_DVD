#!/usr/bin/env python3
"""colour_matrix_es.py -- the header-only stream behind bench/dvd/run_colour_matrix.sh.

Writes two $readmemh files:

  <out>_es.hex   the elementary stream, 64-bit big-endian words (first byte in
                 [63:56]) -- the format bench/dvd/vld_mpeg1_tb.sv reads.
  <out>_exp.hex  one 16-bit word per coded picture, in stream order:
                 {arm[3:0], hold, rgb, 2'b00, mc[7:0]}
                   mc    the matrix_coefficients the vld must have committed
                         for this picture
                   arm   which gate arm this picture's verdict belongs to
                   hold  the matrix must stay === mc on EVERY cycle from the
                         previous picture's commit to this one (the
                         repeated-sequence-header streak guard, arm 5)
                   rgb   also score yuv2rgb's RGB for this picture against
                         the textbook matrix (arms 1 and 3)

The pictures carry no slices. What is under test is header parsing: which
matrix the vld hands yuv2rgb for each sequence. A real disc cannot provide
the junctions this needs (a tagged-709 sequence, then an untagged one, then
MPEG-1) because no disc in the library is tagged 709 (tools/colour_scan.py),
so the stream is built here, field by field from ISO 13818-2 / 11172-2.

Usage: tools/colour_matrix_es.py <out-prefix>
"""
import sys


class Bits(object):
    def __init__(self):
        self.bits = []

    def put(self, val, n):
        for i in range(n - 1, -1, -1):
            self.bits.append((val >> i) & 1)

    def align(self):                     # next_start_code(): zero stuffing to a byte
        while len(self.bits) % 8:
            self.bits.append(0)

    def start(self, code):
        self.align()
        self.put(0x000001, 24)
        self.put(code, 8)

    def data(self):
        self.align()
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            v = 0
            for b in self.bits[i:i + 8]:
                v = (v << 1) | b
            out.append(v)
        return bytes(out)


def seq_header(b, w, h, mpeg1=False):
    b.start(0xB3)
    b.put(w, 12)
    b.put(h, 12)
    b.put(0xC if mpeg1 else 2, 4)        # aspect: MPEG-1 pel aspect (CCIR 601 525) / 4:3
    b.put(4, 4)                          # frame_rate_code 29.97
    b.put(20000, 18)                     # bit_rate_value (x400 bit/s)
    b.put(1, 1)                          # marker
    b.put(112, 10)                       # vbv_buffer_size_value
    b.put(1 if mpeg1 else 0, 1)          # constrained_parameters_flag
    b.put(0, 1)                          # load_intra_quantiser_matrix
    b.put(0, 1)                          # load_non_intra_quantiser_matrix


def seq_ext(b):
    b.start(0xB5)
    b.put(1, 4)                          # extension id: sequence
    b.put(0x48, 8)                       # profile_and_level: MP@ML
    b.put(0, 1)                          # progressive_sequence
    b.put(1, 2)                          # chroma_format 4:2:0
    b.put(0, 2); b.put(0, 2)             # size extensions
    b.put(0, 12)                         # bit_rate_extension
    b.put(1, 1)                          # marker
    b.put(0, 8)                          # vbv_buffer_size_extension
    b.put(0, 1)                          # low_delay
    b.put(0, 2); b.put(0, 5)             # frame_rate_extension_n/d


def disp_ext(b, mc=None, cp=6, tc=6):
    """mc=None writes colour_description=0 (the three bytes absent)."""
    b.start(0xB5)
    b.put(2, 4)                          # extension id: sequence display
    b.put(2, 3)                          # video_format NTSC
    if mc is None:
        b.put(0, 1)
    else:
        b.put(1, 1)
        b.put(cp, 8); b.put(tc, 8); b.put(mc, 8)
    b.put(720, 14)                       # display_horizontal_size
    b.put(1, 1)                          # marker
    b.put(480, 14)                       # display_vertical_size


def gop(b):
    b.start(0xB8)
    b.put(0, 25 - 1 - 6 - 6)             # drop flag, hours, minutes
    b.put(1, 1)                          # marker
    b.put(0, 12)                         # seconds, pictures
    b.put(1, 1)                          # closed_gop
    b.put(0, 1)                          # broken_link


def picture(b, tref, mpeg1=False):
    b.align()
    b.put(0, 8 * 24)                     # zero stuffing (legal before any start code):
    #                                      spaces the commits so the bench's settled
    #                                      sample lands before the next picture
    b.start(0x00)
    b.put(tref, 10)
    b.put(1, 3)                          # I picture
    b.put(0xFFFF, 16)                    # vbv_delay
    b.put(0, 1)                          # extra_bit_picture
    if not mpeg1:
        b.start(0xB5)                    # picture_coding_extension
        b.put(8, 4)
        b.put(0xFFFF, 16)                # f_codes (unused for I)
        b.put(0, 2)                      # intra_dc_precision
        b.put(3, 2)                      # frame picture
        b.put(1, 1)                      # top_field_first
        b.put(1, 1)                      # frame_pred_frame_dct
        b.put(0, 1); b.put(0, 1); b.put(0, 1); b.put(0, 1)
        b.put(0, 1)                      # repeat_first_field
        b.put(1, 1)                      # chroma_420_type
        b.put(0, 1)                      # progressive_frame
        b.put(0, 1)                      # composite_display_flag


def build():
    b = Bits()
    exp = []
    tref = [0]

    def pic(arm, mc, hold=0, rgb=0, mpeg1=False):
        picture(b, tref[0], mpeg1)
        tref[0] += 1
        exp.append((arm << 12) | (hold << 11) | (rgb << 10) | mc)

    def seq(mc='none', mpeg1=False, w=720, h=480):
        """mc: 'none' = no display extension, None = colour_description 0."""
        seq_header(b, w, h, mpeg1)
        if not mpeg1:
            seq_ext(b)
            if mc != 'none':
                disp_ext(b, mc)
        gop(b)

    # [1] MPEG-2, no sequence_display_extension, out of reset -> 0, decodes 601
    seq('none'); pic(1, 0, rgb=1); pic(1, 0, rgb=1)
    # [3] tagged 6 (SMPTE 170M) -> 6, decodes 601
    seq(6); pic(3, 6, rgb=1)
    # [3] tagged 1 (BT.709), explicit -> honoured; then [5] the same tag repeated
    # in three more sequence headers: the output must never leave 1 in between
    seq(1); pic(3, 1, rgb=1)
    for _ in range(3):
        seq(1); pic(5, 1, hold=1)
    # [2] colour_description = 0 straight after a 709 sequence -> 0, not inherited
    seq(None); pic(2, 0)
    # [2] no display extension straight after a 709 sequence -> 0
    seq(1); pic(3, 1, rgb=1)
    seq('none'); pic(2, 0)
    # [4] MPEG-2 (709) -> MPEG-1 splice, no sequence_end between -> 0
    seq(1); pic(3, 1, rgb=1)
    seq(mpeg1=True, w=352, h=240); pic(4, 0, mpeg1=True); pic(4, 0, mpeg1=True)
    # and back: MPEG-1 -> tagged MPEG-2 re-tags
    seq(6); pic(3, 6, rgb=1)
    b.start(0xB7)                        # sequence_end_code
    es = b.data()
    es += b'\x00' * 32                   # getbits_fifo starves a few words before EOS
    es += b'\x00' * (-len(es) % 8)
    return es, exp


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    es, exp = build()
    with open(sys.argv[1] + '_es.hex', 'w') as f:
        f.write('\n'.join(es[i:i + 8].hex() for i in range(0, len(es), 8)) + '\n')
    with open(sys.argv[1] + '_exp.hex', 'w') as f:
        f.write('\n'.join('%04x' % e for e in exp) + '\n')
    print('colour_matrix_es: %d bytes, %d pictures' % (len(es), len(exp)))


if __name__ == '__main__':
    main()
