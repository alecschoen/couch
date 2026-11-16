"""YouTube's TV interface (youtube.com/tv) in the kiosk Chromium.

youtube.com/tv only serves the TV interface to browsers that look like a TV; anything else
is sent to desktop YouTube. So the YouTube tab gets a Samsung Tizen TV user agent, set on
that tab alone over DevTools, and `youtube.js` runs in it before YouTube's own scripts:

- it turns down VP9 and AV1, so YouTube streams H.264, which the Pi 4 decodes in hardware;
- Back from YouTube's top level (it calls `window.close()`) brings couchd back home.

Sign-in is YouTube's own "Sign in with your phone" (yt.be/activate). The cookies live in the
browser profile, /var/lib/couch/chromium, so it's once per install.
"""

from pathlib import Path

from couchd.chromium import PageSetup

URL = "https://www.youtube.com/tv"
MATCH = "https://www.youtube.com/"

# A 2021 Samsung TV. Any Tizen TV works; a desktop or Linux user agent gets desktop YouTube.
USER_AGENT = (
    "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 (KHTML, like Gecko) "
    "85.0.4183.93/6.5 TV Safari/537.36"
)

SCRIPT = Path(__file__).with_name("youtube.js").read_text(encoding="utf-8")

SETUP = PageSetup(user_agent=USER_AGENT, script=SCRIPT)
