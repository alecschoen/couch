"""Dev mode only: open the launcher in a Chromium app window at the TV's resolution.

Chromium, because that's what the Pi runs, so layout and video behave the same. Set
COUCH_BROWSER to use a specific binary. Without Chromium we just log the URL.
"""

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome-stable",
    "google-chrome",
    "brave",
    "brave-browser",
)
WIDTH, HEIGHT = 1920, 1080


def find_browser() -> str | None:
    override = os.environ.get("COUCH_BROWSER")
    if override:
        return shutil.which(override) or override
    return next((path for name in CANDIDATES if (path := shutil.which(name))), None)


def browser_args(url: str, profile: Path) -> list[str]:
    return [
        f"--app={url}",
        f"--window-size={WIDTH},{HEIGHT}",
        "--window-position=0,0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
    ]


class DevBrowser:
    def __init__(self, profile: Path) -> None:
        self.profile = profile
        self._process: asyncio.subprocess.Process | None = None

    async def open(self, url: str) -> None:
        binary = find_browser()
        if binary is None:
            log.warning(
                "Chromium not found; open the launcher yourself or set COUCH_BROWSER",
                extra={"url": url},
            )
            return
        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            self._process = await asyncio.create_subprocess_exec(
                binary,
                *browser_args(url, self.profile),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            log.warning("couldn't start the browser", extra={"browser": binary, "error": str(e)})
            return
        log.info("opened the launcher", extra={"browser": binary, "url": url})

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            async with asyncio.timeout(3):
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
