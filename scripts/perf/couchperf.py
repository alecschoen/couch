#!/usr/bin/env python3
"""Couch performance checks (#23). Measure, don't guess.

Runs on the Pi with couchd's Python, which has aiohttp; nothing is installed or changed:

    ssh couch.local /opt/couch/.venv/bin/python - youtube --minutes 30 < scripts/perf/couchperf.py

or through scripts/perf/run.sh, which does that and keeps the JSON on the PC. Each check
prints one JSON document on stdout; progress goes to stderr. Long checks also save what they
have so far to --out on the Pi (default /tmp/couch-perf/<check>.json), so a dropped SSH
connection doesn't lose a 30-minute run.

Checks:
  sample    CPU, temperature, clocks, throttling and memory for a while (a baseline)
  youtube   YouTube 1080p60: dropped frames from the player, decoder, CPU, temperature and
            throttling over --minutes (30 for the real run, --short for 3)
  launcher  Home's focus animation: frame times from Chromium's trace while keys are pressed
  launch    launcher → YouTube (cold and warm), launcher → Plex (Kodi), Kodi → launcher
  boot      the last boot: journal milestones, Chromium's start-up, the launcher's first paint
  plex      Kodi's side: `plex info` (settings, display modes), `plex watch` (what Kodi plays
            and how), `plex nav` (move around PM4K over JSON-RPC and say what's focused)

Talks only to loopback: couchd's API (8765), Chromium's DevTools (9222), Kodi's JSON-RPC
(9090). It never signs anything in or out and doesn't touch Chromium's or Kodi's profiles.
Remote presses go through couchd's API, as the TV remote's would.
"""

import argparse
import asyncio
import contextlib
import itertools
import json
import math
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:  # unit tests import the pure parts without it
    aiohttp = None

API = os.environ.get("COUCH_API", "http://127.0.0.1:8765")
CDP = os.environ.get("COUCH_CDP", "http://127.0.0.1:9222")
KODI = (
    os.environ.get("COUCH_KODI_HOST", "127.0.0.1"),
    int(os.environ.get("COUCH_KODI_PORT", 9090)),
)
OUT_DIR = Path(os.environ.get("COUCH_PERF_OUT", "/tmp/couch-perf"))
# Big Buck Bunny at 60 fps (Blender Foundation, 10 min, SDR), which YouTube also serves as
# H.264 1080p60 (itag 299); --video picks another.
DEFAULT_VIDEO = "aqz-KE-bpKQ"
FRAME_MS = 1000 / 60
LCP_QUIET, LCP_MAX = 3.0, 30.0
T0 = time.monotonic()


def log(*parts: object) -> None:
    print(f"[{time.monotonic() - T0:7.1f}s]", *parts, file=sys.stderr, flush=True)


# --- small statistics ---------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarize(values: list[float], digits: int = 2) -> dict[str, Any]:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    r = lambda v: None if v is None else round(v, digits)  # noqa: E731
    return {
        "n": len(values),
        "mean": r(statistics.fmean(values)),
        "p50": r(percentile(values, 50)),
        "p95": r(percentile(values, 95)),
        "p99": r(percentile(values, 99)),
        "max": r(max(values)),
        "min": r(min(values)),
    }


def frame_intervals(timestamps: list[float]) -> list[float]:
    ts = sorted(timestamps)
    return [b - a for a, b in itertools.pairwise(ts)]


def jank(intervals_ms: list[float], frame_ms: float = FRAME_MS) -> dict[str, Any]:
    """Frame intervals, and how many frames were missed: an interval of ~2 frames is one
    missed frame, and so on."""
    missed = sum(max(0, round(i / frame_ms) - 1) for i in intervals_ms)
    return {
        **summarize(intervals_ms),
        "over_1.5_frames": sum(1 for i in intervals_ms if i > 1.5 * frame_ms),
        "missed_frames": missed,
    }


# --- the Pi itself ------------------------------------------------------------------------

