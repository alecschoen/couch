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
| Black between the boot splash and the launcher | none | | |
| Launcher → YouTube playing | | | |
| Launcher → Plex add-on on screen | | | |
| Plex 4K HEVC direct play | hardware decode, no stalls | | |
| Plex 1080p H.264 | hardware decode, no stalls | | |
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

### Black between cage and the launcher

Before #23, cage modesets at ~17.4 s and then shows black until Chromium's first paint at
~24.7 s. That was measured in #4.

The fix is in `system/couch-session-start` and `couchd/splash.py`. cage's client first runs
`swayimg` on `ui/brand/splash.png`, the launcher's Startup frame, and only then starts
Chromium. cage stacks newly mapped windows on top, so Chromium covers the splash when it maps.
The launcher reports its first frame with `ui.first_frame`, and 0.5 s later couchd ends swayimg.
Left running under Chromium, swayimg would cost the compositor a fullscreen blend on every frame.

What to check after a reboot:

- No black frame between the boot splash and the Startup screen. A person has to watch for this.
- `couchd.splash: splash handed over` appears in the journal, and swayimg is gone afterwards.
- Chromium's `Startup.BrowserWindow.FirstPaint` versus `Startup.FirstWebContents.NonEmptyPaint3`. If Chromium maps its window before it draws content, one blank frame could still show. Plan B in that case: couchd starts a second swayimg when Chromium maps, which lands on top, and ends it after the first frame.

Result:

### Boot cuts

These are noted for later. Both need the `boot` numbers before deciding.

- **NetworkManager (~2.7 s before network.target).** `couchd.service` has `After=network.target`, and `couch-session.service` waits for couchd, so the network is on the launcher's critical path. couchd doesn't need the network to start: it checks connectivity itself, and YouTube and Plex wait for it later. Dropping `network.target` from `After=` takes NetworkManager off that path. The cost would be that couchd can start before the Wi-Fi is up, which it already handles. Measured: network.target at  s, couchd at  s; critical chain: .
- **Boot fsck (~0.8 s).** Keep root's fsck: ext4 with `fsck.repair=yes` is what recovers it after a power cut (#25). The ~0.8 s is probably `systemd-fsck@` for the vfat `/boot/firmware`. Setting its fstab pass to 0 would skip it. That's a change to #25's `couch-fstab` owner record, not a new edit of fstab. Measured: `systemd-fsck-root` s, `systemd-fsck@…boot-firmware` s.

## Plex

From `run.sh plex info` and `run.sh plex watch`. Kodi setting `videoplayer.adjustrefreshrate`:
.

| Content | Decoder | Size | fps | Screen refresh | Stalls | CPU % |
|---|---|---|---|---|---|---|
| 4K HEVC (direct play) | | | | | | |
| 1080p H.264 | | | | | | |
| 23.976 film | | | | | | |

TV modes from EDID: .

## Temperature and throttling

| Run | Minutes | Temp start → max (°C) | ARM clock min (MHz) | Throttle flags |
|---|---|---|---|---|
| Idle on the launcher | | | | |
| YouTube 1080p60 | | | | |
| After YouTube | | | | |

## Needs the user (morning)

- Watch a cold boot, power on to launcher, for any black frame between the splash and the Startup screen.
- Watch YouTube 1080p60 and the Plex test files by eye: smoothness, judder at 23.976, and audio sync.

## Follow-ups

None yet. Anything over target becomes an issue.
