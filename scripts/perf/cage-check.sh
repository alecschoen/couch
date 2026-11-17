#!/usr/bin/env bash
# The session splash in a real cage, on this PC (#23):
#   scripts/perf/cage-check.sh [--system-cage] [--no-splash] [OUT_DIR]
#
# Builds a Debian Trixie image with the Pi's cage and wlroots (0.3.1 and 0.20, from the
# Raspberry Pi archive), and starts the session as couch-session-start does: couch-splash
# first, waiting for cage's socket, then cage, whose client is a fake browser that maps a
# white window a second later (as Chromium does). Every frame is recorded with wf-recorder -D
# and classified with frames.py. It passes when no black or white frame shows between the
# splash's first frame and the hand-over. Needs Docker, ffmpeg and ffprobe here.
#
# The cage is Couch's (cage/dist/cage-x86_64, from cage/build.sh amd64) with the boot logo as
# its background, if it's built, or with --system-cage the archive's own. --no-splash leaves
# couch-splash out, to see cage on its own: black until the window maps with the system's
# cage, the logo with Couch's. The headless output is 1280x720, so there's no fbsplash cache
# to match; couch-splash renders the TGA itself, as on a Pi whose fbsplash didn't run.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
cage=/couch/cage/dist/cage-x86_64
splash=1
while [ $# -gt 0 ]; do
    case $1 in
        --system-cage) cage=/usr/bin/cage; shift ;;
        --no-splash) splash=0; shift ;;
        *) break ;;
    esac
done
if [ "$cage" != /usr/bin/cage ] && [ ! -x "$root/cage/dist/cage-x86_64" ]; then
    echo "cage/dist/cage-x86_64 isn't built (cage/build.sh amd64): using the system's cage" >&2
    cage=/usr/bin/cage
fi
out=${1:-$(mktemp -d -t couch-cage-check.XXXXXX)}
mkdir -p "$out"
docker build -q -t couch-cage-check -f "$here/cage-check/Dockerfile" "$root/cage" >/dev/null
docker run --rm -v "$root:/couch:ro" -v "$root/ui:/opt/couch/ui:ro" -v "$out:/out" \
    -e XDG_RUNTIME_DIR=/tmp -e WLR_BACKENDS=headless -e WLR_RENDERER=pixman \
    -e WLR_LIBINPUT_NO_DEVICES=1 -e CAGE="$cage" -e SPLASH="$splash" couch-cage-check \
    /couch/scripts/perf/cage-check/start.sh 2>&1 | grep -E 'couch-splash:|Background' || true
python3 "$here/frames.py" "$out/session.mkv" --json "$out/frames.json"