THROTTLE_BITS = {
    0: "under-voltage",
    1: "arm frequency capped",
    2: "throttled",
    3: "soft temperature limit",
    16: "under-voltage has occurred",
    17: "arm frequency capping has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}


def decode_throttled(value: int | None) -> list[str]:
    if value is None:
        return []
    return [name for bit, name in THROTTLE_BITS.items() if value & (1 << bit)]


def parse_throttled(text: str) -> int | None:
    """`throttled=0x50005` (vcgencmd) or `50005` (sysfs) → int."""
    m = re.search(r"(?:0x)?([0-9a-fA-F]+)\s*$", text.strip())
    return int(m.group(1), 16) if m else None


def read(path: str | Path, default: str | None = None) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return default


def run(cmd: list[str], timeout: float = 10.0, sudo: bool = False) -> str | None:
    """stdout of a command, or None. With `sudo`, retries with `sudo -n` (never prompts)."""
    for attempt in (cmd, ["sudo", "-n", *cmd] if sudo else None):
        if attempt is None:
            break
        try:
            out = subprocess.run(attempt, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0:
            return out.stdout
    return None


def throttled() -> int | None:
    sysfs = read("/sys/devices/platform/soc/soc:firmware/get_throttled")
    if sysfs is not None:
        return parse_throttled(sysfs)
    out = run(["vcgencmd", "get_throttled"])
    return parse_throttled(out) if out else None


def temperature() -> float | None:
    raw = read("/sys/class/thermal/thermal_zone0/temp")
    return round(int(raw) / 1000, 1) if raw and raw.strip().isdigit() else None


def cpu_times() -> dict[str, tuple[int, int]]:
    """{"cpu": (busy, total), "cpu0": ...} in jiffies, from /proc/stat."""
    out = {}
    for line in (read("/proc/stat") or "").splitlines():
        if not line.startswith("cpu"):
            continue
        name, *fields = line.split()
        values = [int(v) for v in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        total = sum(values[:8])  # guest time is already in user
        out[name] = (total - idle, total)
    return out


def cpu_percent(before: dict, after: dict) -> dict[str, float]:
    out = {}
    for name, (busy, total) in after.items():
        if name in before:
            d_total = total - before[name][1]
            if d_total > 0:
                out[name] = round(100 * (busy - before[name][0]) / d_total, 1)
    return out


PROCESS_GROUPS = {
    "chromium": ("chromium", "chrome"),
    "kodi": ("kodi.bin", "kodi", "kodi-standalone"),
    "cage": ("cage",),
    "couchd": ("couchd",),
    "swayimg": ("swayimg",),
    "pipewire": ("pipewire", "pipewire-pulse", "wireplumber"),
}


def parse_proc_stat(text: str) -> tuple[str, int, int, int]:
    """(comm, ppid, utime + stime in jiffies, start time in jiffies since boot)."""
    comm = text[text.index("(") + 1 : text.rindex(")")]
    fields = text[text.rindex(")") + 2 :].split()
    # fields[0] is the state (field 3 in proc(5)); utime and stime are fields 14 and 15.
    return comm, int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[19])


def processes() -> list[dict[str, Any]]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        text = read(entry / "stat")
        if not text:
            continue
        try:
            comm, ppid, cpu, start = parse_proc_stat(text)
        except (ValueError, IndexError):
            continue
        out.append({"pid": int(entry.name), "comm": comm, "ppid": ppid, "cpu": cpu, "start": start})
    return out


def group_of(comm: str) -> str | None:
    for group, names in PROCESS_GROUPS.items():
        if comm in names or any(comm.startswith(n) for n in names):
            return group
    return None


def group_cpu(procs: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for p in procs:
        if (g := group_of(p["comm"])) is not None:
            totals[g] = totals.get(g, 0) + p["cpu"]
    return totals


def meminfo() -> dict[str, int]:
    out = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "CmaTotal", "CmaFree"}:
            out[key] = int(rest.split()[0])  # kB
    return out


def uptime() -> float:
    return float((read("/proc/uptime") or "0").split()[0])


class Sampler:
    """CPU (whole Pi, per core, per program), temperature, ARM clock, throttling and memory,
    every `interval` seconds while running."""

    def __init__(self, interval: float = 5.0) -> None:
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def start(self) -> "Sampler":
        self._task = asyncio.create_task(self._run())
        return self

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        cpu0, procs0, t0 = cpu_times(), group_cpu(processes()), time.monotonic()
        while True:
            await asyncio.sleep(self.interval)
            cpu1, procs1, t1 = cpu_times(), group_cpu(processes()), time.monotonic()
            per_core = cpu_percent(cpu0, cpu1)
            seconds = t1 - t0
            programs = {
                g: round(100 * (procs1[g] - procs0.get(g, 0)) / self._hz / seconds, 1)
                for g in procs1
                if procs1[g] >= procs0.get(g, 0)
            }
            freq = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
            mem = meminfo()
            value = throttled()
            self.samples.append(
                {
                    "t": round(t1 - T0, 1),
                    "uptime": round(uptime(), 1),
                    "cpu": per_core.pop("cpu", None),
                    "cores": per_core,
                    "programs": programs,  # % of one core
                    "temp_c": temperature(),
                    "arm_mhz": int(freq) // 1000 if freq and freq.strip().isdigit() else None,
                    "throttled": hex(value) if value is not None else None,
                    "mem_available_mb": mem.get("MemAvailable", 0) // 1024 or None,
                    "cma_free_mb": mem.get("CmaFree", 0) // 1024 if "CmaFree" in mem else None,
                }
            )
            cpu0, procs0, t0 = cpu1, procs1, t1

    def summary(self) -> dict[str, Any]:
        return summarize_samples(self.samples)


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {"samples": 0}
    flags = 0
    for s in samples:
        if s.get("throttled"):
            flags |= int(s["throttled"], 16)
    programs = sorted({g for s in samples for g in s.get("programs", {})})
    return {
        "samples": len(samples),
        "seconds": round(samples[-1]["t"] - samples[0]["t"], 1) if len(samples) > 1 else 0,
        "cpu_percent": summarize([s.get("cpu") for s in samples], 1),
        "busiest_core_percent": summarize(
            [max(s["cores"].values(), default=None) for s in samples], 1
        ),
        "programs_percent_of_a_core": {
            g: summarize([s["programs"].get(g) for s in samples], 1) for g in programs
        },
        "temp_c": summarize([s.get("temp_c") for s in samples], 1),
        "arm_mhz": summarize([s.get("arm_mhz") for s in samples], 0),
        "throttled": hex(flags),
        "throttled_flags": decode_throttled(flags),
        "mem_available_mb_min": min(
            (s["mem_available_mb"] for s in samples if s.get("mem_available_mb")), default=None
        ),
    }


def environment() -> dict[str, Any]:
    return {
        "host": socket.gethostname(),
        "model": (read("/proc/device-tree/model") or "").strip("\x00\n") or platform.machine(),
        "kernel": platform.release(),
        "uptime": round(uptime(), 1),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mem_total_mb": meminfo().get("MemTotal", 0) // 1024,
        "throttled": hex(t) if (t := throttled()) is not None else None,
        "temp_c": temperature(),
        "governor": (read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "").strip()
        or None,
        "arm_max_mhz": int(f) // 1000
        if (f := read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"))
        else None,
        "v3d_clock": (run(["vcgencmd", "measure_clock", "v3d"]) or "").strip() or None,
        "gpu_mem": (run(["vcgencmd", "get_mem", "gpu"]) or "").strip() or None,
    }


# --- couchd and Chromium ------------------------------------------------------------------


class Couch:
    """couchd's HTTP API (docs/protocol.md)."""

    def __init__(self, http: "aiohttp.ClientSession", base: str = API) -> None:
        self.http = http
        self.base = base

    async def state(self) -> dict[str, Any]:
        async with self.http.get(f"{self.base}/api/state") as r:
            return await r.json()

    async def command(self, name: str, args: dict | None = None, timeout: float = 60) -> Any:
        async with self.http.post(
            f"{self.base}/api/command/{name}",
            json=args or {},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            body = await r.json()
        if not body.get("ok", False):
            raise RuntimeError(f"{name}: {body.get('error')}")
        return body.get("data")

    async def wait_for(self, check, timeout: float = 60, every: float = 0.25) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
                state = await self.state()
                if check(state):
                    return state
            await asyncio.sleep(every)
        return None


class Target:
    """One DevTools connection, to a page or to the browser."""

    def __init__(self, ws: "aiohttp.ClientWebSocketResponse") -> None:
        self.ws = ws
        self._ids = iter(range(1, 1 << 31))
        self._pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self.listeners: dict[str, list] = {}
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, http: "aiohttp.ClientSession", url: str) -> "Target":
        return cls(
            await http.ws_connect(url, max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=5))
        )

    async def _read(self) -> None:
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if "id" in data and (fut := self._pending.pop(data["id"], None)) and not fut.done():
                fut.set_result(data)
            elif "method" in data:
                for listener in self.listeners.get(data["method"], []):
                    listener(data.get("params", {}))
                await self.events.put(data)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("DevTools connection closed"))

    def on(self, method: str, listener) -> None:
        self.listeners.setdefault(method, []).append(listener)

    async def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self.ws.send_json({"id": msg_id, "method": method, "params": params or {}})
        reply = await asyncio.wait_for(fut, timeout)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    async def evaluate(self, expression: str, timeout: float = 30) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"page error: {result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    async def wait_event(self, method: str, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            event = await asyncio.wait_for(self.events.get(), left)
            if event["method"] == method:
                return event.get("params", {})

    async def close(self) -> None:
        await self.ws.close()
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader


class Browser:
    def __init__(self, http: "aiohttp.ClientSession", base: str = CDP) -> None:
        self.http = http
        self.base = base

    async def version(self) -> dict[str, Any]:
        async with self.http.get(f"{self.base}/json/version") as r:
            return await r.json()

    async def pages(self) -> list[dict[str, Any]]:
        async with self.http.get(f"{self.base}/json/list") as r:
            return [t for t in await r.json() if t.get("type") == "page"]

    async def find(self, match, timeout: float = 60) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError, OSError):
                for page in await self.pages():
                    if match(page["url"]):
                        return page
            await asyncio.sleep(0.2)
        return None

    async def attach(self, page: dict[str, Any]) -> Target:
        return await Target.connect(self.http, page["webSocketDebuggerUrl"])

    async def browser_target(self) -> Target:
        return await Target.connect(self.http, (await self.version())["webSocketDebuggerUrl"])


def is_launcher(url: str) -> bool:
    return url.startswith(API + "/") or url == API


def is_youtube(url: str) -> bool:
    return "youtube.com/tv" in url


# The page's clock in seconds since the kernel started: counted back from performance.now(),
# not timeOrigin, since the Pi has no clock battery and the wall clock jumps at NTP.
PAINTS_JS = """JSON.stringify({
  now: performance.now(),
  origin: performance.timeOrigin,
  wall: Date.now(),
  paints: Object.fromEntries(performance.getEntriesByType('paint').map(e => [e.name, e.startTime])),
  nav: (performance.getEntriesByType('navigation')[0] || {}).toJSON?.() || null,
})"""

# The page's largest-contentful-paint so far (ms on its clock), from an observer that also
# replays earlier entries. Kept on the page so later calls see later entries.
LCP_JS = """new Promise(resolve => {
  if (!window.__couchperfLcp) {
    window.__couchperfLcp = {last: null, count: 0};
    try {
      new PerformanceObserver(list => {
        for (const e of list.getEntries()) { window.__couchperfLcp.last = e.startTime; window.__couchperfLcp.count++; }
      }).observe({type: 'largest-contentful-paint', buffered: true});
    } catch (e) {}
  }
  setTimeout(() => resolve(window.__couchperfLcp), 100);
})"""


