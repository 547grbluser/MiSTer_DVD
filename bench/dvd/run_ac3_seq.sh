#!/usr/bin/env bash
# run_ac3_seq.sh -- the engine's sequencer running AC-3's program (dvd/dts/dts_seq.sv with
# codec = 1, bench/dvd/dts_seq_tb.sv; goldens tools/ac3_golden.py <- the emulator
# tools/ac3_isa.py, itself checked against tools/ac3_model.py, which is bit-exact with
# dvd/ac3/'s RTL). docs/ac3_engine.md A2.
#
# GREEN: every register write, store, mantissa-unit item (kind 2) and unit record write
# (kind 3), in program order, identical to the emulator, and the frame / refusal /
# overrun counts:
#   every stream of tools/test_ac3_model.py's set (tools/streams, bench/ac3/vectors and
#     $AC3_TEST_DIR, default ~/ac3-streams/gate), 4 frames (a *refuse* window 5: the
#     refused frame and one after it, so the restart is scored too)
#   S1 noise 5.1 (the worst frame) with input stalls and item back-pressure
#   S2 the invalid grouped-code window (E_GROUP mid-AQ) with stalls and back-pressure
#   R1 tone 5.1, frame 1's exponent codes rewritten: EXPD refuses (E_EXP)
#   T1 tone 5.1, frame 1 delivered 200 bytes short: read past its end as zeros (counted)
#
# --red [MUTS="X1 X5" to run only those]: mutations on a COPY of the RTL, each caught by
# its own arm (the arms are in the mut lines below).
set -u
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
GATE=${AC3_TEST_DIR:-$HOME/ac3-streams/gate}
GEN=.sim/ac3/seq
mkdir -p "$GEN"
fail=0
red=0; [ "${1:-}" = "--red" ] && red=1
RTL=dvd/dts/dts_seq.sv
TB=bench/dvd/dts_seq_tb.sv
JOBS=${JOBS:-$(nproc)}

echo "== GREEN: the microcode and its tables are current =="
python3 tools/dts_isa.py --asm --check | sed 's/^/  /' || fail=1

