#!/bin/bash
# Build the native launcher for the Pi (Raspberry Pi OS trixie, aarch64) on a PC:
#   launcher/cross-build.sh        -> launcher/dist/couch-launcher-aarch64
# install.sh installs that as /opt/couch/bin/couch-launcher.
#
# Links in a debian:trixie container against trixie's arm64 glibc and fontconfig, the Pi's
# own, using the host's Rust toolchain (rustup target add aarch64-unknown-linux-gnu) and
# cargo cache. Needs Docker. Everything else (EGL, Wayland, xkbcommon, libpulse) is opened
# at run time, so the binary links only libc, libm, libgcc_s and libfontconfig.
set -euo pipefail
cd "$(dirname "$0")"
TARGET=aarch64-unknown-linux-gnu
RUSTUP_HOME=${RUSTUP_HOME:-$HOME/.rustup}
CARGO_HOME=${CARGO_HOME:-$HOME/.cargo}
IMAGE=couch-launcher-cross

"$CARGO_HOME/bin/rustup" target add "$TARGET" >/dev/null

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" - <<'EOF'
FROM debian:trixie
RUN dpkg --add-architecture arm64 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gcc gcc-aarch64-linux-gnu libc6-dev libc6-dev-arm64-cross pkg-config \
    libfontconfig-dev:arm64 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
EOF
fi

docker run --rm --user "$(id -u):$(id -g)" \
    -v "$PWD:/src" -v "$RUSTUP_HOME:/rustup" -v "$CARGO_HOME:/cargo" -w /src \
    -e RUSTUP_HOME=/rustup -e CARGO_HOME=/cargo -e CARGO_TARGET_DIR=/src/target/cross \
    -e CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    -e PKG_CONFIG_ALLOW_CROSS=1 -e PKG_CONFIG_PATH=/usr/lib/aarch64-linux-gnu/pkgconfig \
    -e PKG_CONFIG_SYSROOT_DIR=/ \
    "$IMAGE" /cargo/bin/cargo build --release --locked --target "$TARGET"

mkdir -p dist
cp target/cross/$TARGET/release/couch-launcher dist/couch-launcher-aarch64
echo "dist/couch-launcher-aarch64 ($(du -h dist/couch-launcher-aarch64 | cut -f1))"
