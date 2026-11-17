#!/bin/bash
# Build Couch's cage: the Pi's own cage (Raspberry Pi OS trixie's 0.3.1, with its patches) plus
# couch-background.patch, which draws the boot logo from cage's first frame until the first
# window maps (cage -b FILE), so the boot goes from the splash to cage without a black frame.
#   cage/build.sh          -> cage/dist/cage-aarch64 (for the Pi, cross-built)
#   cage/build.sh amd64    -> cage/dist/cage-x86_64  (for this PC: scripts/perf/cage-check.sh)
# install.sh installs it as /opt/couch/bin/cage; couch-session-start runs it if it's there,
# and the system's /usr/bin/cage otherwise. Needs Docker.
#
# It links the same libraries as the system's cage (wlroots 0.20 and the rest), so it's for
# the Pi's cage and wlroots version: CAGE_VERSION in the Dockerfile and the .dsc's checksum
# here change together, when Raspberry Pi OS updates cage.
set -euo pipefail
cd "$(dirname "$0")"
ARCH=${1:-arm64}
DSC=cage_0.3.1-1~bpo13+1+rpt2.dsc
DSC_SHA256=dff06bb97bf3403be878ad63f96a5f5f9fd86778982eea684f54c9b9b02637fb
case $ARCH in
    arm64) MACHINE=aarch64 ;;
    amd64) MACHINE=x86_64 ;;
    *) echo "usage: $0 [arm64|amd64]" >&2; exit 2 ;;
esac
IMAGE=couch-cage-build:$ARCH

docker build -q --build-arg ARCH="$ARCH" -t "$IMAGE" . >/dev/null
mkdir -p dist
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/couch-cage:ro" -v "$PWD/dist:/out" \
    -e ARCH="$ARCH" -e MACHINE="$MACHINE" -e DSC="$DSC" -e DSC_SHA256="$DSC_SHA256" \
    "$IMAGE" bash -euo pipefail -c '
cd /tmp
echo "$DSC_SHA256  /cage-source/$DSC" | sha256sum -c --quiet
# Unpacks the source, checks it against the .dsc and applies the Pi'"'"'s patches.
dpkg-source --no-check -x "/cage-source/$DSC" cage >/dev/null
cd cage
patch -p1 --quiet </couch-cage/couch-background.patch
cross=()
if [ "$ARCH" = arm64 ]; then
    printf "%s\n" "[binaries]" "c = '"'"'aarch64-linux-gnu-gcc'"'"'" \
        "strip = '"'"'aarch64-linux-gnu-strip'"'"'" \
        "pkg-config = '"'"'aarch64-linux-gnu-pkg-config'"'"'" \
        "[host_machine]" "system = '"'"'linux'"'"'" "cpu_family = '"'"'aarch64'"'"'" \
        "cpu = '"'"'aarch64'"'"'" "endian = '"'"'little'"'"'" >/tmp/cross.ini
    cross=(--cross-file /tmp/cross.ini)
fi
# Built as Debian builds it (hardening=+all), stripped.
eval "$(DEB_HOST_ARCH=$ARCH DEB_BUILD_MAINT_OPTIONS=hardening=+all dpkg-buildflags --export=sh)"
meson setup build "${cross[@]}" --buildtype=plain --strip -Dman-pages=disabled >/dev/null
ninja -C build >/dev/null
install -m 755 build/cage "/out/cage-$MACHINE"
'
echo "dist/cage-$MACHINE ($(du -h "dist/cage-$MACHINE" | cut -f1))"
