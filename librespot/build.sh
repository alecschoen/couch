#!/bin/bash
# Build librespot, Couch's Spotify Connect speaker (#80), for the Pi on a PC:
#   librespot/build.sh     -> librespot/dist/librespot-aarch64
# install.sh installs that as /opt/couch/bin/librespot, which couch-spotify.service runs.
# Needs Docker.
#
# Neither Debian nor Raspberry Pi OS packages librespot for trixie (shairport-sync, for
# AirPlay, is packaged and comes from apt), and Couch adds no third-party apt repositories.
# So this builds one pinned release from its source tarball, checked against the sha256 below,
# with trixie's own Rust and the crate versions in librespot's Cargo.lock (--locked).
#
# Features: PulseAudio output (PipeWire's pipewire-pulse, like everything else on Couch, so
# couchd's output and volume apply), Avahi for zeroconf (the Pi already runs avahi-daemon;
# librespot's built-in mDNS responder would fight it for port 5353), and rustls with the
# system's certificates (no OpenSSL to cross-link). The binary links libc, libgcc_s and
# libpulse/libpulse-simple (libpulse0, installed by install.sh).
#
# To update: change VERSION and SHA256 together (sha256sum of the GitHub tag tarball).
# With the binary already built from this VERSION and FEATURES it does nothing (--force
# rebuilds), so scripts/deploy.sh can run it every time.
set -euo pipefail
cd "$(dirname "$0")"
VERSION=0.8.0
SHA256=0e4922997e1c67d27b3f50dcc388ecb8a3c08eba23b764879071f6e9e8c07ec7
FEATURES="rustls-tls-native-roots pulseaudio-backend with-avahi"
TARGET=aarch64-unknown-linux-gnu
IMAGE=couch-librespot-build
BUILT="$VERSION $FEATURES"

if [[ ${1:-} != --force && -f dist/librespot-aarch64 && -f dist/librespot-aarch64.built ]] \
    && [[ $(<dist/librespot-aarch64.built) == "$BUILT" ]]; then
    echo "dist/librespot-aarch64 is librespot $VERSION already"
    exit 0
fi
docker build -q -t "$IMAGE" . >/dev/null
mkdir -p dist target
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/target:/work" -v "$PWD/dist:/out" \
    -e VERSION="$VERSION" -e SHA256="$SHA256" -e FEATURES="$FEATURES" -e TARGET="$TARGET" \
    -e CARGO_HOME=/work/cargo -e CARGO_TARGET_DIR=/work/build \
    -e CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    -e CARGO_PROFILE_RELEASE_STRIP=symbols \
    -e PKG_CONFIG_ALLOW_CROSS=1 -e PKG_CONFIG_PATH=/usr/lib/aarch64-linux-gnu/pkgconfig \
    -e PKG_CONFIG_SYSROOT_DIR=/ \
    "$IMAGE" bash -euo pipefail -c '
cd /work
tarball=librespot-$VERSION.tar.gz
[ -f "$tarball" ] || curl -fsSL -o "$tarball" \
    "https://github.com/librespot-org/librespot/archive/refs/tags/v$VERSION.tar.gz"
echo "$SHA256  $tarball" | sha256sum -c --quiet
rm -rf "src-$VERSION" && mkdir "src-$VERSION"
tar -xzf "$tarball" -C "src-$VERSION" --strip-components=1
cd "src-$VERSION"
# Its rust-toolchain.toml asks rustup for rustfmt and clippy; there is no rustup here.
rm -f rust-toolchain.toml
cargo build --release --locked --target "$TARGET" --no-default-features --features "$FEATURES"
install -m 755 "$CARGO_TARGET_DIR/$TARGET/release/librespot" /out/librespot-aarch64
'
echo "$BUILT" >dist/librespot-aarch64.built
echo "dist/librespot-aarch64 ($(du -h dist/librespot-aarch64 | cut -f1), librespot $VERSION)"
