# Test on the real TV

One run through everything Couch does, with only the Sony remote, in about 45 minutes (10 more
with the pending features). Each
step has a box and what should happen. If something doesn't, note the step's number (say
**D4**) and what you saw, and carry on. Section times are rough.

Steps marked **(pending merge)** are features still in review. Skip them if they aren't on
the Pi yet.

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
- [ ] **A2** Watch the screen. Expected: black, then the Couch logo on dark grey. No rainbow
      square, no text, no blinking cursor at any point.
- [ ] **A3** Expected: a progress bar and "Turning on the TV" fade in under the logo. The logo
      doesn't move or flash while that happens.
- [ ] **A4** Expected: Home appears within about 15 s of plugging in. Write down the time: ____ s.
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
- [ ] **B7** Press Blue. Expected: the quick menu slides up from the bottom. Return closes it.
- [ ] **B8** Press Return on Home. Expected: nothing happens.

## C. Settings (5 min)

Open the Settings card. Up and Down pick a category; Right or OK goes into it; Return comes out.

- [ ] **C1** Display: "Resolution" says 1920 × 1080 · 60 Hz. Evening colors: After sunset /
      Always / Off. Dim to clock after: Left and Right change it.
- [ ] **C2** Sound: Output opens a list of outputs; picking one moves the sound. The two
      switches flip with OK. Volume limit changes with Left and Right.
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

      Note: Red still switches the sound while the test runs. Switch it back with Red if
      you need to.
- [ ] **C4** Updates: shows "Couch" and a version, and the overnight switch. **(pending merge)**
      Check now looks for an update and says what it found.
- [ ] **C5** About: Name on the TV is Couch, Device is the Raspberry Pi 4, Network shows the Wi-Fi
      or cable in use and an address, and TV connection says the TV is on.
- [ ] **C6** Power: press OK once on Restart. Expected: it changes to "Press OK again to
      restart". Wait 4 s without pressing. Expected: it goes back to Restart.
- [ ] **C7** Compare the screen with the design, especially the Display and Power panels.

## D. Bluetooth (5 min)

- [ ] **D1** Open the Bluetooth card. Expected: "Your devices" on the left, "Nearby" on the
      right with "Looking for devices".
- [ ] **D2** Put the speaker in pairing mode. Expected: it shows up under Nearby within about
      10 s, as "OK to pair".
- [ ] **D3** OK on it. Expected: "Pairing", then it moves to Your devices as "Connected · sound
      plays here", and a "Sound now on …" message. "Sound:" top right names it.
- [ ] **D4** Hold OK on that device for about a second. Expected: Options opens with
      Disconnect and Forget. Return closes it.
- [ ] **D5** Press Return to go Home. Expected: the Bluetooth card's line says the speaker is connected.
- [ ] **D6** Turn the speaker off. Expected: within a few seconds, "Sound now on TV speakers".
- [ ] **D7** Turn it back on and wait up to a minute. Expected: it reconnects by itself and
      the sound moves back to it ("Sound now on …").
- [ ] **D8** Leave the speaker on for E. (The sound keeps going there.)

## E. YouTube (6 min)

- [ ] **E1** OK on YouTube. Expected: a red Opening screen, then YouTube's TV interface (not the
      website, no cookie banner), signed in to your account.
- [ ] **E2** Hold Down on the home grid. Expected: it scrolls smoothly and stops when you let go.
- [ ] **E3** Play a 1080p video and watch for a minute. Expected: no visible stutter, sound on
      the Bluetooth speaker.
- [ ] **E4** Pause, Play, Stop, ⏪ / ⏩ (if the TV sends them: C3). Expected: YouTube does each one.
- [ ] **E5** Press Red while it plays. Expected: the sound moves to the next output with a
      message on top of YouTube **(pending merge: the message over YouTube)**. Go back to the
      speaker with Red.
- [ ] **E6** Press Blue. **(pending merge)** Expected: the quick menu over YouTube, in YouTube's
      red, with Home, Sound, Volume and Bluetooth. Return closes it.
- [ ] **E7** Press Return a few times. Expected: YouTube steps back (player → grid → …). On
      YouTube's home, Return goes to Couch's Home.
- [ ] **E8** Open YouTube again. Expected: it comes straight back where you left it, without the
      Opening screen.
- [ ] **E9** Play something, then hold Return for about a second. Expected: Couch's Home, and the
      video is paused.

## F. TV remote volume (3 min) (pending merge)

- [ ] **F1** With the sound on the Bluetooth speaker, press the TV remote's Volume + and −.
      Expected: the speaker gets louder and quieter, and a volume panel shows on screen.
- [ ] **F2** Mute. Expected: the speaker goes quiet; Mute again brings it back.
- [ ] **F3** Red to Headphone jack, Volume + and −. Expected: the same, for the jack.
- [ ] **F4** Red to TV speakers, Volume + and −. Expected: the TV's own volume, as usual.
- [ ] **F5** Settings › Sound › Volume limit to 50. Volume + on the speaker. Expected: it stops
      at 50. Set it back to No limit.

