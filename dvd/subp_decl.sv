// subp_decl.sv -- which of the 32 subpicture streams the loaded PGC DECLARES
// Part of MiSTer DVD Player Core (feature/subp-32; docs/track_selection.md
// "32 subtitle tracks").
//
// DVD-Video allows 32 subpicture streams; the PGC's subp_control[32] table says
// which exist (bit 31 of each word). emu keeps the 32 words themselves in a block
// RAM (subp_ctl_ram) for the one per-cycle read the substream map needs, and this
// module keeps only the 32 DECLARED bits, because three consumers need all of them
// at once:
//
//   * the Subtitle button steps over declared streams only: OFF -> first declared
//     -> next declared above the current one -> ... -> OFF. The IFO stream count is
//     an authoring-tool claim (140/1,431 library discs say 32 while their PGCs
//     declare a handful; Universal's copy-protection decoy title sets declare 32
//     streams of one unit each), so stepping by it would walk through empty tracks.
//   * the forced-subtitle fallback is the first declared stream (libdvdnav's
//     vm_get_subp_active_stream);
//   * the popup's "SUB n/N" total is the highest declared stream + 1.
//
// Pure bookkeeping: one 32-bit register written from the reader's pgc_ctl bus,
// three priority encoders as plain for-loops (no casts, no functions -- the
// Quartus-17 lessons in CLAUDE.md), and a mask. Bench: bench/dvd/subp_decl_tb.sv.

`default_nettype none

module subp_decl (
    input  wire        clk,
    // the reader's PGC stream-control bus: waddr 0..31 = subp_control words
    input  wire        ctl_we,
    input  wire [5:0]  ctl_waddr,
    input  wire        ctl_wbit31,     // pgc_ctl_wdata[31] = "this stream is declared"
    input  wire [4:0]  cur,            // the current logical stream (emu: sp_sel)
    output reg  [31:0] declared,
    output wire        any,            // the PGC declares at least one stream
    output reg  [4:0]  first,          // lowest declared stream (0 when none)
    output reg  [4:0]  last,           // highest declared stream (0 when none)
    output wire        next_ok,        // some stream ABOVE cur is declared
    output reg  [4:0]  next            // the lowest declared stream above cur
);
    always @(posedge clk)
        if (ctl_we && !ctl_waddr[5]) declared[ctl_waddr[4:0]] <= ctl_wbit31;

    assign any = |declared;

    // the declared streams strictly above cur: clear bits 0..cur. (2 << 31 overflows
    // to 0 in 32 bits, so cur = 31 leaves nothing above -- correct.)
    wire [31:0] above = declared & ~((32'd2 << cur) - 32'd1);
    assign next_ok = |above;

    always @* begin
        first = 5'd0;
        last  = 5'd0;
        next  = 5'd0;
        for (int i = 31; i >= 0; i = i - 1) begin
            if (declared[i]) first = i[4:0];
            if (above[i])    next  = i[4:0];
        end
        for (int j = 0; j < 32; j = j + 1)
            if (declared[j]) last = j[4:0];
    end
endmodule

`default_nettype wire
