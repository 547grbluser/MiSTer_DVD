#!/usr/bin/env bash
# run_dts_seq.sh -- the DTS sequencer gate (dvd/dts/dts_seq.sv, bench/dvd/dts_seq_tb.sv;
# goldens tools/dts_golden.py <- the emulator tools/dts_isa.py, itself checked against
# tools/dts_fixed.py on the same frames). docs/dts_decoder.md sec 10.
#
# GREEN: every register write, store and XQ code, in program order, identical to the
# emulator, and the frame / refusal / overrun / lenient / CNT counts:
#   every stream of the gate set (DTS_GATE_DIR, default ~/dts-streams/gate), 4 frames:
#     the written spec maxima, joint intensity, all ten AMODEs, Huffman sample codes,
#     block and raw codes, Shadoan's lenient block codes, embedded downmix (CNT)
#   R1 T2, frame 1's last DSYNC corrupted: refused after half its PCM, with input
#      stalls (+stall=20) and XQ back-pressure (+xstall=5)
#   R2 T2, frame 1 delivered 300 bytes short: read past its end as zeros, refused; the
#      next frame proves none of its bytes were taken
#   R3 Shadoan with stalls (+stall=40 +xstall=8)
#
# --red [MUTS="Q1 Q4" to run only those]: mutations on a COPY of the RTL, each caught
# by its own arm:
#   Q1  ALU sub computes add                          (T2)        -> [trace]
#   Q2  bri's 10-bit constant is not sign-extended    (T2)        -> [trace]
#       (a signed lt/ge taken unsigned is NOT observable by this program: its only
#       negative comparands are error checks that refuse either way, e.g. a scale
#       index < 0 is also >= 64 unsigned. A microcode change that relies on a signed
#       compare of a negative value needs its own arm.)
#   Q3  the bit reader serves a byte's LSB first      (T2)        -> [trace]
#   Q4  the tree walk swaps a node's children         (T2)        -> [trace]
#   Q18 a child offset is taken from the root, not the node (synth 768k: deep
#       Huffman sample codes; T2 never steps past a root's children) -> [trace]
#   Q5  CALL pushes its own address                   (T2)        -> [trace] / [hang]
#   Q6  BPOS counts bytes, not bits                   (misc: aux) -> [trace]
#   Q7  a load returns the address, not the data      (T2)        -> [trace]
#   Q8  FEND drains one byte short                    (T2)        -> [trace] / [count] / [hang]
#   Q9  a block-code digit's offset is one too small  (T2)        -> [trace]
#   Q10 an overflowed block code is not counted       (Shadoan)   -> [count]
#   Q11 raw codes are not sign-extended               (A.I. 1536k)-> [trace]
#   Q12 ERR drops the untaken bytes without reading them (R1)     -> [trace] / [count] / [hang]
#   Q13 bits past the frame come from the next frame  (R2)        -> [trace] / [count] / [hang]
#   Q14 XQ's Huffman book ignores the selector        (synth 768k)-> [trace]
#   Q15 the in-place division reads the next bit up   (T2)        -> [trace]
#   Q16 an overrun bit is not counted                 (R2)        -> [count]
#   Q19 a branch to an error vector is not an err     (R1)        -> [trace] / [count] / [hang]
#   Q17 a VLC symbol is zero-extended (amode9: only the written streams code a
#       negative scale delta; no disc or encoder stream does) -> [trace]
set -u
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
GATE=${DTS_GATE_DIR:-$HOME/dts-streams/gate}
GEN=.sim/dts/seq
mkdir -p "$GEN"
fail=0
red=0; [ "${1:-}" = "--red" ] && red=1
RTL=dvd/dts/dts_seq.sv
TB=bench/dvd/dts_seq_tb.sv
JOBS=${JOBS:-$(nproc)}

echo "== GREEN: the microcode and its tables are current =="
python3 tools/dts_isa.py --asm --check | sed 's/^/  /' || fail=1

