#!/usr/bin/env bash
# 32 subtitle tracks (feature/subp-32; docs/track_selection.md "32 subtitle tracks").
#
#   bench/dvd/run_subp32.sh          GREEN: wiring checks + every bench this change touched
#   bench/dvd/run_subp32.sh --red    + mutation arms: each must be caught by its bench
#
# The reader is ALSO covered by bench/dvd/run_reader_regress.sh: verdicts must stay
# identical to main; its traces differ by the widened ports and the longer
# subp_control walk (see docs/status_log.md for the masked comparison).
set -u
cd "$(dirname "$0")/../.."
fail=0
SIM=$(mktemp -d)
trap 'rm -rf "$SIM"' EXIT
iv() { iverilog -g2012 -I rtl/mpeg2 -o "$1" "${@:2}"; }

echo "== emu.sv wiring =="
for c in check_subp32_wiring check_forced_subs_wiring check_track_step_wiring check_subp_map_wiring; do
    if python3 "tools/$c.py" > "$SIM/$c.log" 2>&1; then tail -1 "$SIM/$c.log" | sed 's/^/  /'
    else echo "  FAIL $c"; sed 's/^/    /' "$SIM/$c.log"; fail=1; fi
done

# run <label> <pass-regex> <vvp-args...> -- after building $SIM/<label> from SRC
run() {
    local label=$1 pat=$2; shift 2
    if vvp "$SIM/$label.vvp" "$@" > "$SIM/$label.log" 2>&1 && grep -qE "$pat" "$SIM/$label.log"; then
        echo "  PASS $label $*"
    else
        echo "  FAIL $label $*"; grep -E "FAIL|ERR" "$SIM/$label.log" | head -8 | sed 's/^/    /'; fail=1
    fi
}
build() { local label=$1; shift; iv "$SIM/$label.vvp" "$@" 2> "$SIM/$label.build" || { echo "  FAIL build $label"; cat "$SIM/$label.build"; fail=1; }; }

echo "== GREEN: benches =="
python3 tools/gen_subp_map_vec.py > /dev/null
build decl   dvd/subp_decl.sv bench/dvd/subp_decl_tb.sv
run   decl   "RESULT: PASS"
build smap   dvd/subp_stream_map.sv bench/dvd/subp_stream_map_tb.sv
run   smap   "ALL [0-9]+ VECTORS PASSED"
build sctl   dvd/dvd_iso_reader.sv dvd/bcd_time_add.sv bench/dvd/iso_reader_subpctl_tb.sv
run   sctl   "ALL TESTS PASSED"
build attr   dvd/dvd_iso_reader.sv dvd/bcd_time_add.sv bench/dvd/iso_reader_attr_tb.sv
run   attr   "PASSED"
run   attr   "PASSED" +x32
run   attr   "PASSED" +x32 +subcnt=12
run   attr   "PASSED" +subcnt=40
run   attr   "PASSED" +subcnt=0
build hud    dvd/transport_hud.sv bench/dvd/transport_hud_tb.sv
run   hud    "ALL TESTS PASSED"

red() {   # name  file  sed-script  label  pass-regex  [vvp-args]
    local name=$1 file=$2 script=$3 label=$4 pat=$5; shift 5
    local d; d=$(mktemp -d)
    mkdir -p "$d/dvd"; cp dvd/*.sv "$d/dvd/"
    sed "$script" "$file" > "$d/$file"
    if cmp -s "$d/$file" "$file"; then
        echo "  FAIL $name: the mutation did not apply (anchor moved)"; fail=1; rm -rf "$d"; return
    fi
    local srcs
    case $label in
        decl) srcs="$d/dvd/subp_decl.sv bench/dvd/subp_decl_tb.sv" ;;
        sctl) srcs="$d/dvd/dvd_iso_reader.sv dvd/bcd_time_add.sv bench/dvd/iso_reader_subpctl_tb.sv" ;;
        attr) srcs="$d/dvd/dvd_iso_reader.sv dvd/bcd_time_add.sv bench/dvd/iso_reader_attr_tb.sv" ;;
    esac
    if ! iv "$d/sim" $srcs 2> "$d/build"; then
        echo "  FAIL $name: the mutated module did not build"; sed 's/^/      /' "$d/build"; fail=1
    elif vvp "$d/sim" "$@" > "$d/log" 2>&1 && grep -qE "$pat" "$d/log"; then
        echo "  FAIL $name: the bench PASSED with the mutation"; fail=1
    else
        echo "  PASS $name (caught: $(grep -cE 'FAIL|ERR' "$d/log") failing line(s))"
    fi
    rm -rf "$d"
}

if [ "${1:-}" = "--red" ]; then
    echo "== RED arms =="
    # the reader walks only the first 16 subp_control words again
    red red-walk16  dvd/dvd_iso_reader.sv "s/walk_left <= 13'd128;          \/\/ all 32 streams x 4 bytes (spec max)/walk_left <= 13'd64;/" \
        sctl "ALL TESTS PASSED"
    # the attribute sweep stops after 8 subpicture entries again
    red red-attr8   dvd/dvd_iso_reader.sv "s/if (attr_idx == (attr_phase ? 5'd31 : 5'd7)) begin/if (attr_idx == 5'd7) begin/" \
        attr "PASSED" +x32
    # 'next declared' includes the current stream (off-by-one in the mask)
    red red-nextmask dvd/subp_decl.sv "s/wire \[31:0\] above = declared \& ~((32'd2 << cur) - 32'd1);/wire [31:0] above = declared \& ~((32'd1 << cur) - 32'd1);/" \
        decl "RESULT: PASS"
fi

[ $fail -eq 0 ] && echo "ALL GREEN" || echo "FAILURES"
exit $fail
