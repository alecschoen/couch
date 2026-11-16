#!/usr/bin/env bash
# What the TV showed during the last boot, and when, from the Pi's own logs (#4). Read-only.
#
#   ssh couch.local 'bash -s' < scripts/boot-timing.sh
#
# Times are seconds since the kernel started, and each is the first time this boot (a later
# restart doesn't count). The firmware's own start-up comes before that and isn't logged;
# time a cold boot by hand to see it.
set -uo pipefail

kmsg() { dmesg 2>/dev/null || sudo -n dmesg; }
units() { journalctl -b -o short-monotonic --no-pager "$@" 2>/dev/null; }
# first [   12.345678] stamp of a line matching $1 in stdin
at() { grep -m1 -E "$1" | sed -E 's/^\[ *([0-9]+\.[0-9]+)\].*/\1/'; }
row() { printf '  %-44s %s\n' "$1" "${2:-(not in the logs)}"; }

echo "Boot $(journalctl --list-boots --no-pager 2>/dev/null | tail -1 | awk '{print $4, $5}')"
echo "cmdline: $(cat /proc/cmdline)"
echo
echo "On the TV:"
row "firmware framebuffer (splash can draw)" "$(kmsg | at 'simple-framebuffer.*fb0|simplefb registered')"
row "splash file asked for" "$(kmsg | at 'Fullscreen splash enabled')"
row "vc4 takes the screen (splash redrawn)" "$(kmsg | at 'fb0: vc4drmfb|vc4drmfb frame buffer')"
row "couch-session starts (cage)" "$(units -u couch-session | at 'Started couch-session')"
row "cage's login session opens" "$(units | at 'pam_unix\(login:session\): session opened for user couch')"
row "couchd sees Chromium" "$(units -u couchd | at 'attached to Chromium')"
row "launcher page connected (UI ready)" "$(units -u couchd | at 'ui connected')"
# When Chromium first painted the launcher, from the page's own clock (DevTools, loopback).
# Counted back from performance.now(), not timeOrigin: the Pi has no clock battery, so the
# wall clock jumps when NTP sets it, after the page has started.
paint=$(python3 - <<'PY' 2>/dev/null
import asyncio, json, aiohttp
async def main():
    async with aiohttp.ClientSession() as s:
        tabs = await (await s.get("http://127.0.0.1:9222/json")).json()
        tab = next(t for t in tabs if t["type"] == "page" and ":8765" in t["url"])
        async with s.ws_connect(tab["webSocketDebuggerUrl"]) as ws:
            js = "JSON.stringify([performance.now(), performance.getEntriesByType('paint').map(e => e.startTime)])"
            await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {"expression": js}})
            reply = await ws.receive_json()
            uptime = float(open("/proc/uptime").read().split()[0])
    now, paints = json.loads(reply["result"]["result"]["value"])
    start = uptime - now / 1000
    print(f"{start + paints[0] / 1000:.2f} (page started {start:.2f})")
asyncio.run(main())
PY
)
row "launcher's first paint (DevTools)" "$paint"
echo
echo "Splash problems (none is good):"
kmsg | grep -E -i 'logo|tga|palette' | grep -v 'Fullscreen splash enabled|Fullscreen Logo Enabled' \
    | sed 's/^/  /' || true
echo
systemd-analyze 2>/dev/null | head -1
systemd-analyze critical-chain couch-session.service 2>/dev/null | sed -n '4,12p'
echo
echo "CEC: $(ls /dev/cec0 2>/dev/null || echo 'no /dev/cec0!')"
echo "Mode: $(cat /sys/class/drm/card*-HDMI-A-1/modes 2>/dev/null | head -1)," \
    "fb0 $(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)"
# Text that reached tty1 since boot. Needs root; empty means nothing was printed on the TV.
if [[ -r /dev/vcs1 ]] || sudo -n true 2>/dev/null; then
    text=$(sudo -n cat /dev/vcs1 2>/dev/null | tr -s ' ')
    echo "tty1 text: '${text// /}'"
fi
