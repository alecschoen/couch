# Sony KDL-40V55 over HDMI-CEC

What Couch's TV, a 2010 Sony Bravia KDL-40V55 ("Bravia Sync"), sends and accepts over
HDMI-CEC, and what couchd does about it. The Pi is on HDMI 1.

Sources: COUCH-7 (CEC playback device, PR #37), COUCH-8 (remote keys, PR #39), and the
COUCH-9 probe on 17 Nov 2026 (cec-ctl on the Pi while couchd ran). **✓** = seen on this TV.
**?** = not seen yet: someone has to press the button. `scripts/cec-button-survey.sh`
checks those rows (see [Re-running the survey](#re-running-the-survey)).

PROBE_RESULTS

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
- **Hotplug drops** for under a second when the TV powers on and when it switches back to
  HDMI 1. Couch's address goes to f.f.f.f and comes back by itself. ✓
- **The TV never sends <Request Active Source>.** When it's turned on with its own remote it
  comes back on the input it was on. ✓
- couchd asks the TV's power status every 15 s, since the TV doesn't always say. ✓

## Volume (COUCH-11)

PROBE_AUDIO

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
