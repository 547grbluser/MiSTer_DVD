#!/usr/bin/env bash
# fit_unit.sh -- a full Quartus fit of ONE module on the core's device, before it is
# wired into emu.sv: its area (ALM, M10K, DSP, per entity) and its Fmax at both slow
# corners. docs/dts_decoder.md P1b (the DTS engine's go/no-go) is what it was built for.
#
#   USE_DOCKER=1 tools/fit_unit.sh TOP "CLK=MHz ..." file.sv ...
#   USE_DOCKER=1 tools/fit_unit.sh dts_top "clk=27" dvd/dts/dts_seq.sv dvd/dts/dts_vec.sv dvd/dts/dts_top.sv
#
# Every top-level port except the named clocks is a VIRTUAL PIN: a register or LUT
# stands in for the pad, so the module's own paths are timed and the device's I/O is
# not. Each clock gets create_clock at its MHz; several clocks are asynchronous to each
# other. The settings are the core's where they touch timing: the device, junction
# -40 / 100 C with both slow corners analysed (CLAUDE.md: the cold corner often binds),
# FITTER_AGGRESSIVE_ROUTABILITY_OPTIMIZATION ALWAYS, SEED 1.
#
# The numbers are an ESTIMATE. Alone on an empty device the placer has room it will not
# have beside the decoder, so a clock that closes with little margin here may not close
# in the core. Area is closer to final, but packing beside other logic moves it +-5-10 %.
#
# The project lives in .sim/fit_<top>/ (scratch, rebuilt each run). `dvd` there links to
# the repo's, so `include and $readmemh paths written relative to the repo root resolve
# as they do in the core. A fit takes a few GB: do not run two at once.
#   PATHS_ONLY=1 ... re-reports an existing fit without refitting.
set -u
source "$(dirname "$0")/docker_reexec.sh"
maybe_reexec_in_docker "$0" "$@"
cd "$(dirname "$0")/.."
ROOT=$(pwd)
top=${1:?usage: fit_unit.sh TOP \"clk=MHz ...\" files...}; clocks=${2:?clocks}; shift 2
[ $# -gt 0 ] || { echo "fit_unit: no files"; exit 2; }
D=.sim/fit_$top
if [ "${PATHS_ONLY:-0}" != "1" ]; then
  rm -rf "$D"; mkdir -p "$D"
  ln -s "$ROOT/dvd" "$D/dvd"
  {
    echo "set_global_assignment -name FAMILY \"Cyclone V\""
    echo "set_global_assignment -name DEVICE 5CSEBA6U23I7"
    echo "set_global_assignment -name TOP_LEVEL_ENTITY $top"
    echo "set_global_assignment -name SEARCH_PATH $ROOT"
    echo "set_global_assignment -name MIN_CORE_JUNCTION_TEMP \"-40\""
    echo "set_global_assignment -name MAX_CORE_JUNCTION_TEMP 100"
    echo "set_global_assignment -name TIMEQUEST_MULTICORNER_ANALYSIS ON"
    echo "set_global_assignment -name FITTER_AGGRESSIVE_ROUTABILITY_OPTIMIZATION ALWAYS"
    echo "set_global_assignment -name SEED 1"
    echo "set_global_assignment -name NUM_PARALLEL_PROCESSORS ${FIT_CPUS:-4}"
    echo "set_global_assignment -name SDC_FILE $top.sdc"
    for f in "$@"; do echo "set_global_assignment -name SYSTEMVERILOG_FILE $ROOT/$f"; done
    echo "set_instance_assignment -name VIRTUAL_PIN ON -to *"
    for c in $clocks; do echo "set_instance_assignment -name VIRTUAL_PIN OFF -to ${c%%=*}"; done
  } > "$D/$top.qsf"
  echo "PROJECT_REVISION = \"$top\"" > "$D/$top.qpf"
  {
    groups=""
    for c in $clocks; do
      n=${c%%=*}; mhz=${c#*=}
      echo "create_clock -name $n -period [expr {1000.0 / $mhz}] [get_ports {$n}]"
      groups="$groups -group {$n}"
    done
    [ "$(echo "$clocks" | wc -w)" -gt 1 ] && echo "set_clock_groups -asynchronous$groups"
    echo "derive_clock_uncertainty"
  } > "$D/$top.sdc"
  ( cd "$D" && quartus_map "$top" > map.log 2>&1 && quartus_fit "$top" > fit.log 2>&1 \
      && quartus_sta "$top" > sta.log 2>&1 )
  rc=$?
  if [ $rc != 0 ]; then
    echo "fit_unit: the flow failed (exit $rc)"; grep -h -m8 "Error" "$D"/*.log; exit 1
  fi
fi
R="$D/output_files/$top.sta.rpt"; [ -f "$R" ] || R="$D/$top.sta.rpt"
F="$D/output_files/$top.fit.rpt"; [ -f "$F" ] || F="$D/$top.fit.rpt"
M="$D/output_files/$top.map.rpt"; [ -f "$M" ] || M="$D/$top.map.rpt"
[ -f "$R" ] && [ -f "$F" ] || { echo "fit_unit: no reports in $D"; exit 1; }
echo "-- Fmax (restricted, slow corners):"
for c in $clocks; do
  n=${c%%=*}; mhz=${c#*=}
  awk -v pat="$n" -v want="$mhz" '
    /^; Slow 1100mV 100C Model Fmax Summary/ { corner = "100C"; next }
    /^; Slow 1100mV -40C Model Fmax Summary/ { corner = "-40C"; next }
    /^; (Fast|Multicorner|Setup|Hold)/       { corner = "" }
    corner != "" && / MHz / {
      n = split($0, f, ";"); name = f[4]; gsub(/^ +| +$/, "", name)
      if (name == pat) { v = f[3]; gsub(/^ +| +$/, "", v); sub(/ MHz$/, "", v); r[corner] = v }
    }
    END { printf "  %-6s target %6.2f MHz: 100C %s, -40C %s\n", pat, want,
          (r["100C"] ? r["100C"] : "none"), (r["-40C"] ? r["-40C"] : "none") }' "$R"
done
echo "-- use:"
grep -E "^; (Logic utilization \(in ALMs\)|Total block memory bits|Total RAM Blocks|Total DSP Blocks|Total registers)" "$F" \
  | sed 's/  \+/ /g'
echo "-- by entity (ALMs needed, M10K, DSP): $F 'Fitter Resource Utilization by Entity'"
awk '/^; Fitter Resource Utilization by Entity/ { on = 1; next } on && /^\+/ { n++ } on && n >= 3 { exit } on && /^; \|/ { print }' "$F" \
  | awk -F';' '{ printf "  %-44s ALMs %-10s M10K %-4s DSP %s\n", $2, $3, $11, $13 }' | sed 's/  */ /g' | head -20
echo "-- inferred memories (map report):"
grep -E "Inferred (altsyncram|RAM|ROM)" "$M" 2>/dev/null | sed 's/^/  /' | head -30
