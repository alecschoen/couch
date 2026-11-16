# Performance

Measured on the Pi 5 with `scripts/perf/` (#23). Every number here comes from a JSON result
under `perf-results/<date>/`. Nothing was estimated. A blank cell means it wasn't measured.

_Status: template. The Pi runs haven't happened yet._

## How to measure

Run these from the PC. The scripts need the Pi to be running Couch normally, and SSH access
as `alecschoen@couch.local` (set `COUCH_HOST` to change it).

```bash
scripts/perf/run.sh short
```

```bash
scripts/perf/run.sh full
```

`short` runs everything and plays YouTube for 3 minutes. `full` plays it for 30 minutes.
Results land in `perf-results/<date>/<check>-<time>.json`. `perf-results/` isn't committed.

Single checks:

| Command | What it measures |
|---|---|
| `run.sh boot` | This boot, from the journal and the launcher's paint timing. Run it soon after a reboot, before opening any app |
| `run.sh sample --seconds 60` | CPU per core and per program, temperature, ARM clock, throttling |
| `run.sh launcher` | Frame times while the remote moves the focus around the home screen |
| `run.sh launch --runs 3` | Launcher → YouTube (cold and warm), → Plex, Kodi → launcher |
| `run.sh youtube --minutes 30 [--video ID]` | Dropped frames, decoder, resolution and fps, with the system sampled throughout |
| `run.sh plex info --launch` | Kodi's display, refresh-rate setting and the Plex add-on |
| `run.sh plex nav down down select` | Moves around the Plex add-on (read-only: it only presses keys) |
| `run.sh plex watch --seconds 120 --label 4k-hevc` | Decoder, size, fps, refresh rate and stalls while something plays |
| `run.sh plex play --file ~/couch-perf/CLIP --label NAME` | Plays one of our own clips in Kodi from the file, watches it, stops it |
| `scripts/perf/cage-check.sh` | This PC: the session splash in a real cage with a fake browser, every frame recorded |
| `scripts/perf/record-boot.sh install` | The Pi, as root: record the next session start, for `frames.py` |

The script runs on the Pi with couchd's Python. It talks to couchd's API on 127.0.0.1:8765,
Chromium's DevTools on 127.0.0.1:9222 and Kodi's JSON-RPC on 127.0.0.1:9090. It never deploys
anything or restarts a service. It doesn't sign in or out of anything either, and it never
records Plex URLs, because they carry the token.

Since #25, Chromium's disk cache is a tmpfs, so every boot starts with a cold cache.

## Summary

| Check | Target | Result | OK? |
|---|---|---|---|
| YouTube 1080p60, dropped frames over 30 min | < 0.5 % | | |
| YouTube decoder | hardware | | |
| Launcher focus animation, missed frames | 0 at 60 Hz | | |
| Power on → launcher on screen | | | |
| Black or white between the boot splash and Home | none | | |
| Launcher → YouTube playing | | | |
| Launcher → Plex add-on on screen | | | |
| Kodi 4K HEVC 10-bit clip | hardware decode, no stalls | | |
| Kodi 1080p H.264 clip | hardware decode, no stalls | | |
| Refresh rate follows 23.976 content | switches to 23.98 Hz | | |
| Temperature after 30 min of video | no throttling | | |

## YouTube

Video: `` at ``. Its frame rate was confirmed by `got_1080p60`.

| | Value |
|---|---|
| Minutes played | |
| Frames shown / dropped | |
| Dropped % | |
| Worst 30 s | |
| fps (mean / min) | |
| Decoder (`kVideoDecoderName`, platform?) | |
| Codec / resolution / quality | |
| CPU % (mean / max), Chromium share | |
| Temperature (start / max) | |
| ARM clock min | |
| Throttle flags | |

## Launcher

From `run.sh launcher`, which traces 18 presses over the home rows.

| | p50 | p95 | max |
|---|---|---|---|
| Presented frame interval (ms) | | | |
| Frame latency (ms) | | | |
| rAF interval (ms) | | | |

Frames dropped: . Missed frames at 60 Hz: .

## Launch times

Medians of 3 runs, from the `remote.press`/`apps.launch` command to the event named.

| | Command → frame in front | → page started | → first contentful paint | → largest paint | → playing |
|---|---|---|---|---|---|
| YouTube, cold | | | | | |
| YouTube, warm | | | | | |
| Plex (Kodi) | | | | | |
| Kodi → launcher | | | | | |

## Boot

From `run.sh boot`. Times are seconds since the kernel started. `scripts/boot-timing.sh`
(#4) prints the same milestones for a person to read.

| Milestone | s |
|---|---|
| Firmware framebuffer (splash) | |
| Root fsck done | |
| network.target | |
| couchd ready | |
| cage started | |
| Chromium started | |
| Launcher first paint | |
| Session splash handed over | |
| TV on | |
| multi-user.target | |

`systemd-analyze`: firmware + loader + kernel + userspace = .

### Black and white between the splash and Home

Before #23, cage modesets at ~17.4 s and then shows black until Chromium's first paint at
~24.7 s. That was measured in #4. The director also saw a brief white flash before the
Startup screen: Chromium's window before the launcher paints.

The fix is `system/couch-splash`, which cage starts before Chromium (`couch-session-start`).
It draws couch-fbsplash's own frame, so the logo stays on the same pixels. When Chromium's
window maps over it, it maps a second window on top at once. It goes when couchd says the
launcher has drawn (`couchd/splash.py`). [system/README.md](../system/README.md#session-splash)
has the details.

On this PC, `scripts/perf/cage-check.sh` runs the Pi's cage 0.2.0 headless with a fake browser
that maps a white window. 4 runs recorded every frame, ~380 each:

| | |
|---|---|
| Frames: black → splash → after hand-over | black×19–36 (before the splash starts) → splash×291–297 → ground×66 |
| White frames | 0 in every run. A control with the remap off showed white×179 |
| Splash on screen after its process started | 114 ms (TGA rendered here; from fbsplash's cache drawing takes 7 ms, not 152) |
| Fake browser's frame callbacks while covered | 59–62 per second, so the launcher keeps drawing underneath |
| Splash back on top after the browser mapped | the same frame, per wp_presentation (both presented on one vblank) |

On the Pi, measured:

| | Value |
|---|---|
| cage started → splash on screen (`couch-splash: splash on screen … since_start_ms`) | |
| Remaps (`back on top of a new window`) | |
| `ui.first_frame` → splash handed over | |
| Frames recorded by `record-boot.sh`, classified by `frames.py` | |

Still unavoidable: cage shows black from its modeset until couch-splash's first frame. That's
roughly Python's start plus one round trip; the first table row above measures it.

Result:

### Boot cuts

These are noted for later. Both need the `boot` numbers before deciding.

- **NetworkManager (~2.7 s before network.target).** `couchd.service` has `After=network.target`, and `couch-session.service` waits for couchd, so the network is on the launcher's critical path. couchd doesn't need the network to start: it checks connectivity itself, and YouTube and Plex wait for it later. Dropping `network.target` from `After=` takes NetworkManager off that path. The cost would be that couchd can start before the Wi-Fi is up, which it already handles. Measured: network.target at  s, couchd at  s; critical chain: .
- **Boot fsck (~0.8 s).** Keep root's fsck: ext4 with `fsck.repair=yes` is what recovers it after a power cut (#25). The ~0.8 s is probably `systemd-fsck@` for the vfat `/boot/firmware`. Setting its fstab pass to 0 would skip it. That's a change to #25's `couch-fstab` owner record, not a new edit of fstab. Measured: `systemd-fsck-root` s, `systemd-fsck@…boot-firmware` s.

## Plex

Nothing from the Plex library is played: that would change its watch history and On Deck.
Our own clips from `scripts/perf/make-clips.sh` (ffmpeg's test pattern with noise, no
download) are copied to the Pi, played in Kodi straight from the file with
`run.sh plex play --file ~/couch-perf/<clip> --seconds 120 --label <name>`, and deleted
afterwards. Reading the Plex add-on's and Kodi's state (`plex info`) is fine.

Kodi setting `videoplayer.adjustrefreshrate`: .

| Clip | Decoder | Size | fps | Screen refresh | Open → playing | Stalls | CPU % |
|---|---|---|---|---|---|---|---|
| `couch-perf-4k-hevc10-23.976` (HEVC Main 10, ~40 Mb/s) | | | | | | | |
| `couch-perf-1080p-h264-60` (H.264 High, ~12 Mb/s) | | | | | | | |
| `couch-perf-1080p-h264-23.976` (H.264 High, ~8 Mb/s) | | | | | | | |

TV modes from EDID: .

## Temperature and throttling

| Run | Minutes | Temp start → max (°C) | ARM clock min (MHz) | Throttle flags |
|---|---|---|---|---|
| Idle on the launcher | | | | |
| YouTube 1080p60 | | | | |
| After YouTube | | | | |

## Needs the user (morning)

- Watch a cold boot, power-on to Home, for any black or white between the splash and Home, and for the logo moving.
- Watch YouTube 1080p60 and the test clips by eye: smoothness, judder at 23.976, and audio sync.

## Follow-ups

None yet. Anything over target becomes an issue.
