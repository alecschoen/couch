# Test on the real TV

One run through everything Couch does, with only the Sony remote, in about an hour. Each step has a box and what should happen. If
something doesn't, note the step's number (say **D4**) and what you saw, and carry on.
Section times are rough.

## Before you start (3 min)

- [ ] **0.1** On the Sony, with the Pi's input showing: Screen → Display Area is **Full Pixel**.
      Expected: the setting exists under that name.
- [ ] **0.2** Have nearby: a Bluetooth speaker or headphones (off), something plugged into
      the Pi's headphone jack, your phone, and a stopwatch.
- [ ] **0.3** The Plex server computer is on.
- [ ] **0.4** Put the TV in standby with its remote, then turn the Pi off: pull its power.

## A. Cold boot (4 min)

- [ ] **A1** Plug the Pi in and start the stopwatch. Expected: the TV wakes up by itself and
      switches to the Pi.
- [ ] **A2** Watch the screen. Expected: black, then the Couch logo on dark grey. Later the
      screen goes black once, for under a second (about 0.8 s, when the display session takes
      over), and the logo comes back on the same spot. No rainbow square, no text, no white
      flash, no blinking cursor and no mouse pointer at any point.
- [ ] **A3** Expected: a progress bar and "Turning on the TV" fade in under the logo. The logo
      doesn't move or flash while that happens.
