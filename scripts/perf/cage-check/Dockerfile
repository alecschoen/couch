# cage 0.2.0 and wlroots as on the Pi (Raspberry Pi OS is Debian Trixie), for cage-check.
FROM debian:trixie
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cage wf-recorder python3 && rm -rf /var/lib/apt/lists/*