shopt -s nullglob globstar
STREAMS=(tools/streams/*.ac3 bench/ac3/vectors/*.ac3 "$GATE"/**/*.ac3)
if [ ${#STREAMS[@]} -eq 0 ]; then echo "FAIL: no AC-3 streams"; exit 1; fi
TONE=tools/streams/tone_5p1_48k_192k.ac3
NOISE=tools/streams/noise_5p1_48k_640k.ac3
GROUP=$(ls "$GATE"/*refuse_unmodelled*.ac3 2>/dev/null | head -1)
# a mutation's stream: the first window whose first 4 frames USE the feature (the model's
# own counters), not the first whose name says so -- a window's name is the feature the
# scan extracted it for, and a 4-frame cut can miss it
pick() {
  python3 - "$@" <<'PYEOF'
import sys
sys.path.insert(0, 'tools')
import ac3_model as M
feat = sys.argv[1]
for p in sys.argv[2:]:
    d = M.Decoder()
    try:
        for k, (_, fr) in enumerate(M.frames(open(p, 'rb').read())):
            if k >= 4:
                break
            d.frame(fr)
    except M.Ac3Error:
        pass
    if d.stats.get(feat):
        print(p)
        break
PYEOF
}
ZSNR=$(pick zero_snr "$GATE"/*.ac3)
PHS=$(pick phsflg "$GATE"/*.ac3)
CPL=$(pick cpl "$GATE"/*.ac3)

# arm | stream | golden args | sim args
ARMS=()
for s in "${STREAMS[@]}"; do
  case "$s" in *refuse*) g="--frames 5";; *) g="--frames 4";; esac
  ARMS+=("$(basename "$s" .ac3)|$s|$g|")
done
ARMS+=("S1|$NOISE|--frames 3|+stall=20 +xstall=5")
ARMS+=("S2|$GROUP|--frames 5|+stall=10 +xstall=4")
ARMS+=("R1|$TONE|--frames 4 --badexp 1|")
ARMS+=("T1|$TONE|--frames 4 --truncate 1:200|")
declare -A STEM SIMARGS

echo "== GREEN: goldens (${#ARMS[@]} arms) =="
n=0
for a in "${ARMS[@]}"; do
  IFS='|' read -r name src gargs sargs <<<"$a"
  STEM[$name]=$GEN/$name; SIMARGS[$name]=$sargs
  if [ ! -f "$src" ]; then echo "  FAIL $name: no $src"; fail=1; STEM[$name]=; continue; fi
  # shellcheck disable=SC2086
  python3 tools/ac3_golden.py "$src" --out "${STEM[$name]}" $gargs > "${STEM[$name]}.glog" 2>&1 &
  n=$((n + 1)); [ $((n % JOBS)) -eq 0 ] && wait
done
wait
for a in "${ARMS[@]}"; do
  IFS='|' read -r name _ <<<"$a"
  [ -z "${STEM[$name]}" ] && continue
  if ! grep -q "match the model" "${STEM[$name]}.glog"; then
    echo "  FAIL $name golden: $(tail -1 "${STEM[$name]}.glog")"; fail=1; STEM[$name]=
  fi
done
# the arms must carry what they are named for
for chk in "R1|refused {9: 1}" "S2|refused {10: 1}" "T1|[1-9][0-9]* overrun bits"; do
  IFS='|' read -r name pat <<<"$chk"
  [ -n "${STEM[$name]}" ] && ! grep -q "$pat" "${STEM[$name]}.glog" && {
    echo "  FAIL $name golden does not carry '$pat': $(tail -1 "${STEM[$name]}.glog")"; fail=1; }
done

mkdir -p .sim/dts
iverilog -g2012 -o .sim/dts/ac3_seq_sim $RTL $TB 2>&1 | grep -v "sorry" | grep . && { echo "  FAIL build"; exit 1; }
echo "== GREEN: the sequencer (codec = AC-3) =="
n=0
for a in "${ARMS[@]}"; do
  IFS='|' read -r name _ <<<"$a"
  [ -z "${STEM[$name]}" ] && continue
  # shellcheck disable=SC2086
  vvp -n .sim/dts/ac3_seq_sim +stem="${STEM[$name]}" +codec=1 ${SIMARGS[$name]} 2>&1 \
      | grep -v 'readmem\|Not enough' > "$GEN/$name.log" &
  n=$((n + 1)); [ $((n % JOBS)) -eq 0 ] && wait
done
wait
for a in "${ARMS[@]}"; do
  IFS='|' read -r name _ <<<"$a"
  [ -z "${STEM[$name]}" ] && continue
  if grep -q "^PASS: dts_seq_tb" "$GEN/$name.log"; then
    echo "  PASS $name: $(grep '^dts_seq_tb:' "$GEN/$name.log" | sed 's/^dts_seq_tb: //')"
  else echo "  FAIL $name: $(grep -m1 FAIL "$GEN/$name.log")"; fail=1; fi
done

if [ $red = 1 ]; then
  echo "== MUTATIONS (each must be caught by its own arm) =="
  RES=$(mktemp -d)
  mut() {   # name arm old new arm-pattern
    local name=$1 arm=$2 old=$3 new=$4 pat=$5
    if [ -n "${MUTS:-}" ] && ! echo " $MUTS " | grep -q " $name "; then return; fi
    if [ -z "${STEM[$arm]:-}" ]; then
      { echo "  $name: no golden for arm $arm"; echo MUTFAIL; } > "$RES/$name"; return
    fi
    local d; d=$(mktemp -d)
    mkdir -p "$d/dvd/dts" "$d/bench/dvd"
    cp dvd/dts/*.sv dvd/dts/*.svh dvd/dts/*.mem "$d/dvd/dts/"
    cp $TB "$d/bench/dvd/"
    python3 - "$d/$RTL" "$old" "$new" <<'PYEOF'
import sys
path, old, new = sys.argv[1:4]
s = open(path).read()
assert s.count(old) == 1, f"mutation anchor not unique/found: {old!r}"
open(path, "w").write(s.replace(old, new))
PYEOF
    if [ $? != 0 ] || ! (cd "$d" && iverilog -g2012 -o sim $RTL $TB 2>build.log); then
      { echo "  $name: harness failure (anchor or build)"; echo MUTFAIL; } > "$RES/$name"; rm -rf "$d"; return
    fi
    # shellcheck disable=SC2086
    (cd "$d" && timeout 900 vvp -n sim +stem="$ROOT/${STEM[$arm]}" +codec=1 ${SIMARGS[$arm]} 2>&1 \
        | grep -v 'readmem\|Not enough' > log)
    if grep -q "^PASS: dts_seq_tb" "$d/log"; then
      { echo "  $name: SURVIVED -- the bench cannot see this defect"; echo MUTFAIL; } > "$RES/$name"
    elif grep -qE "FAIL $pat" "$d/log"; then
      echo "  $name: caught by $(grep -m1 -E "FAIL $pat" "$d/log" | cut -c1-110)" > "$RES/$name"
    else
      { echo "  $name: failed but NOT in the expected arm ($pat):"; grep -m2 FAIL "$d/log"; echo MUTFAIL; } > "$RES/$name"
    fi
    rm -rf "$d"
  }
  T=tone_5p1_48k_192k
  Z=$(basename "${ZSNR:-none}" .ac3); P=$(basename "${PHS:-none}" .ac3); C=$(basename "${CPL:-none}" .ac3)
  G=$(basename "${GROUP:-none}" .ac3)
  # EXPD
  mut X1 $T "(u_dig == 2'd0) ? x_dq[3:0] : (u_dig == 2'd1) ? u_mid : u_lo;" \
            "(u_dig == 2'd0) ? u_lo : (u_dig == 2'd1) ? u_mid : x_dq[3:0];" "\[trace\]" &
  mut X2 R1 "end else if (ex_bad) u_ref = 1'b1;" \
            "end else if (ex_bad) begin u_ref = 1'b1; rec_we = 1'b1; rec_wd = {9'd0, ex_e}; end" "\[trace\]" &
  mut X3 R1 "(ex_e > 7'd24)" "(ex_e > 7'd30)" "\[trace\]" &
  mut X4 $T "(rf[11][1:0] == 2'd2) ? 2'd1 : 2'd3;" "(rf[11][1:0] == 2'd2) ? 2'd1 : 2'd2;" "\[trace\]" &
  wait
  # bit allocation
  mut X5 $T "sd_cls <= sd_rst ? 2'd1 : sd_m1 ? 2'd2 : sd_z ? 2'd3 : 2'd0;" \
            "sd_cls <= sd_rst ? 2'd1 : sd_m1 ? 2'd3 : sd_z ? 2'd2 : 2'd0;" "\[trace\]" &
  mut X6 $T "rec_wd = {2'd0, crom_q[5:0], 3'd0, p2_e};" "rec_wd = {2'd0, crom_q[5:0], 8'd0};" "\[trace\]" &
  mut X7 $T "if (!u_more && !p1) begin pc <= pc + 11'd1; state <= S_DEC; end" \
            "if (!u_more) begin pc <= pc + 11'd1; state <= S_DEC; end" "\[trace\]" &
  # BAPZERO's VALUE is a GAP: the gate's one zero-SNR window (DARK PASSENGERS, 114 of
  # 120 blocks) codes every such block with exponent 0 and fresh exponents (bap 0), so
  # its writes are all 0 and neither "the exponent lost" nor "the old bap kept" shows,
  # not in 20 frames either. A zero-SNR block with real exponents, or one reusing a
  # block's that allocated bits, would. X8 scores its range (the last bin dropped).
  mut X8 "$Z" "S_BZ: begin
                p1 <= u_more;" "S_BZ: begin
                p1 <= u_more && (u_k != u_end - 9'd1);" "\[trace\]" &
  wait
  # the mantissa unit
  mut X9 $T "u_dith <= rf[12] != 16'd0; u_cpl <= 1'b0;" \
            "u_dith <= rf[12] != 16'd0; u_cpl <= 1'b0; q1n <= 2'd0; q2n <= 2'd0; q4n <= 1'b0;" "\[trace\]" &
  mut X10 $T "if (a_g1) begin q1a <= u_mid[1:0]; q1b <= u_lo[1:0];" \
             "if (a_g1) begin q1a <= u_lo[1:0]; q1b <= u_mid[1:0];" "\[trace\]" &
  mut X11 "$G" "(x_dq >= {14'd0, x_lev})" "(x_dq > {14'd0, x_lev})" "\[(trace|count|hang)\]" &
  mut X12 $T "a_m <= get_acc << (5'd16 - a_bap[4:0]);" "a_m <= get_acc << (5'd15 - a_bap[4:0]);" "\[trace\]" &
  mut X13 $T "x_emit = 1'b1; x_emit_a = {u_slot, a_k};" "x_emit = 1'b1; x_emit_a = {u_slot + 3'd1, a_k};" "\[trace\]" &
  wait
  # AQC
  mut X14 "$P" "if (u_ch == 3'd1 && u_phs) x_acc <= 24'd0 - x_acc;" "if (1'b0) x_acc <= 24'd0 - x_acc;" "\[trace\]" &
  mut X15 "$C" "x_emit_v = {!u_cpl && u_dith && (a_bap == 6'd0)," "x_emit_v = {(u_cpl || u_dith) && (a_bap == 6'd0)," "\[trace\]" &
  mut X16 "$C" "wire [17:0] co_full = rec_q[5] ?" "wire [17:0] co_full = !rec_q[5] ?" "\[trace\]" &
  mut X17 "$C" "S_AC_6: if (u_ch == u_nf) state <= S_AQ_R;" "S_AC_6: if (u_ch == u_nf - 3'd1) state <= S_AQ_R;" "\[(trace|hang)\]" &
  wait
  # back-pressure and refusal
  mut X18 S1 "S_AQ_E: if (can_out) begin                         // the next bin" "S_AQ_E: if (1'b1) begin                         // the next bin" "\[(trace|count|hang)\]" &
  mut X19 S2 "cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b1; xq_valid <= 1'b0;
            state <= S_DRAIN;" "cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b0; xq_valid <= 1'b0;
            state <= S_DRAIN;" "\[(trace|count|hang)\]" &
  wait
  for f in "$RES"/*; do grep -v MUTFAIL "$f"; grep -q MUTFAIL "$f" && fail=1; done
  rm -rf "$RES"
fi

[ $fail = 0 ] && echo "== ALL GREEN ==" || echo "== FAILURES =="
exit $fail