async def page_paints(page: Target) -> dict[str, Any]:
    data = json.loads(await page.evaluate(PAINTS_JS))
    up = uptime()
    start = up - data["now"] / 1000
    return {
        "page_started_uptime": round(start, 3),
        "page_started_wall": data["origin"] / 1000,
        "paints_ms": data["paints"],
        "paints_uptime": {k: round(start + v / 1000, 3) for k, v in data["paints"].items()},
        "nav": {
            k: round(v, 1)
            for k, v in (data.get("nav") or {}).items()
            if isinstance(v, (int, float))
        },
    }


# --- Kodi --------------------------------------------------------------------------------


class Kodi:
    """Kodi's JSON-RPC over TCP: JSON objects back to back, notifications mixed in."""

    def __init__(self, addr: tuple[str, int] = KODI) -> None:
        self.addr = addr
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._buf = ""
        self._id = 0

    async def connect(self, timeout: float = 2.0) -> None:
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(*self.addr), timeout
        )

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
        self.reader = self.writer = None

    async def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> Any:
        if self.writer is None:
            await self.connect()
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.writer.write(json.dumps(msg).encode())
        await self.writer.drain()
        deadline = time.monotonic() + timeout
        while True:
            for obj in self._drain():
                if obj.get("id") == self._id:
                    if "error" in obj:
                        raise RuntimeError(f"{method}: {obj['error']}")
                    return obj.get("result")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            chunk = await asyncio.wait_for(self.reader.read(65536), left)
            if not chunk:
                await self.close()
                raise ConnectionError("Kodi closed the connection")
            self._buf += chunk.decode(errors="replace")

    def _drain(self):
        decoder = json.JSONDecoder()
        while True:
            text = self._buf.lstrip()
            if not text:
                self._buf = ""
                return
            try:
                obj, end = decoder.raw_decode(text)
            except ValueError:
                self._buf = text
                return
            self._buf = text[end:]
            if isinstance(obj, dict):
                yield obj

    async def labels(self, names: list[str]) -> dict[str, str]:
        return await self.call("XBMC.GetInfoLabels", {"labels": names})

    async def booleans(self, names: list[str]) -> dict[str, bool]:
        return await self.call("XBMC.GetInfoBooleans", {"booleans": names})


# Never Player.Filenameandpath or ListItem.FileNameAndPath: Plex URLs carry the token.
PLAYER_LABELS = [
    "VideoPlayer.Title",
    "VideoPlayer.VideoCodec",
    "VideoPlayer.VideoResolution",
    "VideoPlayer.VideoAspect",
    "VideoPlayer.HdrType",
    "VideoPlayer.AudioCodec",
    "VideoPlayer.AudioChannels",
    "Player.Process(videodecoder)",
    "Player.Process(videohwdecoder)",
    "Player.Process(pixformat)",
    "Player.Process(videowidth)",
    "Player.Process(videoheight)",
    "Player.Process(videofps)",
    "Player.Process(videodar)",
    "Player.Process(deintmethod)",
    "Player.Process(audiodecoder)",
    "Player.Process(audiochannels)",
    "Player.Process(audiosamplerate)",
    "Player.Process(audiobitspersample)",
    "Player.Time",
    "Player.Duration",
    "Player.CacheLevel",
    "System.ScreenResolution",
    "System.FPS",
    "System.CpuUsage",
]

FOCUS_LABELS = [
    "System.CurrentWindow",
    "System.CurrentControl",
    "Container.FolderName",
    "Container.NumItems",
    "ListItem.Label",
    "ListItem.Title",
    "ListItem.Year",
    "ListItem.VideoCodec",
    "ListItem.VideoResolution",
    "ListItem.Duration",
    "ListItem.Property(video.codec)",
    "ListItem.Property(video.res)",
]


def refresh_of(screen_resolution: str) -> float | None:
    """ "1920x1080 @ 23.98Hz - Full Screen" → 23.98."""
    m = re.search(r"@\s*([0-9.]+)\s*Hz", screen_resolution or "")
    return float(m.group(1)) if m else None


# --- the TV's modes, from its EDID -------------------------------------------------------

# CEA-861 VICs a 1080p TV may list, and what they are.
VICS = {
    4: "1280x720p60",
    5: "1920x1080i60",
    16: "1920x1080p60",
    19: "1280x720p50",
    20: "1920x1080i50",
    31: "1920x1080p50",
    32: "1920x1080p24",
    33: "1920x1080p25",
    34: "1920x1080p30",
    93: "3840x2160p24",
    94: "3840x2160p25",
    95: "3840x2160p30",
    96: "3840x2160p50",
    97: "3840x2160p60",
}


def edid_vics(edid: bytes) -> list[int]:
    """The video modes (VICs) in an EDID's CEA-861 extension blocks."""
    vics = []
    for block in range(1, len(edid) // 128):
        ext = edid[block * 128 : (block + 1) * 128]
        if len(ext) < 4 or ext[0] != 0x02:
            continue
        end = ext[2]
        i = 4
        while i < end and i < 127:
            tag, length = ext[i] >> 5, ext[i] & 0x1F
            if tag == 2:  # video data block
                vics += [b & 0x7F if b & 0x7F else b for b in ext[i + 1 : i + 1 + length]]
            i += 1 + length
    return vics


def edid_name(edid: bytes) -> str | None:
    for off in (54, 72, 90, 108):
        d = edid[off : off + 18]
        if len(d) == 18 and d[:3] == b"\0\0\0" and d[3] == 0xFC:
            return d[5:].split(b"\n")[0].decode(errors="replace").strip()
    return None


def display_modes() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for conn in sorted(Path("/sys/class/drm").glob("card*-HDMI-A-*")):
        status = (read(conn / "status") or "").strip()
        if status != "connected":
            continue
        try:
            edid = (conn / "edid").read_bytes()
        except OSError:
            edid = b""
        vics = edid_vics(edid)
        out[conn.name] = {
            "monitor": edid_name(edid),
            "modes": (read(conn / "modes") or "").split(),
            "vics": {v: VICS.get(v, f"vic {v}") for v in vics},
            "has_1080p24": 32 in vics,
        }
    return out


# --- trace analysis ---------------------------------------------------------------------

DRAW_EVENTS = ("Display::DrawAndSwap",)
PRESENTED = ("STATE_PRESENTED_ALL", "STATE_PRESENTED_PARTIAL")


def pipeline_frames(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per frame from cc's PipelineReporter spans: begin and end (presentation)
    times in ms, the state (presented, dropped, no update wanted), and the reporter's pid."""
    open_spans: dict[tuple, dict] = {}
    frames = []
    for e in sorted(events, key=lambda e: e.get("ts", 0)):
        if e.get("name") != "PipelineReporter":
            continue
        key = (e.get("pid"), json.dumps(e.get("id2") or e.get("id")))
        if e.get("ph") == "b":
            args = e.get("args", {})
            info = args.get("frame_reporter") or args.get("chrome_frame_reporter") or {}
            open_spans[key] = {
                "begin": e["ts"] / 1000,
                "state": info.get("state"),
                "smoothness": info.get("affects_smoothness"),
                "pid": e.get("pid"),
            }
        elif e.get("ph") == "e" and key in open_spans:
            frame = open_spans.pop(key)
            frame["end"] = e["ts"] / 1000
            frames.append(frame)
    return frames


def analyze_trace(events: list[dict[str, Any]], window_ms: float = 400) -> dict[str, Any]:
    """Frame times around each key press, from a Chromium trace.

    Key presses are the page's `console.timeStamp("couchperf:key")` marks. Frames are cc's
    PipelineReporter spans, one per vsync the compositor was asked for: presented, dropped
    or not wanted. The time between presented frames is what the TV shows; at 60 Hz it
    should stay at 16.7 ms while focus moves. viz's DrawAndSwap (GPU compositing only) is
    the same thing seen from the display side.
    """
    marks = sorted(
        e["ts"] / 1000
        for e in events
        if e.get("name") == "TimeStamp" and "couchperf:key" in json.dumps(e.get("args", {}))
    )

    def in_windows(t: float) -> bool:
        return any(m <= t <= m + window_ms for m in marks)

    def per_window(times: list[float]) -> list[float]:
        out = []
        for m in marks:
            out += frame_intervals([t for t in times if m <= t <= m + window_ms])
        return out

    frames = pipeline_frames(events)
    states: dict[str, int] = {}
    for f in frames:
        if in_windows(f["begin"]) and f["state"]:
            states[f["state"]] = states.get(f["state"], 0) + 1
    # The page's own compositor: the pid that presented the most frames during the presses.
    by_pid: dict[Any, int] = {}
    for f in frames:
        if f["state"] in PRESENTED and in_windows(f["begin"]):
            by_pid[f["pid"]] = by_pid.get(f["pid"], 0) + 1
    page_pid = max(by_pid, key=by_pid.get) if by_pid else None
    presented = [f["end"] for f in frames if f["pid"] == page_pid and f["state"] in PRESENTED]
    latency = [
        f["end"] - f["begin"]
        for f in frames
        if f["pid"] == page_pid and f["state"] in PRESENTED and in_windows(f["begin"])
    ]
    draws = sorted(
        e["ts"] / 1000 for e in events if e.get("name") in DRAW_EVENTS and e.get("ph") == "X"
    )
    return {
        "key_presses": len(marks),
        "window_ms": window_ms,
        "frame_states": states,
        "dropped_frames": states.get("STATE_DROPPED", 0),
        "presented_interval_ms": jank(per_window(presented)),
        "frame_latency_ms": summarize(latency),
        "draw_and_swap_interval_ms": jank(per_window(draws)) if draws else None,
    }


def analyze_raf(frames: list[float], keys: list[float], window_ms: float = 400) -> dict[str, Any]:
    intervals = []
    for k in keys:
        intervals += frame_intervals([f for f in frames if k <= f <= k + window_ms])
    return {"key_presses": len(keys), "interval_ms": jank(intervals)}


# --- checks -------------------------------------------------------------------------------


def save(name: str, result: dict[str, Any], out: Path | None) -> None:
    path = out or OUT_DIR / f"{name}.json"
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1))
        tmp.replace(path)


