#!/usr/bin/env bash
# The session splash in a real cage, on this PC (#23): scripts/perf/cage-check.sh [OUT_DIR]
#
# Builds a Debian Trixie image with the Pi's cage, runs cage headless with couch-splash and a
# fake browser that maps a white window a second later (as Chromium does), records every
# frame with wf-recorder -D, then classifies the frames with frames.py. It passes when no
# black or white frame shows between the splash's first frame and the hand-over. Needs
# Docker, ffmpeg and ffprobe here. The headless output is 1280x720, so there's no fbsplash
# cache to match; couch-splash renders the TGA itself, as on a Pi whose fbsplash didn't run.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
out=${1:-$(mktemp -d -t couch-cage-check.XXXXXX)}
mkdir -p "$out"
docker build -q -t couch-cage-check "$here/cage-check" >/dev/null
docker run --rm -v "$root:/couch:ro" -v "$root/ui:/opt/couch/ui:ro" -v "$out:/out" \
    -e XDG_RUNTIME_DIR=/tmp -e WLR_BACKENDS=headless -e WLR_RENDERER=pixman \
    -e WLR_LIBINPUT_NO_DEVICES=1 couch-cage-check \
    cage -s -- /couch/scripts/perf/cage-check/session.sh 2>&1 | grep 'couch-splash:' || true
python3 "$here/frames.py" "$out/session.mkv" --json "$out/frames.json"
