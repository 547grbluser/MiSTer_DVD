// player_regs.sv -- the DVD player parameters a disc can READ: SPRM14 (video
// preference), SPRM15 (audio capabilities), SPRM20 (player region).
// Part of MiSTer DVD Player Core (feature/player-regs; docs/dvd_vm.md "Player
// parameters SPRM14/15/20").
//
// These were constants in dvd_vm's sprm_read (0x0100 / 0x7CFC / 0x0001), copied from
// libdvdnav. The 2026-10-01 DVD Demystified audit measured what discs do with them:
//   * SPRM20 -- 507/1,430 library discs read it; on 65/120 tested, a different value
//     takes a different boot path, typically a dead-end "wrong region" still. The
//     constant 0x0001 is REGION 1, not "region-free".
//   * SPRM14 -- 30 discs read it; 10 boot a different VTS (MGM-style discs pick a 4:3
//     or 16:9 version of their intro from it). The constant claimed "4:3 TV, pan&scan"
//     even on a 16:9 HDMI setup.
//   * SPRM15 -- 6 discs read it; the constant claimed SDDS and every karaoke mode.
//
// SPRM20 (user decision 2026-10-01): the FIRST REGION THE DISC ALLOWS, from VMGI
// vmg_category byte 0x23 (bit n set = region n+1 PROHIBITED) -- the 3rd edition's
// "autoswitching player" (p. 5-21). A disc that allows every region (mask 0) reads
// region 1, exactly as before. A mask that prohibits EVERY region (an anti-autoswitch
// "smart disc" trick) falls back to region 1 and raises rmask_all_prohibited, so the
// case is visible rather than silently mapped.
//
// SPRM14 (user decision 2026-10-04, "Analog Aspect decides"). DVD-Video bits 10-11 =
// the player's preferred DISPLAY aspect (0 = 4:3, 3 = 16:9), bits 8-9 = the current
// output mode for 16:9 content on a 4:3 display (0 normal/wide, 1 pan&scan, 2 letterbox):
//     HDMI / progressive output          -> 16:9 TV, wide       0x0C00
//     analog/interlaced, Auto/Letterbox  -> 4:3 TV, letterbox   0x0200
//     analog/interlaced, Crop            -> 4:3 TV, pan&scan    0x0100
//     analog/interlaced, Fit             -> 16:9 TV, wide       0x0C00
// (Fit applies no correction, which is only right for widescreen content on a
// widescreen set.) aa_sel is emu's aa_osd_sel, so the B15 Aspect button counts.
// aa_live is emu's "Analog Aspect is in force" gate -- the SAME predicate that
// enables Letterbox/Crop (today interlaced_eff), so if Analog Aspect is ever
// extended to other rasters (4:3 progressive displays), SPRM14 follows with it.
//
// SPRM15: b14 Dolby Digital, b12 MPEG and b11 DTS. DTS decodes in fabric since
// PRs #148/#149 once its codebooks are loaded (dts_ok), and Passthru always carries
// it. SDDS (b10) and the karaoke bits (b2..b7) are clear: the core supports neither.
//
// Pure combinational. Bench: bench/dvd/player_regs_tb.sv.

`default_nettype none

module player_regs (
    input  wire [7:0]  rmask,          // VMGI vmg_category byte 0x23 (1 = prohibited)
    input  wire        aa_live,        // emu: Analog Aspect is in force (today interlaced_eff)
    input  wire [1:0]  aa_sel,         // emu aa_osd_sel: 0 Auto, 1 Fit, 2 Letterbox, 3 Crop
    input  wire        pass_mode,      // Audio Out = Passthru
    input  wire        dts_ok,         // DTS codebooks loaded (in-fabric DTS decode works)
    output reg  [15:0] sprm14,
    output wire [15:0] sprm15,
    output reg  [15:0] sprm20,
    output wire        rmask_all_prohibited
);
    // ---- SPRM20: lowest allowed region, one-hot (mask 0 -> region 1) ----
    assign rmask_all_prohibited = (rmask == 8'hFF);
    always @* begin
        sprm20 = 16'h0001;                       // also the all-prohibited fallback
        for (int r = 7; r >= 0; r = r - 1)
            if (!rmask[r]) sprm20 = 16'h0001 << r;
    end

    // ---- SPRM14: preferred display aspect + current 4:3 output mode ----
    always @* begin
        if (!aa_live)          sprm14 = 16'h0C00;   // HDMI/progressive: 16:9 TV
        else case (aa_sel)
            2'd1:                  sprm14 = 16'h0C00;   // Fit: a widescreen set
            2'd3:                  sprm14 = 16'h0100;   // Crop: 4:3 TV, pan&scan
            default:               sprm14 = 16'h0200;   // Auto / Letterbox: 4:3 TV, letterbox
        endcase
    end

    // ---- SPRM15: audio capabilities ----
    assign sprm15 = {1'b0, 1'b1, 1'b0, 1'b1, (pass_mode | dts_ok), 11'd0};
endmodule

`default_nettype wire
