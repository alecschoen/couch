# The Pi's cage and wlroots (Raspberry Pi OS trixie: cage 0.3.1, wlroots 0.20, from the
# Raspberry Pi archive, which builds them for amd64 too), for cage-check. Built from cage/,
# for the archive's key: docker build -f scripts/perf/cage-check/Dockerfile cage
FROM debian:trixie
COPY raspberrypi-archive-keyring.pgp /usr/share/keyrings/
RUN printf '%s\n' 'Types: deb' 'URIs: http://archive.raspberrypi.com/debian' \
    'Suites: trixie' 'Components: main' \
    'Signed-By: /usr/share/keyrings/raspberrypi-archive-keyring.pgp' \
    >/etc/apt/sources.list.d/raspi.sources \
 && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cage libwlroots-0.20 wf-recorder python3 && rm -rf /var/lib/apt/lists/*
