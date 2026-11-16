#!/usr/bin/env bash
# Run a Couch performance check on the Pi from this PC and keep its JSON here (#23).
#
#   scripts/perf/run.sh CHECK [ARGS...]     e.g.  scripts/perf/run.sh youtube --short
#   scripts/perf/run.sh short               every check, YouTube for 3 minutes (~15 min)
#   scripts/perf/run.sh full                the same with the 30-minute YouTube run (~45 min)
#
# The checks are scripts/perf/couchperf.py, piped over SSH to couchd's Python on the Pi, so
# nothing is copied or installed there. Results land in perf-results/<date>/<check>-<time>.json
# (progress on stderr). Long checks also keep a copy on the Pi in /tmp/couch-perf, in case
# SSH drops. Nothing restarts or reboots; the `boot` check reads the last boot as it was.
#
# COUCH_HOST picks the Pi (default alecschoen@couch.local).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
host=${COUCH_HOST:-alecschoen@couch.local}
py=/opt/couch/.venv/bin/python
dir=${COUCH_PERF_RESULTS:-$here/../../perf-results/$(date +%F)}
mkdir -p "$dir"

check() {
    local name=$1
    shift
    local out="$dir/$name-$(date +%H%M%S).json"
    echo "== $name $* -> $out" >&2
    if ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "$host" "$py" - "$@" \
        <"$here/couchperf.py" >"$out.tmp"; then
        mv "$out.tmp" "$out"
    else
        echo "!! $name failed (partial output kept in $out.tmp)" >&2
        return 1
    fi
}

suite() {
    local minutes=$1 failed=0
    check boot boot || failed=1
    check sample-idle sample --seconds 60 || failed=1
    check launcher launcher || failed=1
    check launch launch --runs 3 || failed=1
    check youtube youtube --minutes "$minutes" || failed=1
    check plex-info plex info --launch || failed=1
    check sample-after sample --seconds 30 || failed=1
    return $failed
}

case ${1:-} in
    short) suite 3 ;;
    full) suite 30 ;;
    "" | -h | --help) sed -n '2,15p' "$0" ;;
    plex) check "plex-${2:-info}" "$@" ;;
    *) check "$1" "$@" ;;
esac
