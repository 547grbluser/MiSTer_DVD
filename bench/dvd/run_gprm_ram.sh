#!/usr/bin/env bash
# run_gprm_ram.sh -- the GPRMs as a RAM (docs/logic_reclaim.md §9).
#
#   bench/dvd/run_gprm_ram.sh          GREEN: dvd_vm_tb + the access gate
#   bench/dvd/run_gprm_ram.sh --red    + one mutation per RAM mechanism, each
#                                      caught by the arm written for it
#
# The move to an M10K replaced one-cycle combinational reads and writes with an
# operand prefetch, a registered write request, forwarding, a two-cycle swap, a
# read-modify-write tick and a clear walk. Each is a way to be wrong that the
# flop version could not be, so each gets a mutation.
set -u
cd "$(dirname "$0")/../.."
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
rc=0
pass()   { echo "  ok   $*"; }
failed() { echo "  FAIL $*"; rc=1; }
iv() { iverilog -g2012 -Y .sv -Y .v -y dvd -I dvd -o "$1" "$2" bench/dvd/dvd_vm_tb.sv > "$1.log" 2>&1; }

echo "== GREEN"
if iv "$TMP/g" dvd/dvd_vm.sv && vvp "$TMP/g" > "$TMP/g.out" 2>&1 && grep -q "ALL TESTS PASS" "$TMP/g.out"; then
    pass "dvd_vm_tb"
else
    failed "dvd_vm_tb"; grep -E "^FAIL" "$TMP/g.out" | head -5
fi
if python3 tools/check_gprm_ram.py > "$TMP/c.out" 2>&1; then pass "$(cat "$TMP/c.out")"
else failed "check_gprm_ram.py"; cat "$TMP/c.out"; fi

if [ "${1:-}" != "--red" ]; then
    [ $rc -eq 0 ] && echo "run_gprm_ram: ALL GREEN" || echo "run_gprm_ram: FAILURES"
    exit $rc
fi

echo "== RED (each mutation must be caught by its own arm)"
mutate() {   # $1 label, $2 sed expr, $3 expected FAIL text
    sed "$2" dvd/dvd_vm.sv > "$TMP/$1.sv"
    if cmp -s dvd/dvd_vm.sv "$TMP/$1.sv"; then failed "$1: the mutation did not apply (anchor moved)"; return; fi
    if ! iv "$TMP/$1" "$TMP/$1.sv"; then failed "$1: mutant did not compile"; head -3 "$TMP/$1.log"; return; fi
    vvp "$TMP/$1" > "$TMP/$1.out" 2>&1
    if grep -q "ALL TESTS PASS" "$TMP/$1.out"; then failed "$1: dvd_vm_tb PASSED the mutant"
    elif grep -q -e "$3" "$TMP/$1.out"; then pass "$1 -> caught by \"$3\""
    else failed "$1: failed, but not by \"$3\""; grep -E "^FAIL" "$TMP/$1.out" | head -3; fi
}
# Type 4 compares AFTER its own set: the operand register must be forwarded.
mutate F1-no-forward 's/^\( *\)if (opi_A == set_sel_reg) opA <= set_wd;/\1;/' "FAIL: t4_inc_hits"
# Swap is two writes through one port; lose the second.
mutate F2-swap-one-write "s/sw_pend <= 1'b1; sw_wa <= set_sel_reg;/sw_pend <= 1'b0; sw_wa <= set_sel_reg;/" "FAIL: T6s: swap g1"
# The 1 Hz counter tick is now read-modify-write; lose the write.
mutate F3-tick-no-write "s/g_we <= 1'b1; g_wa <= tick_i; g_wd <= g_qa + 16'd1;/g_we <= 1'b0; g_wa <= tick_i; g_wd <= g_qa + 16'd1;/" "FAIL: T1: g13 != 8"
# A mount must still clear the GPRMs, now by a walk.
mutate F4-no-mount-clear "s|clr_busy <= 1'b1; clr_i <= 4'd0;     // the RAM clear walk (see reset)|clr_busy <= 1'b0; clr_i <= 4'd0;|" "FAIL: T7c"
# The operand capture is one slot behind its address; get that off by one.
mutate F5-capture-skew "s/^\( *\)3'd1: opA <= g_qa;/\13'd2: opA <= g_qa;/" "FAIL: and_link_taken"

echo "== RED: the access gate"
if python3 tools/check_gprm_ram.py --red > "$TMP/r.out" 2>&1; then sed 's/^/  /' "$TMP/r.out" | sed 's/^  /  ok   /;s/ok     ok/ok/'
else failed "check_gprm_ram.py --red"; cat "$TMP/r.out"; fi

[ $rc -eq 0 ] && echo "run_gprm_ram: ALL GREEN" || echo "run_gprm_ram: FAILURES"
exit $rc