async def check_sample(args) -> dict[str, Any]:
    sampler = Sampler(args.interval).start()
    log(f"sampling for {args.seconds:g} s")
    await asyncio.sleep(args.seconds)
    await sampler.stop()
    return {"summary": sampler.summary(), "samples": sampler.samples}


YT_STATS_JS = """(() => {
  const v = document.querySelector('video');
  if (!v) return null;
  const q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : {};
  const p = document.querySelector('.html5-video-player') || document.getElementById('movie_player');
  const call = (name, ...a) => { try { return p && typeof p[name] === 'function' ? p[name](...a) : null } catch (e) { return null } };
  const nerds = call('getStatsForNerds', 0);
  const keep = ['codecs', 'resolution', 'dims_and_frames', 'buffer_health_seconds',
                'bandwidth_kbps', 'network_activity_bytes', 'color', 'video_id_and_cpn'];
  return {
    wall: Date.now(), time: v.currentTime, duration: v.duration, paused: v.paused,
    ended: v.ended, rate: v.playbackRate, ready: v.readyState, error: v.error && v.error.code,
    hash: location.hash.slice(0, 40),
    width: v.videoWidth, height: v.videoHeight,
    total: q.totalVideoFrames, dropped: q.droppedVideoFrames, corrupted: q.corruptedVideoFrames,
    quality: call('getPlaybackQuality'),
    vid: (call('getVideoData') || {}).video_id || null,
    ad: !!(p && p.classList && p.classList.contains('ad-showing')),
    nerds: nerds ? Object.fromEntries(keep.filter(k => k in nerds).map(k => [k, nerds[k]])) : null,
  };
})()"""

YT_QUALITY_JS = """(() => {
  const p = document.querySelector('.html5-video-player') || document.getElementById('movie_player');
  const out = {};
  for (const [name, args] of [['setPlaybackQualityRange', ['hd1080', 'hd1080']], ['setPlaybackQuality', ['hd1080']]]) {
    try { if (p && typeof p[name] === 'function') { p[name](...args); out[name] = true } } catch (e) { out[name] = String(e) }
  }
  try { out.available = p.getAvailableQualityLevels() } catch (e) {}
  return out;
})()"""

YT_REWIND_JS = """(() => {
  const v = document.querySelector('video');
  const p = document.querySelector('.html5-video-player') || document.getElementById('movie_player');
  try { if (p && p.seekTo) { p.seekTo(0, true); p.playVideo && p.playVideo(); return 'player' } } catch (e) {}
  if (v) { v.currentTime = 0; void v.play(); return 'video' }
  return null;
})()"""


def youtube_frames(stats: list[dict[str, Any]], video: str | None = None) -> dict[str, Any]:
    """Dropped and decoded frames, from successive getVideoPlaybackQuality() readings. The
    counters restart with each new source (another video, an ad), so a fall is a restart.
    With `video`, only readings while that video played count (not ads)."""
    total = dropped = 0
    rates = []
    worst = None
    prev = None
    for s in stats:
        if s is None or s.get("total") is None:
            prev = None
            continue
        if prev is not None and s["total"] >= prev["total"] and s["dropped"] >= prev["dropped"]:
            d_total, d_dropped = s["total"] - prev["total"], s["dropped"] - prev["dropped"]
            seconds = (s["wall"] - prev["wall"]) / 1000
            ours = video is None or s.get("vid") in (None, video)
            playing = not s.get("paused") and not s.get("ad") and ours
            if playing and seconds > 0 and d_total > 0:
                total += d_total
                dropped += d_dropped
                rates.append(d_total / seconds)
                pct = 100 * d_dropped / d_total
                if worst is None or pct > worst["dropped_percent"]:
                    worst = {
                        "at_s": s.get("t"),
                        "dropped_percent": round(pct, 2),
                        "dropped": d_dropped,
                        "frames": d_total,
                    }
        prev = s
    return {
        "frames": total,
        "dropped": dropped,
        "dropped_percent": round(100 * dropped / total, 3) if total else None,
        "fps": summarize(rates, 1),
        "worst_interval": worst,
    }


