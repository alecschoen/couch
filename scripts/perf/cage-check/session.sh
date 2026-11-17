#!/bin/sh
# cage's client in cage-check: a fake browser in place of the launcher, recorded.
cd /out || exit 1
wf-recorder -D -c ffv1 -f /out/session.mkv >/out/wf-recorder.log 2>&1 &
recorder=$!
sleep 1  # Chromium takes seconds; a second shows the splash alone
python3 /couch/scripts/perf/cage-check/fake-browser.py 1.5 &
browser=$!
sleep 3
if [ -f /tmp/splash.pid ]; then
    kill "$(cat /tmp/splash.pid)"  # what couchd does after the launcher's first frame
fi
sleep 1
kill -INT "$recorder"
sleep 1
kill "$browser"
