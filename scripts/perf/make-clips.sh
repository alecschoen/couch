#!/usr/bin/env bash
# Test clips for the Plex/Kodi checks (#23), made here from ffmpeg's own test pattern, so no
# download and nothing from the Plex library: scripts/perf/make-clips.sh [OUT_DIR] [SECONDS]
#
#   couch-perf-4k-hevc10-23.976.mkv    3840x2160 HEVC Main 10, 23.976 fps, ~40 Mb/s
#   couch-perf-1080p-h264-60.mkv       1920x1080 H.264 High, 60 fps, ~12 Mb/s
#   couch-perf-1080p-h264-23.976.mkv   1920x1080 H.264 High, 23.976 fps, ~8 Mb/s
#
# Each has a 48 kHz stereo AAC tone. The bitrates are forced, because the test pattern alone
# would compress to almost nothing and spare the decoder. Copy them to the Pi and play them
# in Kodi directly (couchperf.py plex play --file ...), never through Plex; delete them after.
set -euo pipefail
out=${1:-perf-clips}
secs=${2:-120}
mkdir -p "$out"

clip() {  # clip NAME SIZE RATE ENCODER-ARGS...
    local name=$1 size=$2 rate=$3
    shift 3
    local file="$out/couch-perf-$name.mkv"
    if [[ -s $file ]]; then
        echo "have $file"
        return
    fi
    echo "making $file"
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc2=size=$size:rate=$rate,noise=alls=12:allf=t" \
        -f lavfi -i "sine=frequency=440:sample_rate=48000" \
        -t "$secs" -map 0:v -map 1:a "$@" -c:a aac -ac 2 -b:a 192k "$file"
}

clip 4k-hevc10-23.976 3840x2160 24000/1001 \
    -c:v libx265 -pix_fmt yuv420p10le -preset ultrafast -b:v 40M -maxrate 50M -bufsize 80M \
    -x265-params log-level=error
clip 1080p-h264-60 1920x1080 60 \
    -c:v libx264 -pix_fmt yuv420p -profile:v high -preset veryfast -b:v 12M -maxrate 16M -bufsize 24M
clip 1080p-h264-23.976 1920x1080 24000/1001 \
    -c:v libx264 -pix_fmt yuv420p -profile:v high -preset veryfast -b:v 8M -maxrate 12M -bufsize 16M

for f in "$out"/couch-perf-*.mkv; do
    ffprobe -v error -select_streams v -show_entries stream=codec_name,profile,width,height,r_frame_rate,pix_fmt \
        -show_entries format=bit_rate -of compact=p=0:nk=1 "$f" | tr '\n' ' '
    echo " $(du -h "$f" | cut -f1) $f"
done