async def check_youtube(args) -> dict[str, Any]:
    minutes = 3 if args.short else args.minutes
    result: dict[str, Any] = {
        "check": "youtube",
        "video": args.video,
        "minutes": minutes,
        "env": environment(),
    }
    async with aiohttp.ClientSession() as http:
        couch, browser = Couch(http), Browser(http)
        result["chromium"] = (await browser.version()).get("Browser")
        log("launching YouTube")
        await couch.command("apps.launch", {"app": "youtube"})
        tab = await browser.find(is_youtube, timeout=30)
        if tab is None:
            raise SystemExit("no youtube.com/tv tab")
        page = await browser.attach(tab)
        decoder: dict[str, Any] = {}

        def media(params: dict) -> None:
            for prop in params.get("properties", []):
                if prop.get("name", "").startswith(
                    ("kVideo", "kIsPlatform", "kAudio", "kFrame", "kIsVideo")
                ):
                    decoder[prop["name"]] = prop.get("value")

        page.on("Media.playerPropertiesChanged", media)
        with contextlib.suppress(RuntimeError):
            await page.call("Media.enable")
        log(f"opening video {args.video}")
        await page.evaluate(f"location.hash = '#/watch?v={args.video}'")
        if not await wait_playing(page, 45):
            log("not playing yet; loading the watch URL")
            await page.call(
                "Page.navigate", {"url": f"https://www.youtube.com/tv#/watch?v={args.video}"}
            )
            if not await wait_playing(page, 60):
                result["error"] = "the video didn't start"
        result["quality_request"] = await page.evaluate(YT_QUALITY_JS)
        sampler = Sampler(args.interval).start()
        stats: list[dict[str, Any]] = []
        deadline = time.monotonic() + minutes * 60
        last_save = time.monotonic()
        rewinds = 0
        while time.monotonic() < deadline:
            try:
                s = await page.evaluate(YT_STATS_JS)
            except Exception as e:
                s = {"error": str(e)}
            if isinstance(s, dict):
                s["t"] = round(time.monotonic() - T0, 1)
                stats.append(s)
                near_end = s.get("duration") and s.get("time", 0) > s["duration"] - 3
                if (s.get("ended") or near_end) and not s.get("ad"):
                    rewinds += 1
                    with contextlib.suppress(RuntimeError, TimeoutError):
                        await page.evaluate(YT_REWIND_JS)
                if s.get("paused") and not s.get("ended") and not s.get("ad"):
                    with contextlib.suppress(RuntimeError, TimeoutError):
                        # Not awaited: play() can stay pending for as long as it's blocked.
                        await page.evaluate("void document.querySelector('video')?.play()")
            if time.monotonic() - last_save > 60:
                left = round((deadline - time.monotonic()) / 60, 1)
                frames = youtube_frames(stats, args.video)
                log(
                    f"{left} min left; dropped {frames['dropped']} of {frames['frames']}; temp {temperature()} °C"
                )
                save(
                    "youtube",
                    {**result, "partial": True, "stats": stats, "system": sampler.samples},
                    args.out,
                )
                last_save = time.monotonic()
            await asyncio.sleep(args.interval)
        await sampler.stop()
        played = {
            s["nerds"]["resolution"].split(" / ")[0]
            for s in stats
            if (s.get("nerds") or {}).get("resolution")
        }
        result["got_1080p60"] = "1920x1080@60" in played
        result.update(
            {
                "frames": youtube_frames(stats, args.video),
                "decoder": decoder,
                "resolutions": sorted(
                    {f"{s.get('width')}x{s.get('height')}" for s in stats if s.get("width")}
                ),
                "qualities": sorted({s["quality"] for s in stats if s.get("quality")}),
                # What the player said it played: "1920x1080@60 / 1920x1080@60" is the goal.
                "nerds_resolutions": sorted(
                    {
                        s["nerds"]["resolution"]
                        for s in stats
                        if s.get("nerds") and s["nerds"].get("resolution")
                    }
                ),
                "codecs": sorted(
                    {
                        s["nerds"]["codecs"]
                        for s in stats
                        if s.get("nerds") and s["nerds"].get("codecs")
                    }
                ),
                "readings_off_video": sum(
                    1 for s in stats if s.get("ad") or s.get("vid") not in (None, args.video)
                ),
                "rewinds": rewinds,
                "system": sampler.summary(),
                "stats": stats,
                "system_samples": sampler.samples,
            }
        )
        if not args.leave:
            with contextlib.suppress(Exception):
                await page.evaluate("document.querySelector('video')?.pause()")
            with contextlib.suppress(Exception):
                await couch.command("apps.home")
        await page.close()
    return result


async def wait_playing(page: Target, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    first = None
    while time.monotonic() < deadline:
        with contextlib.suppress(RuntimeError, asyncio.TimeoutError):
            t = await page.evaluate(
                "(() => { const v = document.querySelector('video'); return v && !v.paused ? v.currentTime : null })()"
            )
            if t is not None:
                if first is None:
                    first = t
                elif t > first + 1:
                    return True
        await asyncio.sleep(0.5)
    return False


RAF_JS = """(() => {
  // Once per page: a second run reuses the listener instead of adding another.
  const fresh = !window.__couchperf;
  const p = window.__couchperf = window.__couchperf || {};
  Object.assign(p, {frames: [], keys: [], on: true});
  const tick = (ts) => { p.frames.push(ts); if (p.on) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  if (fresh) {
    const mark = () => { if (!p.on) return; p.keys.push(performance.now()); console.timeStamp('couchperf:key'); };
    if (window.couchApp) window.couchApp.addEventListener('key', mark);
    else window.addEventListener('keydown', mark, true);
  }
  return !!window.couchApp;
})()"""

TRACE_CATEGORIES = [
    "-*",
    "devtools.timeline",
    "disabled-by-default-devtools.timeline",
    "disabled-by-default-devtools.timeline.frame",
    "viz",
    "cc",
    "benchmark",
    "gpu",
]

KEY_SEQUENCE = ["right"] * 4 + ["left"] * 4 + ["down", "up"] + ["right"] * 4 + ["left"] * 4


async def check_launcher(args) -> dict[str, Any]:
    result: dict[str, Any] = {"check": "launcher", "env": environment()}
    async with aiohttp.ClientSession() as http:
        couch, browser = Couch(http), Browser(http)
        result["chromium"] = (await browser.version()).get("Browser")
        await couch.command("apps.home")
        await couch.wait_for(
            lambda s: s.get("startup", {}).get("done") and not s.get("apps", {}).get("opening"), 60
        )
        await asyncio.sleep(2)
        tab = await browser.find(is_launcher, 10)
        if tab is None:
            raise SystemExit("no launcher tab")
        page = await browser.attach(tab)
        result["hooked_app_keys"] = await page.evaluate(RAF_JS)
        await page.call("Performance.enable")
        before = {
            m["name"]: m["value"] for m in (await page.call("Performance.getMetrics"))["metrics"]
        }
        await page.call(
            "Tracing.start",
            {
                "traceConfig": {
                    "includedCategories": TRACE_CATEGORIES,
                    "recordMode": "recordAsMuchAsPossible",
                },
                "transferMode": "ReturnAsStream",
            },
        )
        sampler = Sampler(1.0).start()
        sequence = KEY_SEQUENCE * args.rounds
        log(f"pressing {len(sequence)} keys, {args.gap:g} s apart")
        for key in sequence:
            await couch.command("remote.press", {"key": key})
            await asyncio.sleep(args.gap)
        await sampler.stop()
        await page.call("Tracing.end")
        done = await page.wait_event("Tracing.tracingComplete", 120)
        events = await read_stream(page, done["stream"])
        after = {
            m["name"]: m["value"] for m in (await page.call("Performance.getMetrics"))["metrics"]
        }
        raf = await page.evaluate(
            "(() => { const p = window.__couchperf; p.on = false; return {frames: p.frames, keys: p.keys} })()"
        )
        await page.close()
        # Back to where it started: leftwards as far as the keys went.
        result.update(
            {
                "trace": analyze_trace(events, args.window),
                "raf": analyze_raf(raf["frames"], raf["keys"], args.window),
                "main_thread_ms": {
                    k: round((after.get(k, 0) - before.get(k, 0)) * 1000, 1)
                    for k in (
                        "TaskDuration",
                        "ScriptDuration",
                        "LayoutDuration",
                        "RecalcStyleDuration",
                    )
                },
                "system": sampler.summary(),
                "trace_events": len(events),
            }
        )
        if args.keep_trace:
            save("launcher-trace", {"traceEvents": events}, None)
            result["trace_file"] = str(OUT_DIR / "launcher-trace.json")
    return result


async def read_stream(target: Target, handle: str) -> list[dict[str, Any]]:
    chunks = []
    while True:
        part = await target.call("IO.read", {"handle": handle, "size": 1 << 20})
        data = part.get("data", "")
        if part.get("base64Encoded"):
            import base64

            data = base64.b64decode(data).decode()
        chunks.append(data)
        if part.get("eof"):
            break
    await target.call("IO.close", {"handle": handle})
    text = "".join(chunks)
    parsed = json.loads(text)
    return parsed.get("traceEvents", parsed) if isinstance(parsed, dict) else parsed


async def check_launch(args) -> dict[str, Any]:
    result: dict[str, Any] = {"check": "launch", "env": environment(), "runs": args.runs}
    async with aiohttp.ClientSession() as http:
        couch, browser = Couch(http), Browser(http)
        await couch.command("apps.home")
        await asyncio.sleep(2)
        if "youtube" in args.apps:
            cold, warm = [], []
            for n in range(args.runs):
                log(f"YouTube cold, run {n + 1}")
                with contextlib.suppress(RuntimeError):
                    await couch.command("apps.close", {"app": "youtube"})
                await couch.command("apps.home")
                await asyncio.sleep(3)
                cold.append(await launch_youtube(couch, browser))
                log(f"  {cold[-1]}")
                await couch.command("apps.home")
                await asyncio.sleep(3)
                log(f"YouTube warm, run {n + 1}")
                warm.append(await launch_youtube(couch, browser))
                log(f"  {warm[-1]}")
                await couch.command("apps.home")
                await asyncio.sleep(3)
            result["youtube_cold"] = {"runs": cold, **summary_of(cold)}
            result["youtube_warm"] = {"runs": warm, **summary_of(warm)}
        if "plex" in args.apps:
            plex, back = [], []
            for n in range(args.runs):
                log(f"Plex (Kodi), run {n + 1}")
                plex.append(await launch_plex(couch))
                log(f"  {plex[-1]}")
                await asyncio.sleep(args.plex_settle)
                log(f"Kodi → launcher, run {n + 1}")
                back.append(await back_to_launcher(couch, browser))
                log(f"  {back[-1]}")
                await asyncio.sleep(5)
            result["plex"] = {"runs": plex, **summary_of(plex)}
            result["kodi_to_launcher"] = {"runs": back, **summary_of(back)}
    return result


def summary_of(runs: list[dict[str, Any]]) -> dict[str, Any]:
    keys = sorted(
        {k for r in runs for k, v in r.items() if isinstance(v, (int, float)) and k.endswith("_s")}
    )
    return {k: summarize([r.get(k) for r in runs], 2) for k in keys}


async def launch_youtube(couch: Couch, browser: Browser) -> dict[str, Any]:
    t0_wall, t0 = time.time(), time.monotonic()
    await couch.command("apps.launch", {"app": "youtube"})
    out: dict[str, Any] = {"command_s": round(time.monotonic() - t0, 3)}
    tab = await browser.find(is_youtube, 10)
    if tab is None:
        out["error"] = "no tab"
        return out
    page = await browser.attach(tab)
    try:
        # The first frame drawn now that it's in front.
        shown = await page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(Date.now()))))",
            10,
        )
        out["frame_in_front_s"] = round(shown / 1000 - t0_wall, 3)
        data = json.loads(await page.evaluate(PAINTS_JS))
        origin = data["origin"] / 1000
        out["page_started_s"] = round(origin - t0_wall, 3)
        for name, ms in data["paints"].items():
            out[f"{name.replace('-', '_')}_s"] = round(origin + ms / 1000 - t0_wall, 3)
        # YouTube keeps drawing for a while (tiles, thumbnails): its largest paint is final
        # once it hasn't changed for LCP_QUIET seconds.
        last, quiet_since, deadline = None, time.monotonic(), time.monotonic() + LCP_MAX
        while time.monotonic() < deadline and time.monotonic() - quiet_since < LCP_QUIET:
            lcp = await page.evaluate(LCP_JS)
            if lcp and lcp.get("last") != last:
                last, quiet_since = lcp["last"], time.monotonic()
            await asyncio.sleep(0.25)
        if last is not None:
            out["lcp_s"] = round(origin + last / 1000 - t0_wall, 3)
        if out["page_started_s"] < -1:
            # A warm switch: the page is older than the press, so its paints aren't this one's.
            for k in [k for k in out if k.endswith("paint_s") or k in ("lcp_s", "page_started_s")]:
                out.pop(k)
    finally:
        await page.close()
    return out


