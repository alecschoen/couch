"""YouTube Music (music.youtube.com) in the kiosk Chromium, made usable with the TV remote.

A prototype. music.youtube.com is made for a mouse, so `ytmusic.js` runs in its tab before
its own scripts and adds what a remote needs: a highlight, the arrow keys moving it to
what's on screen in that direction, OK clicking it, Back closing menus and the player page
and going back, and the media keys driving the player. The tab is laid out at 1280x720 and
drawn 1.5x, so YouTube Music sizes itself for a small screen and reads from the sofa.

It gets the desktop site with Chromium's own user agent, and couchd's overlays.

Signing in is Google's normal web sign-in, in the shared browser profile
(/var/lib/couch/chromium); YouTube's TV sign-in doesn't carry over.
"""

from pathlib import Path

from couchd import overlay_page
from couchd.chromium import PageSetup

URL = "https://music.youtube.com/"
MATCH = "https://music.youtube.com/"

SCRIPT = Path(__file__).with_name("ytmusic.js").read_text(encoding="utf-8") + overlay_page.SCRIPT

SETUP = PageSetup(script=SCRIPT, viewport=(1280, 720, 1.5))
