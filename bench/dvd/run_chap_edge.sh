#!/usr/bin/env bash
# run_chap_edge.sh — Next/Prev chapter at the TITLE EDGE (audit item 7).
#
# With Disc Menus on, a chapter burst with nowhere left to go in the title is
# handed to the VM instead of clamping (libdvdnav vm_jump_next_pg/prev_pg):
#   Next from the last chapter      -> the VM runs the PGC's POST
#   Prev at the start of chapter 1  -> the VM follows prev_pgcn to its LAST
#                                      program (a prev_pgcn naming this PGC, or
#                                      0, restarts chapter 1 as before)
# See docs/dvd_nav.md "Chapter skip at the title's edges".
#
# GREEN: iso_reader_chapedge_tb (the reader side, arms A-L)
#        dvd_vm_tb              (the VM side, S27 arms V1-V7)
#        + the chapter/PTT regressions that share the resolve path:
#          iso_reader_chapter_tb, iso_reader_ptt_tb, iso_reader_linkptt_tb,
#          iso_reader_autoptt_tb
# RED  : sed-mutated copies of the RTL. Each must fail exactly its own arms.
#   reader (iso_reader_chapedge_tb):
#     R1 no-gr-emit   : CH_GR legacy branch never emits      -> A D E J
#     R2 no-vm-gate   : emits with Disc Menus off            -> B
#     R3 overshoot    : an overshooting Next is an edge      -> C
#     R4 no-self      : prev_pgcn == this PGC is followed    -> F
#     R5 no-prev0     : prev_pgcn == 0 is followed           -> G
#     R6 no-dec       : one Prev mid-chapter 1 is an edge    -> H
#     R7 no-sentinel  : pgn 0xFF is not "the last program"   -> I
#     R8 no-arm       : single-chapter titles never arm      -> J
#     R9 no-r-emit    : CH_R (no PTT table) never emits      -> K L
#   VM (dvd_vm_tb, S27 arms; the runner reads ^FAIL S27-Vn):
#     (listed with the VM mutations below)
#
# Usage: bench/dvd/run_chap_edge.sh [--red]
set -u
cd "$(dirname "$0")/../.."
RED=0; [ "${1:-}" = "--red" ] && RED=1
RD="dvd/dvd_iso_reader.sv"
VM="dvd/dvd_vm.sv"
OUT=".sim/chap_edge"; mkdir -p "$OUT"
fail=0

# run <name> <pass-string> <tb> <rtl...>
run() {
    local name=$1 pass=$2 tb=$3; shift 3
    if iverilog -g2012 -y dvd -Y .sv -o "$OUT/$name" "$@" "$tb" 2>"$OUT/$name.build"; then
        if timeout 900 vvp "$OUT/$name" > "$OUT/$name.log" 2>&1 && grep -q "$pass" "$OUT/$name.log"; then
            echo "  PASS $name"
        else
            echo "  FAIL $name"; grep -E "FAIL|TIMEOUT" "$OUT/$name.log" | head -20; fail=1
        fi
    else
        echo "  FAIL $name (build)"; sed 's/^/      /' "$OUT/$name.build" | head -20; fail=1
    fi
}

echo "== GREEN =="
run chapedge "ISO_READER_CHAPEDGE_TB: ALL TESTS PASSED" bench/dvd/iso_reader_chapedge_tb.sv $RD
grep -E 'PASS$' "$OUT/chapedge.log" | sed 's/^/    /'
run vm       "ALL TESTS PASS (dvd_vm_tb)"               bench/dvd/dvd_vm_tb.sv $VM
grep -E '^ *(ok|PASS).*S27' "$OUT/vm.log" | sed 's/^/    /'
run chapter  "ISO_READER_CHAPTER_TB: ALL TESTS PASSED"  bench/dvd/iso_reader_chapter_tb.sv $RD
run ptt      "ISO_READER_PTT_TB: ALL TESTS PASSED"      bench/dvd/iso_reader_ptt_tb.sv $RD
run linkptt  "ALL TESTS PASSED"                         bench/dvd/iso_reader_linkptt_tb.sv $RD
run autoptt  "ISO_READER_AUTOPTT_TB: ALL TESTS PASSED"  bench/dvd/iso_reader_autoptt_tb.sv $RD

if [ $RED -eq 1 ]; then
    echo "== RED (each mutation must fail exactly its arms) =="
    # mutate <name> <rtl> <tb> <arm-regex> <expected arms> <sed-expr>
    mutate() {
        local name=$1 rtl=$2 tb=$3 rx=$4 want=$5 expr=$6
        local src="$OUT/$name.sv"
        sed "$expr" "$rtl" > "$src"
        if cmp -s "$rtl" "$src"; then
            echo "  FAIL $name: the sed matched nothing (mutation is stale)"; fail=1; return
        fi
        if ! iverilog -g2012 -y dvd -Y .sv -o "$OUT/$name" "$src" "$tb" 2>"$OUT/$name.build"; then
            echo "  FAIL $name (build)"; sed 's/^/      /' "$OUT/$name.build" | head; fail=1; return
        fi
        timeout 900 vvp "$OUT/$name" > "$OUT/$name.log" 2>&1
        local got
        got=$(grep -oE "^FAIL $rx" "$OUT/$name.log" | awk '{print $2}' | sed 's/:$//' \
              | sort -u | tr '\n' ' ' | sed 's/ $//')
        if [ "$got" = "$want" ]; then
            echo "  ok   $name -> fails [$got]"
        else
            echo "  FAIL $name: failed [$got], expected [$want]"; fail=1
        fi
    }
    RTB=bench/dvd/iso_reader_chapedge_tb.sv
    RX='[A-L]'
    mutate R1_no_gr_emit  $RD $RTB "$RX" "A D E J" \
        "s/^                    if (chap_edge_go) begin/                    if (1'b0) begin/"
    mutate R2_no_vm_gate  $RD $RTB "$RX" "B" \
        "s/wire       chap_edge_go  = vm_mode \&\&/wire       chap_edge_go  = 1'b1 \&\&/"
    mutate R3_overshoot   $RD $RTB "$RX" "C" \
        "s/chap_nx_edge  = chap_dir_l \&\& !(chap_best + 7'd1 < cmd_nr_pgm\[6:0\]);/chap_nx_edge  = chap_dir_l \&\& (chap_next_sum > {1'b0, chap_last});/"
    mutate R4_no_self     $RD $RTB "$RX" "F" \
        "s/ \&\& (prev_pgcn != cur_pgcn)//"
    mutate R5_no_prev0    $RD $RTB "$RX" "G" \
        "s/(prev_pgcn != 16'd0) \&\& (prev_pgcn != cur_pgcn)/(prev_pgcn != cur_pgcn)/"
    mutate R6_no_dec      $RD $RTB "$RX" "H" \
        "s/(chap_best == 7'd0) \&\& (chap_dec != 5'd0) \&\&/(chap_best == 7'd0) \&\&/"
    mutate R7_no_sentinel $RD $RTB "$RX" "I" \
        "s/(jpgn_l == 8'hFF \&\& walk_left == 13'd1)/1'b0/"
    mutate R8_no_arm      $RD $RTB "$RX" "J" \
        "s/ || (vm_mode \&\& cmd_nr_pgm != 8'd0)//"
    mutate R9_no_r_emit   $RD $RTB "$RX" "K L" \
        "s/^                end else if (chap_edge_go) begin/                end else if (1'b0) begin/"
fi

if [ $fail -eq 0 ]; then echo "RUN_CHAP_EDGE: PASS"; else echo "RUN_CHAP_EDGE: FAIL"; exit 1; fi
