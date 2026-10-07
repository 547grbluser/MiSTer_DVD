#!/usr/bin/env bash
# clock_check.sh — every clock's INTRA-domain setup/hold/recovery/removal, every corner.
#
#   [USE_DOCKER=1] tools/clock_check.sh            # quartus_sta, then the policy pass
#   tools/clock_check.sh --report-only             # re-judge an existing clock_check.tsv
#
# Needs a completed fit on disk (db/ + output_files/); it does NOT refit. Takes about
# 30 s: one timing-netlist update per corner. Writes output_files/clock_check.tsv and
# prints one row per clock plus every WARN/FAIL with its worst path's endpoints.
# Exit: 0 = no FAIL, 1 = FAIL, 2 = no fit / quartus_sta failed / unreadable TSV.
#
# fmax_check.sh stays the gate build_release.sh runs (it reads the .sta.rpt for free);
# this is the deeper check, run on the fit a release ships (docs/timing.md "The checks").
# Rules and the per-clock policy live in tools/clock_check.py.

set -u

if [ "${1:-}" = "--sta" ]; then
    # Internal: the quartus_sta step, re-executed inside the Quartus container when
    # USE_DOCKER=1 (the image is not guaranteed to have python3, so the policy pass
    # below stays on the host).
    source "$(dirname "$0")/docker_reexec.sh"
    maybe_reexec_in_docker "$0" "$@"
    cd "$(dirname "$0")/.."
    exec quartus_sta -t tools/clock_check.tcl
fi

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${1:-}" != "--report-only" ]; then
    if [ ! -d db ] || [ ! -f output_files/DVD.sta.rpt ]; then
        echo "clock_check: no completed fit here (db/ + output_files/DVD.sta.rpt)" >&2
        exit 2
    fi
    rm -f output_files/clock_check.tsv
    "$0" --sta > output_files/clock_check.log 2>&1 || {
        echo "clock_check: quartus_sta failed, see output_files/clock_check.log" >&2
        exit 2
    }
fi

exec python3 tools/clock_check.py output_files/clock_check.tsv
