# Performance

Measured on the Pi 4 Model B (Rev 1.4, 8 GB) with `scripts/perf/` (#23). Every number here comes from a JSON result
under `perf-results/<date>/`. Nothing was estimated. A blank cell means it wasn't measured.

_Status: measured on 2026-11-16 with f2ab061 deployed, using `run.sh short`, the three Kodi clips and one
recorded reboot. YouTube playback wasn't measured: see [YouTube](#youtube)._

_Most of these numbers are from the web launcher, a page in Chromium, which Couch has since
replaced with the native one (`launcher/`). They're kept as the record that led there: the
launcher's frame times, the launches and the boot need measuring again, and `couchperf.py`'s
`launcher` check doesn't apply any more (see the top of that file)._

## How to measure

Run these from the PC. The scripts need the Pi to be running Couch normally, and SSH access
to `couch.local` (set `COUCH_HOST` to change it, e.g. `you@couch.local`).

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
| `run.sh plex play --file /var/tmp/couch-perf/CLIP --label NAME` | Plays one of our own clips in Kodi from the file, watches it, stops it |
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
| YouTube 1080p60, dropped frames over 30 min | < 0.5 % | not measured (account picker) | ? |
| YouTube decoder | hardware | not measured | ? |
| Launcher focus animation, missed frames | 0 at 60 Hz | 3 of 825 rAF frames; 4 of 1,252 compositor frames dropped | close |
| Power on → launcher on screen | | | |
| Black or white between the boot splash and Home | none | | |
| Launcher → YouTube on screen (cold / warm) | | 1.94 s / 0.10 s | |
| Launcher → Plex add-on on screen | | 5.50 s | slow |
| Kodi → launcher painted | | 2.89 s | |
| Kodi 4K HEVC 10-bit clip | hardware decode, no stalls | `hevc-drm_prime`, 23 % of a core, one stall in the first 2 s | yes |
| Kodi 1080p H.264 clip | hardware decode, no stalls | `h264_v4l2m2m`; at 60 fps Kodi uses 110 % of a core | no, at 60 fps |
| Refresh rate follows 23.976 content | switches to 23.98 Hz | 23.98 Hz for both 23.976 clips | yes |
| Temperature under video | no throttling | max 60.9 °C, 0x0 (90 s clips; no 30-min run) | yes, short runs |

## YouTube

Not measured. The 3-minute run (`aqz-KE-bpKQ`) played nothing. A few seconds after the
check set the watch link, YouTube TV showed its "Who's watching?" account picker and went back
to `#/`, so the video element never loaded (0x0, no codec). Picking a profile on the user's
account is the user's call, so the check doesn't press anything on it; it now names the picker
in its error. To measure: pick the profile on the TV, then run `run.sh youtube --short`
(and `--minutes 30` for the target above).

## Launcher

From `run.sh launcher`: 36 key events over the home rows, traced in Chromium.

| | p50 | p95 | max |
|---|---|---|---|
| Presented frame interval (ms) | 16.63 | 16.78 | 24.49 |
| Frame latency (ms) | 50.1 | 66.8 | 102.8 |
| rAF interval (ms) | 16.7 | 16.8 | 50.1 |

Frames dropped: 4 of 1,252 (`STATE_DROPPED`). Missed frames at 60 Hz: 3 of 825 in rAF, 7 in
draw-and-swap. Chromium's main thread spent 2,595 ms of its 6,839 ms recalculating style,
against 121 ms of layout and 547 ms of script. While the focus moved, the system used 25 % CPU
and Chromium 89 % of a core.

## Launch times

Medians of 3 runs, from the `remote.press`/`apps.launch` command to the event named.

| | Command → frame in front | → page started | → first contentful paint | → largest paint | → playing |
|---|---|---|---|---|---|
| YouTube, cold | 1.94 | 0.21 | | 1.90 | not measured |
| YouTube, warm | 0.10 | | | | |
| Plex (Kodi) | 5.50 (PM4K's window; Kodi's JSON-RPC up at 3.30) | | | | |
| Kodi → launcher | 2.89 (DevTools up at 1.90) | | 2.89 | | |

The three cold YouTube runs took 1.97 s, 1.94 s and 0.89 s; the third probably found
YouTube's files still in Chromium's cache. The `apps.launch` command itself returns in 1.29 s for Plex, after cage stops.

## Boot

From `run.sh boot` on the recorded reboot (boot `2185692b`, f2ab061, 07:22). Times are
seconds since the kernel started. `scripts/boot-timing.sh` (#4) prints the same milestones
for a person to read.

| Milestone | s |
|---|---|
| Firmware framebuffer (kernel splash) | 4.66 |
| `/boot/firmware` fsck done (root has no separate fsck unit) | 6.21 |
| vc4 takes the screen | 6.27 |
| couch-fbsplash drew | 7.12 |
| network.target | 10.08 |
| couchd ready | 11.61 |
| cage started | 11.61 |
| TV on | 12.18 |
| cage's modeset (from its log) | 15.25 |
| couch-splash on screen | 16.01 |
| Chromium on DevTools (couchd attached) | 25.89 |
| Chromium's window mapped (couch-splash goes back on top) | 26.20 |
| Launcher connected | 28.07 |
| Launcher's first frame | 28.39 |
| Session splash handed over | 28.90 |

`systemd-analyze`: 2.255 s (kernel) + 9.355 s (userspace) = 11.611 s to `multi-user.target`.
The Pi's firmware time isn't reported. The critical chain to `couch-session.service` runs
through `couchd.service` (+1.52 s), `network.target` and `NetworkManager.service` (+2.92 s).

Where the 17 s from `couch-session` to Home goes: cage takes 3.6 s to its modeset (EGL setup
alone is 1.9 s), then Chromium takes 10.6 s from there to DevTools, and the launcher 2.5 s to
its first frame (`page_ms` 3,549).

### Black and white between the splash and Home

Before #23, cage modesets at ~17.4 s and then shows black until Chromium's first paint at
~24.7 s. That was measured in #4. The director also saw a brief white flash before the
Startup screen: Chromium's window before the launcher paints.

The fix is `system/couch-splash`, which `couch-session-start` starts before cage, so it's
ready the moment cage takes the screen.
It draws couch-fbsplash's own frame, so the logo stays on the same pixels. When Chromium's
window maps over it, it maps a second window on top at once. It goes when couchd says the
launcher has drawn (`couchd/splash.py`). [system/README.md](../system/README.md#session-splash)
has the details.

On this PC, `scripts/perf/cage-check.sh` ran cage 0.2.0 (Debian Trixie's) headless with a fake
browser that maps a white window. 4 runs recorded every frame, ~380 each:

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
| couch-splash started → on screen (`since_start_ms`) | 632 ms: mapped at 435 ms, presented 199 ms later |
| cage's modeset → couch-splash on screen (black) | 0.77 s (15.25 → 16.01) |
| Remaps (`back on top of a new window`) | 1, at 26.202, 1 ms after Chromium's window took the focus |
| `ui.first_frame` → splash handed over | 0.50 s (the set delay), SIGTERM 0.3 ms later |
| Frames recorded by `record-boot.sh`, classified by `frames.py` | splash x298 → other x415: no black or white |

The recording starts at couch-splash's first frame, since cage records nothing before a client
draws, so the 0.77 s of black after the modeset comes from the journal instead. couch-splash
didn't draw from couch-fbsplash's cache here: the Pi's framebuffer is RGB565
(`fb0-1920x1080-16-3840-11-5-5-6-0-5`), and couch-splash only reads a 32-bit one, so it
rendered the TGA in Python (on the PC, 152 ms against 7 ms from a cache). A pre-rendered
32-bit frame would remove most of that black.

Starting couch-splash before cage, with its picture ready and waiting for cage's socket, cut
that black from 0.77 s to 0.08 s (modeset at 15.38 s, splash on screen at 15.46 s). What's
left is cage's own first frame: its modeset commits an empty buffer. Couch's cage
(`cage/`, `-b`) draws the splash in that first frame instead. In `cage-check.sh`, on the Pi's
cage 0.3.1 and wlroots 0.20 (headless, so this is cage's first recorded frame, not the modeset):

| Run | Frames |
|---|---|
| The system's cage, with couch-splash | black×13 → splash×224 → ground×61 |
| Couch's cage, with couch-splash | splash×236 → ground×60: no black |
| The system's cage alone (`--no-splash`) | black×61 → white×91 → ground×151 |
| Couch's cage alone (`--no-splash`) | splash×61 → white×92 → ground×149 |
| Couch's cage's background against couch-splash's window | identical, all 921,600 pixels |

The hand-over frames: the splash, then the Startup screen's logo on the same pixels with its
bar and "Turning on the TV", then Home fading in.

Result: no white, and no black after the splash's first frame. From power-on to Home, the only
black left on screen is the 0.77 s after cage's modeset.

### Boot cuts

One is a follow-up; the other was considered and kept.

- **NetworkManager (2.92 s before network.target): #65.** `couchd.service` has `After=network.target`, and `couch-session.service` waits for couchd, so the network is on the launcher's critical path. couchd doesn't need the network to start: it checks connectivity itself, and YouTube and Plex wait for it later. Dropping `network.target` from `After=` takes NetworkManager off that path. The cost would be that couchd can start before the Wi-Fi is up, which it already handles. Measured: network.target at 10.08 s, couchd ready at 11.61 s; the critical chain to `couch-session.service` is couchd → network.target → NetworkManager → dbus.
- **`/boot/firmware` fsck (0.78 s): considered, kept.** Measured: no `systemd-fsck-root` unit ran (the initramfs checks root); `systemd-fsck@…-01` (`/boot/firmware`) took 0.78 s, 5.43 → 6.21 s. Setting the vfat partition's fstab pass to 0 would skip it. It stays at pass 2: #25's power-cut testing relied on this fsck, with `fsck.repair=yes`, to clear the FAT dirty bit after every one of its 5 hard resets. 0.78 s per boot is a fair price for that.

## Plex

Nothing from the Plex library is played: that would change its watch history and On Deck.
Our own clips from `scripts/perf/make-clips.sh` (ffmpeg's test pattern with noise, no
download) are copied to the Pi, played in Kodi straight from the file with
`run.sh plex play --file /var/tmp/couch-perf/<clip> --seconds 90 --label <name>` (files mode
644, so Kodi, running as `couch`, can read them), and deleted
afterwards. Reading the Plex add-on's and Kodi's state (`plex info`) is fine.

Kodi 21.3, PM4K (`script.plexmod`) 1.3.19. `videoplayer.adjustrefreshrate`: 2 (on start and stop).

| Clip | Decoder | Size | fps | Screen refresh | Open → playing | Stalls | CPU % |
|---|---|---|---|---|---|---|---|
| `couch-perf-4k-hevc10-23.976` (HEVC Main 10, ~40 Mb/s) | `ff-hevc-drm_prime` | 3840x2160 | 23.976 | 23.98 Hz | 0.26 s | 1, at 1.4 s | 6.5 (Kodi 23 % of a core) |
| `couch-perf-1080p-h264-60` (H.264 High, ~12 Mb/s) | `ff-h264_v4l2m2m-drm_prime` | 1920x1080 | 60 | 60 Hz | 0.56 s | 1, at 5.2 s | 28.7 (Kodi 110 % of a core; one core at 95–100 %) |
| `couch-perf-1080p-h264-23.976` (H.264 High, ~8 Mb/s) | `ff-h264_v4l2m2m-drm_prime` | 1920x1080 | 23.976 | 23.98 Hz | 0.52 s | 1, at 1.7 s | 3.8 (Kodi 11 % of a core) |

Each clip played 87–89 s. The one stall in each is the first seconds of playback, while the
refresh rate changes. The 1080p60 H.264 clip is the outlier: the decode is in hardware, yet
Kodi keeps one core busy, about five times what the 4K HEVC clip costs.

TV modes from EDID ("SONY TV"): up to 1920x1080, with 1080p24 (VIC 32), 1080p50 and 1080p60.
No 4K modes, so 4K plays scaled to 1080p.

## Temperature and throttling

| Run | Minutes | Temp start → max (°C) | ARM clock min (MHz) | Throttle flags |
|---|---|---|---|---|
| Idle on the launcher | 1 | 54.5 → 57.5 | 600 | 0x0 |
| Kodi 1080p60 H.264 clip | 1.5 | 58.9 → 60.9 | 1800 | 0x0 |
| Kodi 4K HEVC clip | 1.5 | 56.5 → 58.9 | 700 | 0x0 |
| After the suite | 0.5 | 55.0 → 57.0 | 700 | 0x0 |

No 30-minute run, since YouTube didn't play.

## Needs the user (morning)

- YouTube TV shows its "Who's watching?" picker after a deep link to a video. Pick the profile on the TV, then run `scripts/perf/run.sh youtube --short` (and `--minutes 30` if there's time) to fill in [YouTube](#youtube).
- Watch a cold boot, power-on to Home, for any black or white between the splash and Home, and for the logo moving. Expect about 0.77 s of black after the kernel splash, when cage takes the screen (#62).
- Watch YouTube 1080p60 and the test clips by eye: smoothness, judder at 23.976, and audio sync.

## Follow-ups

All optional, for later (label `polish`):

- #61: Chromium takes 10.6 s from cage's modeset to DevTools, the biggest part of the boot.
- #62: couch-splash can't use couch-fbsplash's RGB565 cache; a 32-bit frame would cut most of the 0.77 s of black after cage's modeset.
- #63: Kodi uses 110 % of a core for 1080p60 H.264, with hardware decode.
- #64: the launcher spends 2.6 s of 6.8 s of main-thread time recalculating style while the focus moves; 3 of 825 frames missed.
- #65: `couchd.service`'s `After=network.target` puts NetworkManager's 2.9 s on the launcher's critical chain.

Considered and kept: the `/boot/firmware` fsck (see [Boot cuts](#boot-cuts)).