shopt -s nullglob
STREAMS=("$GATE"/*.dts)
if [ ${#STREAMS[@]} -eq 0 ]; then
  echo "FAIL: no streams in $GATE (set DTS_GATE_DIR; tools/gen_dts_fixtures.py builds them)"
  exit 1
fi
T2=$GATE/disc_t2_sample.dts

# arm | stream | golden args | sim args
ARMS=()
for s in "${STREAMS[@]}"; do ARMS+=("$(basename "$s" .dts)|$s||"); done
ARMS+=("R1|$T2|--frames 3 --refuse 1|+stall=20 +xstall=5")
ARMS+=("R2|$T2|--frames 3 --truncate 1:300|")
ARMS+=("R3|$GATE/disc_blockoverflow_shadoan.dts||+stall=40 +xstall=8")
declare -A STEM SIMARGS

echo "== GREEN: goldens (${#ARMS[@]} arms) =="
n=0
for a in "${ARMS[@]}"; do
  IFS='|' read -r name src gargs sargs <<<"$a"
  STEM[$name]=$GEN/$name; SIMARGS[$name]=$sargs
  if [ ! -f "$src" ]; then echo "  FAIL $name: no $src"; fail=1; STEM[$name]=; continue; fi
  # shellcheck disable=SC2086
  python3 tools/dts_golden.py "$src" --out "${STEM[$name]}" --frames 4 $gargs \
      > "${STEM[$name]}.glog" 2>&1 &
  n=$((n + 1)); [ $((n % JOBS)) -eq 0 ] && wait
done
wait
for a in "${ARMS[@]}"; do
  IFS='|' read -r name _ <<<"$a"
  [ -z "${STEM[$name]}" ] && continue
  if ! grep -q "matches the model" "${STEM[$name]}.glog"; then
    echo "  FAIL $name golden: $(tail -1 "${STEM[$name]}.glog")"; fail=1; STEM[$name]=
  fi
done

iverilog -g2012 -o .sim/dts/seq_sim $RTL $TB 2>&1 | grep -v "sorry" | grep . && { echo "  FAIL build"; exit 1; }
echo "== GREEN: the sequencer =="
n=0
for a in "${ARMS[@]}"; do
  IFS='|' read -r name _ <<<"$a"
  [ -z "${STEM[$name]}" ] && continue
  # shellcheck disable=SC2086
  vvp -n .sim/dts/seq_sim +stem="${STEM[$name]}" ${SIMARGS[$name]} 2>&1 \
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
    (cd "$d" && timeout 600 vvp -n sim +stem="$ROOT/${STEM[$arm]}" ${SIMARGS[$arm]} 2>&1 \
        | grep -v 'readmem\|Not enough' > log)
    if grep -q "^PASS: dts_seq_tb" "$d/log"; then
      { echo "  $name: SURVIVED -- the bench cannot see this defect"; echo MUTFAIL; } > "$RES/$name"
    elif grep -qE "FAIL $pat" "$d/log"; then
      echo "  $name: caught by $(grep -m1 -E "FAIL $pat" "$d/log" | cut -c1-100)" > "$RES/$name"
    else
      { echo "  $name: failed but NOT in the expected arm ($pat):"; grep -m2 FAIL "$d/log"; echo MUTFAIL; } > "$RES/$name"
    fi
    rm -rf "$d"
  }
  T=disc_t2_sample
  mut Q1 $T "4'd1:  alu_y = vrs - alu_b;" "4'd1:  alu_y = vrs + alu_b;" '\[trace\]' &
  mut Q2 $T "wire [15:0] br_b  = (op == O_BRI) ? {{6{rt[3]}}, rt, aux} : vrt;" "wire [15:0] br_b  = (op == O_BRI) ? {6'd0, rt, aux} : vrt;" '\[trace\]' &
  mut Q3 $T "bit_ok = 1'b1; bit_val = cur[cur_n - 4'd1];" "bit_ok = 1'b1; bit_val = cur[4'd8 - cur_n];" '\[trace\]' &
  mut Q4 $T "wire   [8:0] h_ent   = bit_val ? hnode_q[17:9] : hnode_q[8:0];" "wire   [8:0] h_ent   = bit_val ? hnode_q[8:0] : hnode_q[17:9];" '\[trace\]' &
  wait
  mut Q5 $T "O_CALL: begin stack[sp] <= pc + 10'd1;" "O_CALL: begin stack[sp] <= pc;" '\[(trace|hang)\]' &
  mut Q6 written_misc "O_BPOS: begin w_en = 1'b1; w_val = fbits[15:0]; end" "O_BPOS: begin w_en = 1'b1; w_val = {3'd0, fbits[15:3]}; end" '\[trace\]' &
  mut Q7 $T "S_LD:    begin w_en = 1'b1; w_val = ld_const ? crom_q : rec_q; end" "S_LD:    begin w_en = 1'b1; w_val = maddr; end" '\[trace\]' &
  mut Q8 $T "in_ready = ftaken != fbytes;" "in_ready = ftaken + 16'd1 < fbytes;" '\[(trace|count|hang)\]' &
  wait
  mut Q9 $T "wire  [5:0] x_digit  = {1'b0, x_rem_n} - {2'b0, x_off};" "wire  [5:0] x_digit  = {1'b0, x_rem_n} - {2'b0, x_off} + 6'd1;" '\[trace\]' &
  mut Q10 disc_blockoverflow_shadoan "if (x_dq_w != 19'd0) lenient <= 1'b1;" "if (1'b0) lenient <= 1'b1;" '\[count\]' &
  mut Q11 disc_1536k_transient_ai "x_acc <= (x_it == x_n) ? {24{bit_val}} : {x_acc[22:0], bit_val};" "x_acc <= (x_it == x_n) ? {23'd0, bit_val} : {x_acc[22:0], bit_val};" '\[trace\]' &
  mut Q12 R1 "cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b1;" "cur_n <= 4'd0; in_frame <= 1'b0; drain_err <= 1'b1; ftaken <= fbytes;" '\[(trace|count|hang)\]' &
  wait
  mut Q13 R2 "wire         zero_fill = in_frame && (ftaken == fbytes);" "wire         zero_fill = 1'b0;" '\[(trace|count|hang)\]' &
  mut Q14 synth_stereo_768k "wire  [5:0] x_book  = XQ_QBOOK[6 * x_abm1 +: 6] + {3'd0, x_sel};" "wire  [5:0] x_book  = XQ_QBOOK[6 * x_abm1 +: 6];" '\[trace\]' &
  mut Q18 synth_stereo_768k "wire  [11:0] h_child = h_cur + {4'd0, h_ent[7:0]};" "wire  [11:0] h_child = h_root + {4'd0, h_ent[7:0]};" '\[trace\]' &
  mut Q19 R1 "wire br_err = ((op == O_BR) || (op == O_BRI)) && taken && (imm[9:5] == UC_ERRV);" "wire br_err = 1'b0;" '\[(trace|count|hang)\]' &
  mut Q15 $T "x_dbit = x_dq[x_itm1];" "x_dbit = x_dq[x_it];" '\[trace\]' &
  mut Q16 R2 "else if (zero_fill) overrun_bit <= 1'b1;" "else if (zero_fill) overrun_bit <= 1'b0;" '\[count\]' &
  mut Q17 written_amode9 "S_VLC_W: begin w_en = 1'b1; w_val = {{8{v_sym[7]}}, v_sym}; end" "S_VLC_W: begin w_en = 1'b1; w_val = {8'd0, v_sym}; end" '\[trace\]' &
  wait
  for f in $(ls "$RES" | sort -V); do grep -v '^MUTFAIL$' "$RES/$f"; grep -q '^MUTFAIL$' "$RES/$f" && fail=1; done
  rm -rf "$RES"
fi

[ $fail -eq 0 ] && echo "== ALL GREEN ==" || echo "== FAILURES =="
exit $fail
