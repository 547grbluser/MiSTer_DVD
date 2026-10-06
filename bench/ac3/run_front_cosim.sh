#!/usr/bin/env bash
# Build the Verilator/liba52 co-sim and run it on all generated test streams.
# Regenerates the streams first if they are missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/bench/ac3"

if ! ls "$ROOT"/tools/streams/*.ac3 >/dev/null 2>&1; then
    echo "[streams] generating test streams"
    "$ROOT/tools/gen_test_stream.sh" >/dev/null
fi

make -f Makefile.cosim >/dev/null

rc=0
for s in "$ROOT"/tools/streams/*.ac3; do
    # ac3_front (retired from the core) refuses 1+1 dual mono by design; the engine
    # decodes it, and its gates are run_ac3*.sh and tools/ac3_dualmono.py --check
    # (docs/lpcm_full.md §7)
    case "$s" in *dualmono*) echo "=== $(basename "$s") === skipped: ac3_front refuses 1+1"; continue;; esac
    echo "=== $(basename "$s") ==="
    ./obj_cosim/ac3_front_cosim "$s" || rc=1
done

# Committed vectors that ffmpeg cannot generate (short blocks).  bbb_short_5p1
# exercises the M16 256-pt short transform; run extra frames so multiple short
# blocks are covered (PCM is informational — see bench/ac3/vectors/README.md).
for s in "$ROOT"/bench/ac3/vectors/*.ac3; do
    [ -e "$s" ] || continue
    echo "=== $(basename "$s") (committed; short-block vector) ==="
    AC3_MAX_FRAMES=18 ./obj_cosim/ac3_front_cosim "$s" || rc=1
done
exit $rc
