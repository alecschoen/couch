# Builds librespot for the Pi (librespot/build.sh): Debian Trixie with its own Rust (1.85, the
# version librespot 0.8 needs at least) and the arm64 standard library, so no rustup or other
# installer. Cross-compiled for arm64 against trixie's libpulse, the Pi's own.
FROM debian:trixie
RUN dpkg --add-architecture arm64 \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl cargo rustc libstd-rust-dev:arm64 \
    gcc gcc-aarch64-linux-gnu libc6-dev libc6-dev-arm64-cross pkgconf \
    libpulse-dev:arm64 \
 && rm -rf /var/lib/apt/lists/*