async def launch_plex(couch: Couch) -> dict[str, Any]:
    t0 = time.monotonic()
    await couch.command("apps.launch", {"app": "plex"}, timeout=90)
    out: dict[str, Any] = {"command_s": round(time.monotonic() - t0, 3)}
    kodi = Kodi()
    windows = []
    deadline = t0 + 60
    while time.monotonic() < deadline:
        try:
            if kodi.writer is None:
                await kodi.connect(1.0)
                out.setdefault("jsonrpc_s", round(time.monotonic() - t0, 3))
            win = (await kodi.call("GUI.GetProperties", {"properties": ["currentwindow"]}, 2))[
                "currentwindow"
            ]
            label = (await kodi.labels(["System.CurrentWindow"])).get("System.CurrentWindow")
            entry = (win.get("id"), win.get("label"), label)
            if not windows or windows[-1][1:] != entry:
                windows.append((round(time.monotonic() - t0, 3), *entry))
            # PM4K's own windows are script windows (ids 13000 and up).
            if win.get("id", 0) >= 13000 and "plex_window_s" not in out:
                out["plex_window_s"] = round(time.monotonic() - t0, 3)
            if "plex_window_s" in out and time.monotonic() - t0 > out["plex_window_s"] + 5:
                break
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            await kodi.close()
        await asyncio.sleep(0.25)
    await kodi.close()
    out["windows"] = windows
    return out


async def back_to_launcher(couch: Couch, browser: Browser) -> dict[str, Any]:
    t0 = time.monotonic()
    await couch.command("apps.home", timeout=90)
    out: dict[str, Any] = {"command_s": round(time.monotonic() - t0, 3)}
    tab = await browser.find(is_launcher, 60)
    if tab is None:
        out["error"] = "no launcher tab"
        return out
    out["devtools_s"] = round(time.monotonic() - t0, 3)
    page = await browser.attach(tab)
    try:
        for _ in range(100):
            paints = await page_paints(page)
            if "first-contentful-paint" in paints["paints_uptime"]:
                break
            await asyncio.sleep(0.2)
        now_up = uptime()
        started_up = now_up - (time.monotonic() - t0)
        for name, at in paints["paints_uptime"].items():
            out[f"{name.replace('-', '_')}_s"] = round(at - started_up, 3)
    finally:
        await page.close()
    state = await couch.wait_for(lambda s: s.get("apps", {}).get("foreground") == "launcher", 30)
    out["launcher_in_front"] = state is not None
    return out


# couchd log lines and systemd units whose first appearance is a boot milestone. Matched
# against "<identifier or unit>: <message>".
BOOT_MILESTONES = [
    (
        "kernel: firmware framebuffer",
        r"^kernel: .*(simple-framebuffer|simplefb).*fb0|^kernel: .*fb0: simplefb",
    ),
    ("kernel: vc4 takes the screen", r"^kernel: .*vc4drmfb"),
    ("kernel: root mounted", r"^kernel: EXT4-fs \(\w+\): mounted filesystem"),
    ("systemd: switch root / init", r"^systemd: systemd \d+"),
    ("fsck: root started", r"^systemd: Starting systemd-fsck-root"),
    ("fsck: root finished", r"^systemd: Finished systemd-fsck-root"),
    ("fsck: boot started", r"^systemd: Starting systemd-fsck@"),
    ("fsck: boot finished", r"^systemd: Finished systemd-fsck@"),
    ("NetworkManager started", r"^systemd: Started NetworkManager\.service"),
    ("network.target", r"^systemd: Reached target network\.target"),
    ("network-online.target", r"^systemd: Reached target network-online\.target"),
    ("couch-fbsplash drew", r"^systemd: Finished couch-fbsplash"),
    ("couchd starting", r"couchd: .*couchd starting"),
    ("couchd.service ready", r"^systemd: Started couchd\.service"),
    ("couch-session started (cage)", r"^systemd: Started couch-session\.service"),
    ("cage's login session", r"pam_unix\(login:session\): session opened for user couch"),
    ("couchd attached to Chromium", r"couchd: .*attached to Chromium"),
    ("launcher connected", r"couchd: .*ui connected"),
    ("launcher's first frame (couchd)", r"couchd: .*launcher drew its first frame"),
    ("session splash handed over", r"couchd: .*splash handed over"),
    ("TV on", r"couchd: .*: TV power=on"),
    ("startup done", r"couchd: .*startup done"),
    ("multi-user.target", r"^systemd: Reached target multi-user\.target"),
]


def journal_lines(entries: list[dict[str, Any]]) -> list[tuple[float, str]]:
    out = []
    for e in entries:
        try:
            t = int(e["__MONOTONIC_TIMESTAMP"]) / 1e6
        except (KeyError, ValueError):
            continue
        msg = e.get("MESSAGE", "")
        if isinstance(msg, list):  # binary messages come as byte arrays
            msg = bytes(msg).decode(errors="replace")
        who = e.get("SYSLOG_IDENTIFIER") or e.get("_SYSTEMD_UNIT") or e.get("_COMM") or "?"
        if e.get("_TRANSPORT") == "kernel":
            who = "kernel"
        out.append((t, f"{who}: {msg}"))
    return out


