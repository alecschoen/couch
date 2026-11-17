"""Remote buttons, by name. These names are what the UI receives in `key` messages.

The CEC backend (couchd/cec_tv.py) maps the TV remote onto them; #8 adds key repeat and
per-app routing. In dev mode, and with a USB keyboard on the Pi, the launcher maps keyboard
keys onto them (launcher/ui/main.slint) and sends `remote.press`.
"""

REMOTE_KEYS = frozenset(
    {
        "up",
        "down",
        "left",
        "right",
        "select",
        "back",
        "home",
        "play",
        "pause",
        "play_pause",
        "stop",
        "rewind",
        "fast_forward",
        "red",
        "green",
        "yellow",
        "blue",
        *(str(n) for n in range(10)),
    }
)
