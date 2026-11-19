#!/usr/bin/env bash
# Build Couch on this PC and install it on the Pi:
#
#   scripts/deploy.sh [user@host]          (default: couch.local)
#
# The Pi doesn't build anything itself: the launcher (Rust) and Couch's cage are cross-built
# here in Docker (launcher/cross-build.sh, cage/build.sh, and librespot/build.sh for the
# Spotify speaker). Then this working tree, as it is
# (committed or not, without what .gitignore hides) plus those binaries, goes to ~/couch
# on the Pi, the previous copy is kept as ~/couch.prev, and install.sh runs there. That asks
# for your sudo password on the Pi unless sudo doesn't need one.
set -euo pipefail
cd "$(dirname "$0")/.."
host=${1:-couch.local}

launcher/cross-build.sh
[[ -f cage/dist/cage-aarch64 ]] || cage/build.sh
librespot/build.sh # a pinned release: does nothing once it's built

echo "deploying $(git describe --always --dirty) to $host"
{
    git ls-files -z --cached --others --exclude-standard
    printf '%s\0' launcher/dist/couch-launcher-aarch64 cage/dist/cage-aarch64 \
        librespot/dist/librespot-aarch64
} | tar --null -T - -cz | ssh "$host" '
    set -e
    rm -rf ~/couch.new && mkdir ~/couch.new && tar -xz -C ~/couch.new
    if [ -d ~/couch ]; then rm -rf ~/couch.prev && mv ~/couch ~/couch.prev; fi
    mv ~/couch.new ~/couch'
ssh -t "$host" 'cd ~/couch && sudo ./install.sh'