def boot_milestones(lines: list[tuple[float, str]]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for name, pattern in BOOT_MILESTONES:
        rx = re.compile(pattern)
        out[name] = next((round(t, 3) for t, text in lines if rx.search(text)), None)
    return out


def unit_durations(lines: list[tuple[float, str]], units: list[str]) -> dict[str, float | None]:
    """Seconds from systemd's "Starting X" to its "Started/Finished X", per unit pattern."""
    out = {}
    for unit in units:
        start = next((t for t, s in lines if re.match(rf"systemd: Starting {unit}", s)), None)
        end = next(
            (t for t, s in lines if re.match(rf"systemd: (Started|Finished) {unit}", s)), None
        )
        out[unit] = (
            round(end - start, 3)
            if start is not None and end is not None and end >= start
            else None
        )
    return out


def parse_blame(text: str) -> list[tuple[float, str]]:
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(?:(\d+)min )?([\d.]+)(ms|s) (\S+)", line)
        if m:
            secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) + 60 * int(
                m.group(1) or 0
            )
            out.append((round(secs, 3), m.group(4)))
    return out


def histogram_means(histograms: list[dict[str, Any]]) -> dict[str, float]:
    return {h["name"]: round(h["sum"] / h["count"], 1) for h in histograms if h.get("count")}


async def check_boot(args) -> dict[str, Any]:
    result: dict[str, Any] = {
        "check": "boot",
        "env": environment(),
        "cmdline": (read("/proc/cmdline") or "").strip(),
    }
    raw = run(["journalctl", "-b", "-o", "json", "--no-pager"], timeout=60, sudo=True) or ""
    entries = []
    for line in raw.splitlines():
        with contextlib.suppress(ValueError):
            entries.append(json.loads(line))
    lines = journal_lines(entries)
    result["journal_entries"] = len(lines)
    result["milestones"] = boot_milestones(lines)
    result["unit_seconds"] = unit_durations(
        lines,
        [
            r"systemd-fsck-root",
            r"systemd-fsck@",
            r"NetworkManager\.service",
            r"NetworkManager-wait-online",
            r"cloud-init",
            r"couch-fbsplash",
            r"couchd\.service",
        ],
    )
    # cage's and Chromium's own output at the session's start: cage's modeset and Chromium's
    # first errors show up here.
    session = [
        (round(t, 3), s)
        for t, s in lines
        if s.startswith(("cage", "couch-session", "chromium", "swayimg"))
    ]
    result["session_log"] = session[:60]
    # When the session's programs started, from /proc (jiffies since boot).
    hz = os.sysconf("SC_CLK_TCK")
    started: dict[str, float] = {}
    for p in processes():
        g = (
            p["comm"]
            if p["comm"] in ("cage", "swayimg", "couchd", "kodi.bin")
            else group_of(p["comm"])
        )
        if g:
            at = round(p["start"] / hz, 2)
            started[g] = min(started.get(g, at), at)
    result["process_started_uptime"] = started
    async with aiohttp.ClientSession() as http:
        browser = Browser(http)
        with contextlib.suppress(Exception):
            result["chromium"] = (await browser.version()).get("Browser")
        tab = await browser.find(is_launcher, 5)
        if tab:
            page = await browser.attach(tab)
            result["launcher"] = await page_paints(page)
            await page.close()
        with contextlib.suppress(Exception):
            bt = await browser.browser_target()
            hist = await bt.call("Browser.getHistograms", {"query": "Startup."})
            means = histogram_means(hist.get("histograms", []))
            result["chromium_startup_ms"] = means  # ms since Chromium's process started
            if "chromium" in started:
                result["chromium_startup_uptime"] = {
                    k: round(started["chromium"] + v / 1000, 2)
                    for k, v in means.items()
                    if "Time" in k or "Paint" in k
                }
            await bt.close()
    first = (result.get("launcher") or {}).get("paints_uptime", {}).get("first-paint")
    m = result["milestones"]
    if first and m.get("couch-session started (cage)"):
        result["gap_cage_to_first_paint_s"] = round(first - m["couch-session started (cage)"], 2)
    analyze = run(["systemd-analyze"]) or ""
    result["systemd_analyze"] = analyze.strip().splitlines()[:2]
    result["blame_top"] = parse_blame(run(["systemd-analyze", "blame", "--no-pager"]) or "")[:25]
    for unit in ("couch-session.service", "couchd.service", "network.target"):
        chain = run(["systemd-analyze", "critical-chain", unit, "--no-pager"]) or ""
        result[f"critical_chain {unit}"] = [line.rstrip() for line in chain.splitlines()[3:20]]
    result["network_online_wanted_by"] = (
        run(
            [
                "systemctl",
                "list-dependencies",
                "--reverse",
                "network-online.target",
                "--plain",
                "--no-pager",
            ]
        )
        or ""
    ).split()
    result["display"] = display_modes()
    return result


KODI_SETTINGS = [
    "videoplayer.adjustrefreshrate",
    "videoplayer.usedisplayasclock",
    "videoplayer.pauseafterrefreshchange",
    "videoplayer.useprimedecoder",
    "videoplayer.useprimedecoderforhw",
    "videoplayer.usev4l2codecs",
    "videoplayer.hqscalers",
    "videoscreen.screenmode",
    "videoscreen.whitelist",
    "videoscreen.limitguisize",
    "videoscreen.preferedstereoscopicmode",
]


async def check_plex(args) -> dict[str, Any]:
    if args.plex_cmd == "nav":
        return await plex_nav(args)
    if args.plex_cmd == "watch":
        return await plex_watch(args)
    return await plex_info(args)


async def ensure_kodi(launch: bool) -> Kodi:
    kodi = Kodi()
    try:
        await kodi.connect()
        return kodi
    except OSError:
        if not launch:
            raise SystemExit("Kodi isn't running; add --launch to open Plex first") from None
    async with aiohttp.ClientSession() as http:
        log("launching Plex")
        await Couch(http).command("apps.launch", {"app": "plex"}, timeout=90)
    for _ in range(60):
        try:
            await kodi.connect()
            return kodi
        except OSError:
            await asyncio.sleep(0.5)
    raise SystemExit("Kodi's JSON-RPC didn't answer")


async def plex_info(args) -> dict[str, Any]:
    kodi = await ensure_kodi(args.launch)
    result: dict[str, Any] = {
        "check": "plex info",
        "env": environment(),
        "display": display_modes(),
    }
    result["kodi"] = await kodi.call(
        "Application.GetProperties", {"properties": ["version", "name"]}
    )
    result["jsonrpc"] = await kodi.call("JSONRPC.Version")
    with contextlib.suppress(RuntimeError):
        details = await kodi.call(
            "Addons.GetAddonDetails",
            {"addonid": "script.plexmod", "properties": ["version", "enabled"]},
        )
        result["pm4k"] = details.get("addon")
    settings = {}
    for name in KODI_SETTINGS:
        try:
            settings[name] = (await kodi.call("Settings.GetSettingValue", {"setting": name})).get(
                "value"
            )
        except RuntimeError as e:
            settings[name] = f"unavailable: {e}"
    result["settings"] = settings
    result["screen"] = await kodi.labels(
        ["System.ScreenResolution", "System.FPS", "System.BuildVersion"]
    )
    result["focus"] = await kodi.labels(FOCUS_LABELS)
    result["players"] = await kodi.call("Player.GetActivePlayers")
    await kodi.close()
    return result


