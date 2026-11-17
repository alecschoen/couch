#!/bin/sh
# Run Couch on this PC: couchd with fake hardware, and the launcher in a 1920×1080 window.
# Extra args go to couchd. Keys: arrows, Enter (OK), Backspace/Esc (Back), H (Home), F1–F4
# (the colour keys; F4 is Blue, the quick menu). Closing the window stops both.
#
#   COUCH_IDLE_AFTER=10 scripts/dev.sh     the idle clock after 10 s
#   scripts/dev.sh --config my.toml    a config of your own (system/config.example.toml)
set -e
cd "$(dirname "$0")/.."
uv sync --quiet
cargo=${CARGO:-$(command -v cargo || echo "$HOME/.cargo/bin/cargo")}
"$cargo" build --quiet --manifest-path launcher/Cargo.toml

uv run couchd --dev "$@" &
couchd=$!
trap 'kill $couchd 2>/dev/null; wait $couchd 2>/dev/null' EXIT INT TERM
launcher/target/debug/couch-launcher
