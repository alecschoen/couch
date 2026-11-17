# Builds Couch's cage (cage/build.sh): Debian Trixie with the Raspberry Pi archive, which has
# the Pi's cage source (in /cage-source, checked by apt against the archive's signature) and
# wlroots 0.20. ARCH=arm64 cross-builds for the Pi; ARCH=amd64 builds for this PC
# (cage-check). One architecture per image: the archive rebuilds some libraries (glibc, Mesa)
# for arm64 only, and Multi-Arch packages must be the same version on both.
#
# For arm64, glibc is Debian's, not the archive's rebuild: the cross-compiler's tools need the
# PC's glibc, which must match. It's the same upstream version as the Pi's.
FROM debian:trixie
ARG ARCH=arm64
ARG CAGE_VERSION=0.3.1-1~bpo13+1+rpt2
COPY raspberrypi-archive-keyring.pgp /usr/share/keyrings/
RUN printf '%s\n' 'Types: deb deb-src' 'URIs: http://archive.raspberrypi.com/debian' \
    'Suites: trixie' 'Components: main' \
    'Signed-By: /usr/share/keyrings/raspberrypi-archive-keyring.pgp' \
    >/etc/apt/sources.list.d/raspi.sources \
 && if [ "$ARCH" = arm64 ]; then \
        dpkg --add-architecture arm64; \
        printf '%s\n' 'Package: libc6:any libc6-dev:any libc-bin:any libc-dev-bin:any' \
            'Pin: origin "archive.raspberrypi.com"' 'Pin-Priority: -1' \
            >/etc/apt/preferences.d/glibc; \
        compiler="gcc-aarch64-linux-gnu libc6-dev-arm64-cross"; \
    else \
        compiler="gcc libc6-dev"; \
    fi \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates dpkg-dev xz-utils patch meson ninja-build pkgconf $compiler \
    wayland-protocols libwayland-bin \
    libwlroots-0.20-dev:$ARCH libxkbcommon-dev:$ARCH libpixman-1-dev:$ARCH \
 && mkdir /cage-source && cd /cage-source \
 && apt-get source --download-only "cage=$CAGE_VERSION" \
 && rm -rf /var/lib/apt/lists/*