## G. Plex (5 min)

- [ ] **G1** OK on Plex. Expected: a gold Opening screen, then Plex. Watch the moment Plex takes
      over: the "Plex" title and the bar shouldn't jump. Kodi's own menus never show.
- [ ] **G2** Arrows, OK and Return move around Plex as you'd expect.
- [ ] **G3** Play a movie for a minute. Expected: picture and sound are fine (sound on whichever
      output is current).
- [ ] **G4** Press Blue. **(pending merge)** Expected: the quick menu, or Couch's Home with the
      menu open if Plex can't show it.
- [ ] **G5** Stop, then Return until Plex asks "Are you ready to exit Plex?". Choose Exit.
      Expected: back on Couch's Home after a brief Couch logo (about half a second).
- [ ] **G6** (If there's time.) Open Plex again, then choose Minimize in that question.
      Expected: also back on Home.
- [ ] **G7** Open Plex again and hold Return for about a second. Expected: Couch's Home.

## H. TV off and on, inputs (3 min)

- [ ] **H1** Play a YouTube video, then turn the TV off with its remote. Turn it back on.
      Expected: the TV shows Couch and the video is paused.
- [ ] **H2** Switch the TV to another input (TV, another HDMI), then back to the Pi's. Expected:
      Couch is where you left it, and the remote still works straight away.

## I. When things go wrong (4 min)

- [ ] **I1** Turn off the Wi-Fi router (or unplug the Pi's network cable). Wait a minute, then
      OK on YouTube. Expected: "YouTube needs the internet" on a grey screen, with Try again
      and Go home.
- [ ] **I2** Turn the router back on, wait for it, then Try again. Expected: YouTube opens.
- [ ] **I3** Turn off the Plex server computer (or quit Plex Media Server on it). OK on Plex.
      Expected: "Can't reach your Plex server", with Try again and Go home.
- [ ] **I4** Return. Expected: Home. Turn the Plex server back on and open Plex: it works.
- [ ] **I5** Turn the Bluetooth speaker off while YouTube plays. Expected: a message, and the
      sound carries on from the TV.

## J. Idle clock and evening colours (4 min) (pending merge)

- [ ] **J1** Settings › Display › Dim to clock after: 1 minute. Go to Home and don't touch
      anything for a minute. Expected: a dark screen with the time and date.
- [ ] **J2** Wait a few minutes more (or come back to it later). Expected: the clock has moved
      a little, so nothing burns in.
- [ ] **J3** Press any button. Expected: Home comes back. That press only wakes the screen; it
      doesn't also move focus or open anything.
- [ ] **J4** Play a YouTube video and leave it for over a minute. Expected: no clock while it
      plays.
- [ ] **J5** Evening colors: Off, then Always. Expected: Home's colours change between the two.
      With After sunset, they switch on their own after sunset.
- [ ] **J6** Set Dim to clock after back to 5 minutes.

## K. Updates (2 min) (pending merge)

- [ ] **K1** Settings › Updates: the switch "Install updates overnight" is on.
- [ ] **K2** Check now. Expected: it says whether there's an update. If one installs (now or
      overnight), the next time Home opens it says "Updated to …".

## L. Restart, turn off, power cut (6 min)

- [ ] **L1** Settings › Power › Restart, OK twice. Expected: the Couch logo stays up while it
      shuts down (no text), then the boot from A2–A4 again.
- [ ] **L2** Open YouTube. Expected: still signed in.
- [ ] **L3** Open Plex. Expected: still signed in, no plex.tv/link code.
- [ ] **L4** Settings: the things you changed in C are still as you left them.
- [ ] **L5** Pull the Pi's power while YouTube plays. Plug it back in. Expected: it boots normally
      to Home. **(pending merge)** Do this three times; every boot is clean.
- [ ] **L6** Settings › Power › "Turn the TV off too" on. Then Turn off, OK twice. Expected: the
      Couch logo, then the Pi's green light stops, and the TV goes to standby.
- [ ] **L7** Unplug and replug the Pi to bring it back. Turn "Turn the TV off too" back off if
      you don't want it.

## When you're done

Tell Claude the step numbers that failed and what you saw. Fill in C3's table too: it
decides what the remote's buttons do.

## Where these come from

| Area | Issues and PRs |
| --- | --- |
| Boot splash, Full Pixel | #4, PR #42 |
| Startup, Opening, error screens | #19, PR #44 |
| CEC, TV off/on | #7 (PR #37), #9 |
| Remote keys, holds | #8, PR #39 |
| Home | #15, PR #41 |
| Bluetooth | #12 (PR #35), #16 (PR #40) |
| Settings, Restart, Turn off | #17, PR #48 |
| YouTube | #21, PR #45 |
| Plex via Kodi | #22, PR #43 |
| Sound outputs | #10, PR #34 |
| Quick menu, messages, volume panel | #18 (pending) |
| TV remote volume | #11 (pending) |
| Idle clock, evening colours | #20 (pending) |
| Updates | #24 (pending) |
| Power cuts | #25 (pending) |