async def plex_watch(args) -> dict[str, Any]:
    kodi = await ensure_kodi(False)
    result: dict[str, Any] = {"check": "plex watch", "label": args.label, "env": environment()}
    sampler = Sampler(args.interval).start()
    rows = []
    deadline = time.monotonic() + args.seconds
    last_save = time.monotonic()
    while time.monotonic() < deadline:
        row: dict[str, Any] = {"t": round(time.monotonic() - T0, 1), "wall": time.time()}
        try:
            players = await kodi.call("Player.GetActivePlayers")
            video = next((p for p in players if p.get("type") == "video"), None)
            if video:
                props = await kodi.call(
                    "Player.GetProperties",
                    {
                        "playerid": video["playerid"],
                        "properties": [
                            "time",
                            "totaltime",
                            "speed",
                            "currentvideostream",
                            "currentaudiostream",
                        ],
                    },
                )
                row["player"] = props
            row.update(await kodi.labels(PLAYER_LABELS))
        except (OSError, RuntimeError, TimeoutError, ConnectionError) as e:
            row["error"] = str(e)
            await kodi.close()
            with contextlib.suppress(OSError):
                await kodi.connect()
        rows.append(row)
        if time.monotonic() - last_save > 60:
            save(f"plex-{args.label}", {**result, "partial": True, "rows": rows}, args.out)
            last_save = time.monotonic()
        await asyncio.sleep(1.0)
    await sampler.stop()
    await kodi.close()
    result.update(
        {
            **summarize_plex(rows),
            "system": sampler.summary(),
            "rows": rows,
            "system_samples": sampler.samples,
        }
    )
    return result


def kodi_seconds(t: dict[str, int] | None) -> float | None:
    if not t:
        return None
    return (
        t.get("hours", 0) * 3600
        + t.get("minutes", 0) * 60
        + t.get("seconds", 0)
        + t.get("milliseconds", 0) / 1000
    )


def summarize_plex(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What played and how, and whether playback kept time: a second of wall clock should be
    a second of film while the speed is 1. Less is a stall (buffering, a decoder that can't
    keep up)."""
    playing = [r for r in rows if r.get("player")]
    seen = lambda key: sorted({r.get(key) for r in playing if r.get(key)})  # noqa: E731
    stalls, worst = 0, None
    for a, b in itertools.pairwise(playing):
        if a["player"].get("speed") != 1 or b["player"].get("speed") != 1:
            continue
        film = (kodi_seconds(b["player"].get("time")) or 0) - (
            kodi_seconds(a["player"].get("time")) or 0
        )
        wall = b["wall"] - a["wall"]
        if wall <= 0 or film < 0:  # a seek
            continue
        ratio = film / wall
        if ratio < 0.8:
            stalls += 1
            if worst is None or ratio < worst["ratio"]:
                worst = {"at_s": b["t"], "ratio": round(ratio, 2)}
    refreshes = sorted({refresh_of(r.get("System.ScreenResolution", "")) for r in rows} - {None})
    return {
        "seconds_playing": len(playing),
        "titles": seen("VideoPlayer.Title"),
        "codec": seen("VideoPlayer.VideoCodec"),
        "resolution": seen("VideoPlayer.VideoResolution"),
        "decoder": seen("Player.Process(videodecoder)"),
        "hw_decoder": seen("Player.Process(videohwdecoder)"),
        "pixformat": seen("Player.Process(pixformat)"),
        "video_size": sorted(
            {
                f"{r.get('Player.Process(videowidth)')}x{r.get('Player.Process(videoheight)')}"
                for r in playing
            }
        ),
        "video_fps": seen("Player.Process(videofps)"),
        "screen": sorted(
            {r.get("System.ScreenResolution") for r in rows if r.get("System.ScreenResolution")}
        ),
        "screen_refresh_hz": refreshes,
        "gui_fps": summarize(
            [
                float(r["System.FPS"])
                for r in rows
                if (r.get("System.FPS") or "").replace(".", "").isdigit()
            ],
            1,
        ),
        "stalls": stalls,
        "worst_stall": worst,
        "audio": seen("Player.Process(audiodecoder)"),
    }


NAV_ACTIONS = {
    "up": "Input.Up",
    "down": "Input.Down",
    "left": "Input.Left",
    "right": "Input.Right",
    "select": "Input.Select",
    "back": "Input.Back",
    "info": "Input.Info",
    "menu": "Input.ContextMenu",
}


async def plex_nav(args) -> dict[str, Any]:
    """Moves around Kodi like the remote would, one step at a time, and says what's focused
    after each. `wait:2` waits 2 s; `action:NAME` runs Input.ExecuteAction (e.g. playpause,
    stop, codecinfo)."""
    kodi = await ensure_kodi(args.launch)
    steps = []
    for step in args.steps or ["look"]:
        if step.startswith("wait:"):
            await asyncio.sleep(float(step[5:]))
        elif step.startswith("action:"):
            await kodi.call("Input.ExecuteAction", {"action": step[7:]})
        elif step in NAV_ACTIONS:
            await kodi.call(NAV_ACTIONS[step])
        elif step != "look":
            raise SystemExit(
                f"unknown step {step!r}; one of {', '.join(NAV_ACTIONS)}, wait:S, action:NAME, look"
            )
        await asyncio.sleep(args.pause)
        focus = await kodi.labels(FOCUS_LABELS)
        steps.append({"step": step, **{k: v for k, v in focus.items() if v}})
        log(
            step,
            "→",
            focus.get("System.CurrentWindow"),
            "|",
            focus.get("ListItem.Label") or focus.get("System.CurrentControl"),
        )
    await kodi.close()
    return {"check": "plex nav", "steps": steps}


# --- CLI ------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="couchperf", description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, help="also save the result here, on the Pi")
    sub = p.add_subparsers(dest="check", required=True)

    s = sub.add_parser("sample", help="CPU, temperature, throttling for a while")
    s.add_argument("--seconds", type=float, default=60)
    s.add_argument("--interval", type=float, default=2)

    s = sub.add_parser("youtube", help="YouTube 1080p60 playback")
    s.add_argument("--minutes", type=float, default=30)
    s.add_argument("--short", action="store_true", help="3 minutes instead")
    s.add_argument("--video", default=DEFAULT_VIDEO)
    s.add_argument("--interval", type=float, default=5)
    s.add_argument("--leave", action="store_true", help="leave the video playing at the end")

    s = sub.add_parser("launcher", help="focus animation frame times")
    s.add_argument("--rounds", type=int, default=2)
    s.add_argument("--gap", type=float, default=0.6, help="seconds between key presses")
    s.add_argument("--window", type=float, default=400, help="ms after each press to look at")
    s.add_argument("--keep-trace", action="store_true", help="save the raw trace on the Pi")

    s = sub.add_parser("launch", help="cold launch times")
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--apps", nargs="+", default=["youtube", "plex"], choices=["youtube", "plex"])
    s.add_argument("--plex-settle", type=float, default=8, help="seconds in Plex before going home")

    sub.add_parser("boot", help="the last boot's timeline")

    s = sub.add_parser("plex", help="Kodi's player, display modes and settings")
    s.add_argument("plex_cmd", choices=["info", "watch", "nav"])
    s.add_argument(
        "steps",
        nargs="*",
        help="nav: up/down/left/right/select/back/info/menu, wait:S, action:NAME",
    )
    s.add_argument("--launch", action="store_true", help="open Plex first if Kodi isn't running")
    s.add_argument("--seconds", type=float, default=300, help="watch: how long")
    s.add_argument("--label", default="playback", help="watch: a name for this run, e.g. 4k-hevc")
    s.add_argument("--interval", type=float, default=5, help="watch: system sample interval")
    s.add_argument("--pause", type=float, default=0.8, help="nav: seconds after each step")
    return p


CHECKS = {
    "sample": check_sample,
    "youtube": check_youtube,
    "launcher": check_launcher,
    "launch": check_launch,
    "boot": check_boot,
    "plex": check_plex,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if aiohttp is None:
        raise SystemExit("needs aiohttp: run it with /opt/couch/.venv/bin/python")
    started = time.time()
    result = asyncio.run(CHECKS[args.check](args))
    result.setdefault("check", args.check)
    result["started"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started))
    result["seconds"] = round(time.time() - started, 1)
    name = args.check if args.check != "plex" else f"plex-{args.plex_cmd}"
    save(name, result, args.out)
    json.dump(result, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
