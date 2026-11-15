"""Remote buttons, by name. These names are what the UI receives in `key` messages.

The CEC service (#8) maps the TV remote onto them. In dev mode, and with a USB keyboard on
the Pi, the UI maps keyboard keys onto them (ui/js/keys.js) and sends `remote.press`.
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
