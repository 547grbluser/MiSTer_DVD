// dvd/dts/cb_host_ram.sv -- half of the DTS ADPCM codebook, held as a RAM's power-up
// contents until dts_cb_mem copies it to DDR3 (docs/dts_decoder.md D4)
//
// D4 put the codebooks in the power-up contents of three FIFOs that existed anyway:
// audio_ring's, lpcm_unpack's and mp2_decode's PCM FIFO. MP2 moved onto the shared
// engine (docs/mp2_engine.md M3) and plays out of lpcm_unpack's FIFO like DTS, so
// mp2_decode is gone and its FIFO with it. This is that FIFO's read side alone, with
// the same contract dts_cb_mem has with every host: cp_q is the word at the read
// pointer (one cycle after it moves), cp_step advances the pointer, and the reset
// returns it to 0 (the copier's host_rst after the copy). It is never written: Quartus
// infers a ROM from the init file.
//
// Its words are idle after the copy. A later feature can give the block a job (an
// output FIFO, as it was) without moving the codebook, as long as the copy runs first.

`default_nettype none

module cb_host_ram #(
    parameter int AW      = 12,              // 2^AW words of 32 bits
    parameter     CB_INIT = ""               // "" = none (a bench that does not copy)
) (
    input  wire         clk,
    input  wire         rst,                 // synchronous, active high
    input  wire         cp_step,
    output logic [31:0] cp_q
);
    (* ramstyle = "M10K" *) logic [31:0] mem [0:(1 << AW) - 1];
    initial if (CB_INIT != "") $readmemh(CB_INIT, mem);

    logic [AW-1:0] rp;
    always_ff @(posedge clk) begin
        if (rst) rp <= '0;
        else if (cp_step) rp <= rp + 1'b1;
        cp_q <= mem[rp];
    end
endmodule

`default_nettype wire
