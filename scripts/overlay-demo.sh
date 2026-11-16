#!/bin/sh
# Walk through the overlays (#18) in dev mode: the quick menu, toasts and the volume panel,
# over the launcher, YouTube and Plex. Start couchd first (scripts/dev.sh), then run this
# and watch the launcher window. In dev mode YouTube is a real tab, so the menu and toasts
# there are the injected overlay; Plex is pretend, so its menu shows in the launcher and its
# toasts are logged as the Kodi calls couchd would make (see couchd's log).
set -e
API=${COUCH_API:-http://127.0.0.1:8765}
PAUSE=${PAUSE:-1.5}

cmd() { curl -fsS -XPOST "$API/api/command/$1" -d "${2:-{\}}" >/dev/null; }
key() { for k in "$@"; do cmd remote.press "{\"key\": \"$k\"}"; sleep 0.4; done; }
say() { printf '%s\n' "$*"; }
wait_() { sleep "$PAUSE"; }

demo() {
  say "$1: quick menu (Blue), across to Volume, louder, back out"
  key blue right right up up
  wait_
  key back
  say "$1: a toast, then the volume panel"
  cmd overlay.toast '{"text": "Updated to v0.4.0", "icon": "update"}'
  sleep 3
  cmd audio.set_volume '{"volume": 35}'
  sleep 0.5
  cmd audio.set_volume '{"volume": 45}'
  sleep 3
}

curl -fsS "$API/api/health" >/dev/null || { say "couchd isn't up at $API (scripts/dev.sh)"; exit 1; }
key home
wait_
demo launcher
cmd apps.launch '{"app": "youtube"}'
sleep 4
demo youtube
cmd apps.launch '{"app": "plex"}'
sleep 3
demo plex
key home
say "done"
