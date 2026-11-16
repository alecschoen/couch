# Sony KDL-40V55 over HDMI-CEC

What Couch's TV, a 2010 Sony Bravia KDL-40V55 ("Bravia Sync"), sends and accepts over
HDMI-CEC, and what couchd does about it. The Pi is on HDMI 1.

Sources: COUCH-7 (CEC playback device, PR #37), COUCH-8 (remote keys, PR #39), and the
COUCH-9 probe on 16 Nov 2026 (cec-ctl on the Pi while couchd ran). **✓** = seen on this TV.
**?** = not seen yet: someone has to press the button. `scripts/cec-button-survey.sh`
checks those rows (see [Re-running the survey](#re-running-the-survey)).

## What the TV says about itself

Asked from the Pi with `cec-ctl --to 0` while couchd ran:

| Question | Answer |
| --- | --- |
| CEC version | 1.3a ✓ |
| Vendor ID | 0x080046, Sony ✓ |
| OSD name | "TV" ✓ |
| Physical address | 0.0.0.0, logical address 0 (TV) ✓ |
| Menu language | eng ✓ |
| Power status | on, standby, or to-on while it wakes ✓ |
| <Give Audio Status>, <Give System Audio Mode Status>, <System Audio Mode Request> | Feature Abort, "unrecognized opcode" ✓. The TV has no amplifier of its own to report on. |
| <Give Features> | Feature Abort, "unrecognized opcode" ✓ (CEC 2.0; the TV is 1.3a) |
| Reply time | 9–12 ms ✓ |
| Other devices on the bus | none: the TV and Couch ✓ |

The Pi's side: the vc4_hdmi adapter (kernel 6.18) does CEC 1.4 at physical address 1.0.0.0
(HDMI 1), and it holds **one logical address**. So couchd is either Playback Device 1 (4)
or Audio System (5), never both at once (see [Volume](#volume-couch-11)). It can't watch
the whole bus: `cec-ctl --monitor-all` falls back to `--monitor`, which only shows frames
to and from Couch's address and broadcasts.

What couchd logs about CEC at the default `info` level: "claimed CEC address", "lost CEC
address", and "TV power=… showing_couch=…" on each change. COUCH-11 adds "TV remote
volume", "claiming CEC addresses roles=…" and "System Audio Mode on=…". Every frame sent
and received ("cec tx", "cec rx"), remote buttons, unmapped buttons and Feature Aborts are
logged at `debug` only (`[log] level = "debug"` in /etc/couch/config.toml).

## Remote buttons

The TV only forwards buttons to the input it's showing. Each press is a
<User Control Pressed> with the code below, and letting go is a <User Control Released>.
A press-and-release takes about 135 ms (COUCH-7).

| Button | CEC code | couchd key | What it does in Couch | Seen |
| --- | --- | --- | --- | --- |
| Up / Down / Left / Right | 0x01–0x04 | `up` `down` `left` `right` | Move; repeat while held (couchd's own repeat) | ✓ |
| OK | 0x00 Select | `select` | Select; held 800 ms = long press | ✓ |
| Return | 0x0D Exit | `back` | Back; held 800 ms = Home | ✓ |
| Home | 0x09 Root Menu? | `home` | Home | ? COUCH-7 saw 0x09 once, COUCH-8 thought it isn't forwarded |
| Options | ? | | | ? |
| Sync Menu | (TV's own menu) | | Opens the TV's Bravia Sync menu; not forwarded | ? |
| Guide | ? | | | ? |
| i+ / Info | ? | | | ? |
| Red | 0x72 F2 | `red` | Next sound output | ✓ |
| Green | 0x73 F3 | `green` | Kept for couchd | ✓ |
| Yellow | 0x74 F4 | `yellow` | Kept for couchd | ✓ |
| Blue | 0x71 F1 | `blue` | Quick menu | ✓ |
| Play | 0x44 | `play` | Typed into apps | ✓ |
| Pause | 0x46 | `pause` | Typed into apps | ✓ |
| Stop | 0x45 | `stop` | Typed into apps | ✓ |
| Rewind / Fast forward | 0x48 / 0x49? | `rewind` `fast_forward` | Typed into apps | ? |
| Previous / Next | 0x4C / 0x4B? | | Not mapped | ? |
| Digits 0–9 | 0x20–0x29 | `0`–`9` | Typed into apps | not forwarded (COUCH-7) |
| Prog + / − | 0x30 / 0x31? | | Not mapped | ? |
| Volume + / − / Mute | 0x41 / 0x42 / 0x43 | | Only sent to an Audio System; see [Volume](#volume-couch-11) | ? |
| Input, Power, Text, Wide… | | | The TV's own | ? |

Also seen:

- **TV standby** makes couchd send `pause` to whatever is playing (COUCH-7). After Standby the
  TV broadcasts <Vendor Command With ID> `08:00:46` (Sony) with payload `00:09:00:01`.

## Holds and repeats

| | Seen |
| --- | --- |
| Does the TV repeat <User Control Pressed> while a button is held? | ? |
| Does a held button end with <User Control Released>? | ✓ for a short press (COUCH-7); ? for a long hold |

couchd doesn't rely on the TV's repeats: it ignores them and repeats arrows itself (first
after 400 ms, then faster down to every 60 ms), and lets go of a button after 15 s if no
release comes (couchd/remote.py).

## Power and inputs

- **couchd wakes the TV on every start**: <Image View On>, then <Active Source>. ✓
- **Standby** (`tv.standby`, <Standby> to the TV): the TV reports standby to the next power
  query ~130 ms later. couchd pauses playback. ✓
- **Wake** (`tv.power_on`, <Image View On> then <Active Source>): the TV reports "to-on"
  ~190 ms later. It then broadcasts <Report Physical Address>, <Device Vendor ID>, and
  two <Vendor Command With ID> Sony frames with payloads `00:04:00:01` and `00:08:00:00`,
  and asks Couch for its OSD name and vendor ID. ✓
- **The hotplug drops 12.3 s after <Image View On>**, for 0.6 s (f.f.f.f → 1.0.0.0). couchd
  claims its address again; TV power reads "unknown" for ~3 s until the next answer. While
  the hotplug is down the Pi's HDMI sink disappears and sound falls back to the jack for
  about a second. couchd waits 2 s for the output to settle before taking the Audio System
  address, so the blip doesn't change it (COUCH-11). ✓
- **Hotplug drops** for under a second when the TV powers on and when it switches back to
  HDMI 1. Couch's address goes to f.f.f.f and comes back by itself. ✓
- **The TV never sends <Request Active Source>.** When it's turned on with its own remote it
  comes back on the input it was on. ✓
- couchd asks the TV's power status every 15 s, since the TV doesn't always say. ✓

## Volume (COUCH-11)

The TV only sends its volume buttons to an Audio System (logical address 5) that has System
Audio Mode on. It has no audio status of its own: asked by Playback Device 1, it
Feature-Aborts all three audio queries (above).

**It accepts Couch as the Audio System.** With the adapter's one address, couchd gives up 4
and claims 5 while sound is on the jack, Bluetooth or USB, and goes back to 4 when sound is
on the TV (COUCH-11). Seen on the bus, 16 Nov 2026, with sound moved to the jack:

- The claim takes ~150 ms. couchd then broadcasts <Report Physical Address> (Audio System),
  <Device Vendor ID>, <Active Source> 1.0.0.0 and <Set System Audio Mode> on. ✓
- 80 ms after the claim the TV asks 5 for <Give System Audio Mode Status>, and later for
  <Give OSD Name>, <Give Device Vendor ID> and <Give Audio Status>. couchd answers each one.
  The TV broadcasts <Routing Change> 0.0.0.0 → 1.0.0.0, so it treats 5 as the active
  source on HDMI 1. ✓
- The TV sends no Feature Abort for <Set System Audio Mode> or <Report Audio Status>. ✓
- After that it asks nothing more: 25 s went by with no audio polls. ✓
- couchd's volume handling, fed through `tv.simulate`: frames from the TV to 5 were
  Vol+ ×2, Vol− and Mute ×2, then Vol+ held 1.2 s. They took the jack from 40 to 42, 44
  and 42, muted and unmuted it, then took it to 52 (couchd's own repeat, no TV repeats).
  Each change sent <Report Audio Status> to the TV. A Vol+ to 5 after sound went back to
  the TV was ignored. ✓
- Moving sound back to the TV broadcasts <Set System Audio Mode> off, then claims 4 again
  and re-announces <Active Source>. The TV sends <Routing Change> again and asks 4 for its
  name. ✓

Not seen yet, because it needs someone at the TV:

- ? Does the TV mute its own speakers in System Audio Mode, and unmute them when it ends?
- ? Does it send Vol+ / Vol− / Mute to 5? (`sudo ./scripts/cec-button-survey.sh --volume`)
- ? Does it still send arrows, OK and Return to Couch while Couch is at 5? If it doesn't,
  turn off `audio.tv_remote_volume`. The quick menu's volume (`audio.step_volume`) still
  works.
- ? Does it show its own volume bar from <Report Audio Status>?

## Re-running the survey

On the Pi, with the TV on and showing Couch:

```bash
sudo ./scripts/cec-button-survey.sh
```

It asks for each button in turn: press it on the TV remote, and the script records what the
TV sent, whether a release came, the gap between repeats while held, and how long to the
release. A button the TV doesn't forward times out after 12 s. Enter skips one.
`--volume` does only the volume buttons (it moves sound to the headphone jack first, so
Couch is the TV's Audio System, and back afterwards). Results go to
`./cec-survey-<date>/summary.md`; paste that table under the button table here, and
update the key map in `couchd/cec_tv.py` (`UI_COMMANDS`) if a code turns up.
