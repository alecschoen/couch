#!/bin/sh
# What couch-session-start --start-cage does, in cage-check: couch-splash first (waiting for
# cage's socket), then cage, with Couch's background if it's Couch's cage.
if [ "$SPLASH" = 1 ]; then
    /couch/system/couch-splash --wait &
    echo $! >/tmp/splash.pid
fi
background=
if [ "$CAGE" != /usr/bin/cage ]; then
    background="-b /couch/ui/brand/splash.tga"
fi
# shellcheck disable=SC2086 # $background is two words or none
exec "$CAGE" $background -s -- /couch/scripts/perf/cage-check/session.sh