- [ ] **A4** Expected: Home appears about 30–35 s after plugging in (measured overnight: 29 s
      after the kernel starts, plus the Pi's firmware), with no white flash just before it.
      Write down the time: ____ s, and how long the black in A2 lasts: ____ s.
- [ ] **A5** Expected: nothing is cut off at the edges. The clock at top left and "Sound: …" at
      top right have space around them.

## B. Home and the remote (5 min)

- [ ] **B1** Press Right through the cards: YouTube, Plex, Audio, Bluetooth, Settings. Expected:
      each one turns the whole screen its own colour and changes the big title. The colours
      look right on this TV (Plex's gold is the one most likely to look off).
- [ ] **B2** From the couch: the clock, the line under the title and "Sound: …" are easy to read.
- [ ] **B3** Hold Left down. Expected: focus moves along by itself after a moment, then faster.
      It stops when you let go, without running on past it.
- [ ] **B4** On Audio, press OK. Expected: a row of outputs (TV speakers, Headphone jack, and any
      connected Bluetooth device), with focus on the current one.
- [ ] **B5** Pick Headphone jack. Expected: a "Sound now on Headphone jack" message, and the
      top right says "Sound: Headphone jack". Press Return to leave the row.
- [ ] **B6** Press Red a few times. Expected: the sound goes TV → Headphone jack → … in turn,
      with a "Sound now on …" message each time. Leave it on TV speakers.
- [ ] **B7** Press Blue. Expected: the quick menu slides up from the bottom with Home, Sound,
      Volume and Bluetooth. Left and Right move between them; on Volume, Up and Down change the
      volume and the card shows it. While it's open, Home underneath doesn't move. Return closes
      it. Open it again and leave it: it closes by itself after about 30 seconds.
- [ ] **B8** Press Return on Home. Expected: nothing happens.

## C. Settings (5 min)

Open the Settings card. Up and Down pick a category; Right or OK goes into it; Return comes out.

- [ ] **C1** Display: "Resolution" says 1920 × 1080 · 60 Hz. Evening colors: After sunset /
      Always / Off. Dim to clock after: Left and Right change it.
- [ ] **C2** Sound: Output opens a list of outputs; picking one moves the sound. The two
      switches flip with OK. Volume limit changes with Left and Right.
- [ ] **C2b** UI sounds (#49), on by default. On Home and in Settings, from the couch:
      moving makes a soft tick, OK a short rising pair, Back a falling pair, and pushing
      against an edge a low thud. Hold Down in Settings: a light, steady ticking, not a
      rattle. Expected: quiet next to a video at the same volume, and no lag you can feel.
      Then try "TV only" with the sound on the jack (silent), and Off (silent). Write down
      any sound that's too loud, too sharp or annoying: ____. Then open Plex: Kodi's own
      clicks when you move, and none with the setting on Off. Put it back to On.
- [ ] **C3** Remote → Test your remote → OK. Press each button below and write what the box
      shows (it shows the name and the TV's code, or nothing if the TV keeps the button to
      itself). Return twice stops the test.

      | Button | Shows | Button | Shows |
      | --- | --- | --- | --- |
      | Up / Down / Left / Right | | Red | |
      | OK | | Green | |
      | OK held | "(held)"? | Yellow | |
      | Return | | Blue | |
      | Home | | Play | |
      | Options | | Pause | |
      | Guide | | Stop | |
      | Digits 0–9 | | ⏪ / ⏩ | |
      | Volume + / − / Mute | | ⏮ / ⏭ | |

      Expected: Red shows up as a key and the sound stays where it is; Blue shows up too and
      no quick menu opens. (Home still goes Home.)
- [ ] **C4** Updates: Launcher, Last check, System and Overnight rows, and a Check now button
      (more in K).
- [ ] **C5** About: Name on the TV is Couch, Device is the Raspberry Pi 4, Network says Ethernet
      and an address, and TV connection says the TV is on.
- [ ] **C6** Power: press OK once on Restart. Expected: it changes to "Press OK again to
      restart". Wait 4 s without pressing. Expected: it goes back to Restart.
- [ ] **C7** Compare the screen with the design, especially the Display and Power panels.
- [ ] **C8** Network: Connection says Ethernet, and Wi-Fi is off ("Off. Couch
      uses the network cable."). Couch is meant to be on the cable, so Wi-Fi stays off unless
      you want it. To try it: OK turns it on ("On. Not connected to a network." if the Pi
      knows no Wi-Fi network; one it knows, it joins within a few seconds); OK again turns
      it off. Leave it off.

## D. Bluetooth (5 min)

- [ ] **D1** Open the Bluetooth card. Expected: "Your devices" on the left, "Nearby" on the
      right with "Looking for devices".
- [ ] **D2** Put the speaker in pairing mode. Expected: it shows up under Nearby within about
      10 s, as "OK to pair".
- [ ] **D3** OK on it. Expected: "Pairing", then it moves to Your devices as "Connected · sound
      plays here", and a "Sound now on …" message. "Sound:" top right names it. Play something
      short (or wait for E) and listen: the sound really comes from it.
- [ ] **D4** Hold OK on that device for about a second. Expected: Options opens with
      Disconnect and Forget. Return closes it.
- [ ] **D5** Press Return to go Home. Expected: the Bluetooth card's line says the speaker is connected.
- [ ] **D6** Turn the speaker off. Expected: within a few seconds, "Sound now on TV speakers".
- [ ] **D7** Turn it back on and wait up to a minute. Expected: it reconnects by itself and
      the sound moves back to it ("Sound now on …").
- [ ] **D8** If you have headphones as well as a speaker: pair them too, from scratch, the same
      way (D2–D3), and listen. Then Red back to the speaker.
- [ ] **D9** Leave the speaker on for E. (The sound keeps going there.)

## E. YouTube (6 min)

- [ ] **E1** OK on YouTube. Expected: a red Opening screen, then YouTube's TV interface (not the
      website, no cookie banner), signed in to your account. If it shows "Who's watching?",
      pick your profile: Couch leaves that choice to you. (It can also come up a few seconds
      after a video opens.)
- [ ] **E2** Hold Down on the home grid. Expected: it scrolls smoothly and stops when you let go.
- [ ] **E3** Play a 1080p video and watch for a minute. Expected: no visible stutter, sound on
      the Bluetooth speaker.
- [ ] **E4** Pause, Play, Stop, ⏪ / ⏩ (if the TV sends them: C3). Expected: YouTube does each one.
- [ ] **E5** Press Red while it plays. Expected: the sound moves to the next output with a
      message on top of YouTube. Go back to the
      speaker with Red.
- [ ] **E6** Press Blue while it plays. Expected: the quick menu over YouTube, in YouTube's
      red, with Home, Sound, Volume and Bluetooth. Press Left, Right, Up, Down and OK on Volume
      (mute, then OK again to unmute). Expected: only the menu reacts; YouTube underneath doesn't
      move, seek or pause. Return closes it, and YouTube's own Return works again.
- [ ] **E7** Press Return a few times. Expected: YouTube steps back (player → grid → …). On
      YouTube's home, Return goes to Couch's Home.
- [ ] **E8** Open YouTube again. Expected: it comes straight back where you left it, without the
      Opening screen.
- [ ] **E9** Play something, then hold Return for about a second. Expected: Couch's Home, and the
      video is paused.

## F. TV remote volume (5 min)

With the sound off the TV, Couch takes over the TV remote's volume buttons (it tells the TV
it's the sound system). Settings › Sound › "TV remote controls volume" must be on (it is by
default).

- [ ] **F1** Red to the Bluetooth speaker. Expected: the TV's own speakers go quiet.
- [ ] **F2** Press the TV remote's Volume + and −, then hold Volume +. Expected: the speaker
      gets louder and quieter, and keeps going while you hold. Note whether the TV shows its
      own volume bar. Couch's own volume panel shows the new volume and goes away a couple of
      seconds after the last press.
- [ ] **F3** Mute, then Mute again. Expected: the speaker goes quiet, then comes back.
- [ ] **F4** With the sound still on the speaker, move around Home with the arrows, OK and
      Return. Expected: they all still work. If they don't, note it, turn "TV remote controls
      volume" off and carry on.
- [ ] **F5** Red to Headphone jack, Volume + and −. Expected: the same as F2, for the jack, and
      the TV's speakers stay quiet.
- [ ] **F6** Still on the jack, switch the TV to another input, then back to the Pi's. Expected:
      on the other input the TV's speakers work as usual; back on Couch, the volume buttons
      drive the jack again.
- [ ] **F7** Red to TV speakers. Expected: the TV's speakers are back, not muted, and Volume +
      and − work the TV's own volume as usual.
- [ ] **F8** Settings › Sound › Volume limit to 50. Red to the jack, hold Volume +. Expected:
      it stops at 50. Set the limit back to No limit and Red back to where you want it.

## G. Plex (5 min)

- [ ] **G1** OK on Plex. Expected: a gold Opening screen, then Plex. Watch the moment Plex takes
      over: the "Plex" title and the bar shouldn't jump. Kodi's own menus and home screen (plain
      Kodi) never show, here or anywhere below.
- [ ] **G2** Arrows, OK and Return move around Plex as you'd expect.
- [ ] **G3** Play a movie for a minute. Expected: picture and sound are fine (sound on whichever
      output is current).
- [ ] **G4** Press Blue while the movie plays. Expected: the quick menu over Plex, in Plex's
      gold (Kodi draws this one; nobody has seen it on the TV yet, so compare it with
      YouTube's in E6 and note anything off). Left, Right, Up, Down and OK on Volume move only
      the menu: Plex underneath doesn't move, seek or pause. Return closes it. If instead you
      land on Couch's Home with the menu open (and Plex stopped), note that: it's the fallback
      when Kodi can't draw the menu.
- [ ] **G4b** If the sound isn't on the TV speakers (Red to the speaker or the jack first),
      press the TV remote's Volume + and −. Expected: Couch's volume panel over Plex, as in F2.
- [ ] **G5** Stop, then Return until Plex asks "Are you ready to exit Plex?". Choose Exit.
      Expected: back on Couch's Home after a brief Couch logo (about half a second). It should
      read as a calm hand-over, not a flicker.
- [ ] **G6** (If there's time.) Open Plex again, then choose Minimize in that question.
      Expected: also back on Home.
- [ ] **G7** Open Plex again and hold Return for about a second. Expected: Couch's Home.

## H. TV off and on, inputs (4 min)

- [ ] **H1** Play a YouTube video, then turn the TV off with its remote. Turn it back on.
      Expected: the TV shows Couch and the video is paused.
- [ ] **H2** Same in Plex: play something, turn the TV off with its remote, turn it back on.
      Expected: Couch or Plex on screen, and the video is paused.
- [ ] **H3** Switch the TV to another input (TV, another HDMI), then back to the Pi's. Expected:
      Couch is where you left it, and the remote still works straight away.

## I. When things go wrong (4 min)

- [ ] **I1** Unplug the Pi's network cable (or turn off the internet router). Wait a minute, then
      OK on YouTube. Expected: "YouTube needs the internet" on a grey screen, with Try again
      and Go home.
- [ ] **I2** Plug the cable back in (or turn the router back on and wait for it), then Try again. Expected: YouTube opens.
- [ ] **I3** Turn off the Plex server computer (or quit Plex Media Server on it). OK on Plex.
      Expected: "Can't reach your Plex server", with Try again and Go home.
- [ ] **I4** Return. Expected: Home. Turn the Plex server back on and open Plex: it works.
- [ ] **I5** Turn the Bluetooth speaker off while YouTube plays. Expected: a message, and the
      sound carries on from the TV.

## J. Idle clock and evening colours (5 min)

- [ ] **J1** Settings › Display › Dim to clock after: 1 minute. Go to Home and don't touch
      anything for a minute. Expected: a dark screen with the time and date.
- [ ] **J2** Leave it for 3 minutes more (or come back to it later). Expected: the clock has
      moved to another spot, so nothing burns in.
- [ ] **J3** Press any button. Expected: Home comes back as you left it. That press only wakes
      the screen; it doesn't also move focus or open anything.
- [ ] **J4** Play a YouTube video and leave it for over a minute. Expected: no clock while it
      plays. Do the same in Plex.
- [ ] **J5** Settings › Display: under Evening colors it says today's sunset and sunrise (or
      "18:00 to 07:00 until the timezone is set"). Switch Off, then Always. Expected: Home's
      colours change between the two. With After sunset, they follow the sunset time shown.
- [ ] **J6** Set Dim to clock after back to 5 minutes. Then leave Home alone for 5 minutes:
      the clock comes up, and one press brings Home back without moving anything.
- [ ] **J7** "Turn the TV off when idle" is off by default. It turns the TV off after 2 hours on
      Couch with no buttons pressed, never while something plays. Leave it off unless you want
      it; it's too slow to test here.

## K. Updates (3 min)

Overnight updates (#24) are off until you turn them on, and the Pi can't read releases yet
(see "Updates" under "With a laptop"). This checks the screen before that.

- [ ] **K1** Settings › Updates. Expected: Launcher says "Couch (not from a release)" (nothing
      is released yet), System says "Off until overnight updates are turned on", and Overnight
      says "Not turned on yet" with "Check now still works" under it. No switch yet.
- [ ] **K2** Check now. Expected: the button says what it's doing for a moment, then Last check
      says "Not set up yet · just now", with the reason under it (no token, so Couch can't read
      its releases). Nothing else changes.
- [ ] **K3** After X1–X3 (any day): Check now says "Up to date", or installs the release, and
      the next time Home is on screen it says "Updated to …". Overnight is then a switch,
      "Install updates overnight", on.

## L. Restart, turn off, power cut (8 min)

- [ ] **L1** Settings › Power › Restart, OK twice. Expected: the Couch logo stays up while it
      shuts down (no text), then the boot from A2–A4 again.
- [ ] **L2** Open YouTube. Expected: still signed in.
- [ ] **L3** Open Plex. Expected: still signed in, no plex.tv/link code.
- [ ] **L4** Settings: the things you changed in C are still as you left them.
- [ ] **L5** Pull the Pi's power while YouTube plays. Plug it back in. Expected: it boots normally
      to Home, with no "Restore pages?" bubble anywhere. Do this three times; every boot is
      clean. (The full 20-pull run is under "With a laptop".)
- [ ] **L6** Settings › Power › Turn off, OK twice, with "Turn the TV off too" off. Expected: the
      Couch logo stays up while it shuts down, the Pi's green light stops, and the TV stays on.
- [ ] **L7** Unplug and replug the Pi. Then Settings › Power › "Turn the TV off too" on, and Turn
      off again. Expected: the same, and then the TV goes to standby.
- [ ] **L8** Unplug and replug the Pi to bring it back. Turn "Turn the TV off too" back off if
      you don't want it.

## With a laptop (optional, any time)

These need a terminal or GitHub, not just the remote.

### Sony buttons (10 min)

The Sony remote's buttons that nobody has recorded yet (#9). With the TV showing Couch, on
the Pi from a checkout of Couch:

```bash
sudo ./scripts/cec-button-survey.sh
sudo ./scripts/cec-button-survey.sh --volume
```

It asks for each button in turn. Paste the `summary.md` table it writes into the issue, or
give it to Claude: it fills in the "?" rows of the Sony notes (Home, Options, Guide, i+,
rewind and fast forward, previous and next, Prog + and −, Input, holds and repeats).
`cec-ctl --monitor-all` shows the same raw codes live, if you'd rather watch.

### Updates (15 min)

Overnight updates (#24) need three things from you first. The details are in
system/README.md, under "Updates" and "Release access". Until then Couch installs nothing by
itself, and Settings › Updates says "Not turned on yet".

- [ ] **X1** Release access. The repo is private, so the Pi needs a token to read releases.
      Recommended: on GitHub, Settings › Developer settings › Fine-grained tokens › Generate,
      for this repo only, with Contents: Read-only and nothing else, and an expiry you'll
      notice. Save it on the Pi:

      ```bash
      sudo sh -c 'umask 077; cat > /etc/couch/github-token'
      ```

      Paste it, Enter, Ctrl-D. Then Settings › Updates › Check now. Expected: "Up to date",
      with "No releases yet" under it, instead of "Not set up yet". (The other ways, a deploy key, a public mirror of the release files or a public
      repo, are in PR #54.)
- [ ] **X2** Make the first release: a GitHub release tagged `vX.Y.Z` (for example `v0.1.0`),
      not a draft or prerelease. Nothing is tagged yet. Check now again: it installs it, and
      Home says "Updated to v0.1.0" the next time it's on screen.
- [ ] **X3** Turn on overnight updates (Couch's releases, plus Debian's security updates):

      ```bash
      echo 'nightly = true' | sudo tee -a /etc/couch/update.toml
      sudo /opt/couch/source/install.sh
      ```

      Expected: Settings › Updates shows the "Install updates overnight" switch, on, and
      System says "Security updates overnight".
- [ ] **X4** Decide whether to keep the weekly full upgrade (`full_upgrade_days`, which also
      updates Chromium) and the reboot at night when one is needed (`reboot`). Both are on
      by default, in the same `/etc/couch/update.toml`. Leave them unless you'd rather not.

### Power cuts (45 min)

The full test for SD card protection (#25, docs/sd-card.md): 20 real plug pulls during use.
Overnight the Pi was only reset from software, and those resets checked Plex's settings
file, not that Plex was still signed in. So after **each** pull, check:

- the launcher comes back by itself;
- no "Restore pages?" bubble, on Home or in YouTube;
- YouTube is still signed in;
- a setting you changed is still changed;
- Plex opens signed in, with no plex.tv/link code.

- [ ] **X5** 8 pulls with YouTube playing.
- [ ] **X6** 4 on the launcher, within 2 s of changing a setting (before it's saved).
- [ ] **X7** 4 with Plex playing.
- [ ] **X8** 2 within 10 s of power-on, during the boot.
- [ ] **X9** 2 while a Bluetooth speaker is connected.
- [ ] **X10** After all 20, from the laptop:

      ```bash
      ssh couch.local 'journalctl -k -b | grep -iE "ext4|fat|mmc"'
      ```

      Expected: only the normal journal recovery lines.
- [ ] **X11** (If curious.) How much a boot writes to the card:

      ```bash
      ssh couch.local "awk '{print \$7 * 512 / 1e6 \" MB written since boot\"}' /sys/block/mmcblk0/stat"
      ```

      Expected: about 5–7 MB right after a boot, as measured overnight.

### Performance (20 min)

From a checkout of Couch on the PC, with SSH to couch.local. The overnight numbers and what
each script does are in docs/performance.md (#23). Give Claude the results, or the JSON
files under `perf-results/<date>/`, for that page.

- [ ] **P1** YouTube. On the TV, open YouTube and pick your profile on "Who's watching?": the
      overnight run couldn't, so YouTube isn't measured yet. Go back Home, then:

      ```bash
      scripts/perf/run.sh youtube --short
      ```

      It plays a 1080p60 video for 3 minutes. Expected: under 0.5 % dropped frames and a
      hardware decoder. If the picker comes back during the run, pick your profile again
      and run it again. With time to spare, `--minutes 30` instead of `--short`.
- [ ] **P2** The boot recording. Overnight, every frame from the session splash to Home was
      recorded (480 × 270). It's in the main Couch checkout on the PC,
      `perf-results/2026-11-16/session-072302-15.27.mkv`.
      Open it in a video player that steps one frame at a time (VLC: E; mpv: `.`).
      Expected: the logo, then the Startup screen's logo on the same pixels with its bar and
      "Turning on the TV", then Home fading in. No black or white frame, and the logo doesn't jump. (The recording starts at the
      splash's first frame, so A2's short black isn't in it; A2 is the check for that.)
- [ ] **P3** Kodi at 1080p60. Overnight, our own 1080p60 H.264 test clip played with hardware
      decoding, yet Kodi used 110 % of a core (one core at 95–100 %), against 23 % for 4K
      HEVC. If your Plex library has something at 60 fps (sport, a phone video), play it in
      Plex and run:

      ```bash
      scripts/perf/run.sh plex watch --seconds 120 --label 1080p60
      ```

      Note Kodi's CPU % (the result's `system` summary, per program): ____ %, and whether
      the picture stutters. (It only reads Kodi's player state; it doesn't press anything or
      touch your watch history.)

### Later, on your own

- Plex: if you like, set up Plex's user select or automatic sign-in in Plex's own settings.

## When you're done

Tell Claude the step numbers that failed and what you saw. Fill in C3's table too: it
decides what the remote's buttons do.

## Where these come from

| Area | Issues and PRs |
| --- | --- |
| Boot splash, Full Pixel | #4, PR #42 |
| Startup, Opening, error screens | #19, PR #44 |
| CEC, TV off/on | #7 (PR #37), #9 (PR #52) |
| Remote keys, holds | #8, PR #39 |
| Home | #15, PR #41 |
| Bluetooth | #12 (PR #35), #16 (PR #40) |
| Settings, Restart, Turn off | #17, PR #48 |
| YouTube | #21, PR #45 |
| Plex via Kodi | #22, PR #43 |
| Sound outputs | #10, PR #34 |
| Quick menu, messages, volume panel | #18, PR #56 |
| TV remote volume | #11, PR #53 |
| Idle clock, evening colours, TV off when idle | #20, PR #50 |
| Updates | #24, PR #54 |
| Power cuts, SD card | #25, PR #51 |
| Wi-Fi option | #31, PR #57 |
| Performance, boot splash without black or white | #23, PR #60 |
