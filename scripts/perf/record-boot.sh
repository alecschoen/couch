#!/usr/bin/env bash
# Record what cage shows from the session's start, to check the splash has no gap (#23).
# Run on the Pi as root, by whoever holds the Pi; it's a test tool and comes off afterwards:
#
#   sudo scripts/perf/record-boot.sh install [SECONDS]   (needs: apt install wf-recorder)
#   sudo reboot
#   scp couch.local:/var/tmp/couch-perf/session-*.mkv . && scripts/perf/frames.py session-*.mkv
#   sudo scripts/perf/record-boot.sh remove
#
# install adds a drop-in to couch-session.service so that cage's client first starts
# wf-recorder (every frame, -D, scaled to 480x270 so the Pi keeps up) for SECONDS (default 25)
# and then does what couch-session-start always does. Every session start is recorded while
# it's installed, Kodi's hand-backs included. Recording costs CPU, so the timings in that boot
# aren't representative; it's only for black or white frames. Run `boot` without it.
set -euo pipefail
DROPIN=/etc/systemd/system/couch-session.service.d/50-couch-perf-record.conf
WRAPPER=/usr/local/lib/couch-perf/record-session
OUT=/var/tmp/couch-perf

case ${1:-} in
install)
    secs=${2:-25}
    command -v wf-recorder >/dev/null || { echo "wf-recorder isn't installed (apt install wf-recorder)" >&2; exit 1; }
    install -d -o couch -g couch -m 0755 "$OUT"
    install -d -m 0755 "$(dirname "$WRAPPER")" "$(dirname "$DROPIN")"
    cat >"$WRAPPER" <<SH
#!/bin/sh
# couch-perf (#23): record this session's first ${secs} s, then start it as usual.
file=$OUT/session-\$(date +%H%M%S)-\$(cut -d' ' -f1 /proc/uptime).mkv
timeout -s INT ${secs} wf-recorder -D -c ffv1 -F scale=480:270:flags=area -f "\$file" \\
    >/dev/null 2>&1 </dev/null &
exec /opt/couch/bin/couch-session-start
SH
    chmod 0755 "$WRAPPER"
    cat >"$DROPIN" <<INI
# couch-perf (#23): remove with scripts/perf/record-boot.sh remove
[Service]
ExecStart=
ExecStart=/usr/bin/cage -s -- $WRAPPER
INI
    systemctl daemon-reload
    echo "installed; the next session start (or a reboot) is recorded into $OUT"
    ;;
remove)
    rm -f "$DROPIN" "$WRAPPER"
    rmdir --ignore-fail-on-non-empty "$(dirname "$DROPIN")" "$(dirname "$WRAPPER")" 2>/dev/null || true
    systemctl daemon-reload
    echo "removed; recordings are still in $OUT"
    ;;
*)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
