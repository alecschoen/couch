#!/bin/sh
# cage's client in cage-check: what couch-session-start does, with a fake browser, recorded.
cd /out || exit 1
wf-recorder -D -c ffv1 -f /out/session.mkv >/out/wf-recorder.log 2>&1 &
recorder=$!
sleep 0.5
/couch/system/couch-splash &
splash=$!
sleep 1  # Chromium takes seconds; a second shows the splash alone
python3 /couch/scripts/perf/cage-check/fake-browser.py 1.5 &
browser=$!
sleep 3
kill "$splash"  # what couchd does after the launcher's first frame
sleep 1
kill -INT "$recorder"
sleep 1
kill "$browser"
