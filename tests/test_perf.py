"""The performance checks' arithmetic (scripts/perf/couchperf.py, #23). The checks themselves
need the Pi; what they compute from what they read is tested here."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parent.parent / "scripts" / "perf" / "couchperf.py"
spec = importlib.util.spec_from_file_location("couchperf", PATH)
perf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(perf)


def test_percentiles_and_jank():
    assert perf.percentile([1, 2, 3, 4], 50) == 2.5
    assert perf.percentile([], 50) is None
    smooth = perf.jank([16.7] * 10)
    assert smooth["missed_frames"] == 0 and smooth["over_1.5_frames"] == 0
    # One 50 ms gap is two missed frames at 60 Hz.
    rough = perf.jank([16.7, 16.7, 50.0, 16.7])
    assert rough["missed_frames"] == 2 and rough["over_1.5_frames"] == 1
    assert perf.summarize([None, 3.0, 1.0])["n"] == 2


def test_throttled_bits():
    assert perf.parse_throttled("throttled=0x50005\n") == 0x50005
    assert perf.parse_throttled("50000\n") == 0x50000
    assert perf.decode_throttled(0x50005) == [
        "under-voltage",
        "throttled",
        "under-voltage has occurred",
        "throttling has occurred",
    ]
    assert perf.decode_throttled(0) == []


def test_proc_stat_with_a_space_in_the_name():
    # pid (comm) state ppid ... utime(14) stime(15) ... starttime(22)
    fields = ["S", "1"] + ["0"] * 9 + ["120", "30"] + ["0"] * 6 + ["4567"] + ["0"] * 10
    comm, ppid, cpu, start = perf.parse_proc_stat(f"42 (Web Content) {' '.join(fields)}")
    assert (comm, ppid, cpu, start) == ("Web Content", 1, 150, 4567)
    assert perf.group_of("chromium") == "chromium"
    assert perf.group_of("kodi.bin") == "kodi"
    assert perf.group_of("bash") is None


def test_cpu_percent():
    before = {"cpu": (100, 1000), "cpu0": (50, 500)}
    after = {"cpu": (350, 2000), "cpu0": (300, 1000)}
    assert perf.cpu_percent(before, after) == {"cpu": 25.0, "cpu0": 50.0}


def test_sample_summary_ors_the_throttle_flags():
    samples = [
        {
            "t": 0,
            "cpu": 10,
            "cores": {"cpu0": 20},
            "programs": {"chromium": 50},
            "temp_c": 60,
            "arm_mhz": 1800,
            "throttled": "0x0",
            "mem_available_mb": 900,
        },
        {
            "t": 5,
            "cpu": 30,
            "cores": {"cpu0": 80},
            "programs": {"chromium": 70},
            "temp_c": 81,
            "arm_mhz": 1500,
            "throttled": "0x80008",
            "mem_available_mb": 800,
        },
    ]
    s = perf.summarize_samples(samples)
    assert s["throttled"] == "0x80008"
    assert s["throttled_flags"] == ["soft temperature limit", "soft temperature limit has occurred"]
    assert s["temp_c"]["max"] == 81
    assert s["programs_percent_of_a_core"]["chromium"]["mean"] == 60
    assert s["mem_available_mb_min"] == 800


def edid_with(vics: list[int], name: str = "SONY TV") -> bytes:
    base = bytearray(128)
    base[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    base[72:77] = b"\0\0\0\xfc\0"
    base[77:90] = (name.encode() + b"\n").ljust(13, b" ")
    base[126] = 1
    ext = bytearray(128)
    ext[0], ext[1] = 0x02, 0x03
    block = bytes([(2 << 5) | len(vics), *vics])
    ext[4 : 4 + len(block)] = block
    ext[2] = 4 + len(block)
    return bytes(base + ext)


def test_edid_modes():
    edid = edid_with([16 | 0x80, 31, 32, 4])  # 1080p60 native, 1080p50, 1080p24, 720p60
    assert perf.edid_vics(edid) == [16, 31, 32, 4]
    assert perf.edid_name(edid) == "SONY TV"
    assert perf.refresh_of("1920x1080 @ 23.98Hz - Full Screen") == 23.98
    assert perf.refresh_of("") is None


def trace_events():
    events = []
    for i, key_ms in enumerate([1000.0, 2000.0]):
        events.append(
            {
                "name": "TimeStamp",
                "ph": "I",
                "ts": key_ms * 1000,
                "args": {"data": {"message": "couchperf:key"}},
            }
        )
        t = key_ms
        for n in range(20):
            state = "STATE_DROPPED" if (i == 1 and n == 5) else "STATE_PRESENTED_ALL"
            events.append(
                {
                    "name": "PipelineReporter",
                    "ph": "b",
                    "pid": 7,
                    "id2": {"local": hex(n)},
                    "ts": t * 1000,
                    "args": {"frame_reporter": {"state": state}},
                }
            )
            events.append(
                {
                    "name": "PipelineReporter",
                    "ph": "e",
                    "pid": 7,
                    "id2": {"local": hex(n)},
                    "ts": (t + 5) * 1000,
                    "args": {},
                }
            )
            t += 16.7
    return events


def test_trace_analysis_finds_the_dropped_frame():
    result = perf.analyze_trace(trace_events(), window_ms=400)
    assert result["key_presses"] == 2
    assert result["dropped_frames"] == 1
    assert result["frame_states"]["STATE_PRESENTED_ALL"] == 39
    # The dropped frame leaves one 33 ms gap between presented frames.
    assert result["presented_interval_ms"]["missed_frames"] == 1
    assert result["frame_latency_ms"]["p50"] == 5.0
    assert result["draw_and_swap_interval_ms"] is None


def test_raf_analysis():
    frames = [1000 + 16.7 * n for n in range(30)] + [2000 + 16.7 * n for n in (0, 1, 2, 4, 5)]
    result = perf.analyze_raf(frames, [1000, 2000], window_ms=200)
    assert result["interval_ms"]["missed_frames"] == 1


def reading(t, total, dropped, **kw):
    return {"t": t, "wall": t * 1000, "total": total, "dropped": dropped, "paused": False, **kw}


def test_youtube_frames_across_a_restart_and_an_ad():
    stats = [
        reading(0, 0, 0, vid="ad1"),
        reading(5, 300, 9, vid="ad1"),  # an ad: not counted
        reading(10, 0, 0, vid="v"),  # counters restart with the video
        reading(15, 300, 0, vid="v"),
        reading(20, 600, 3, vid="v"),
        None,
        reading(25, 900, 3, vid="v"),  # after a failed reading: no delta
        reading(30, 1200, 3, vid="v", paused=True),
    ]
    result = perf.youtube_frames(stats, "v")
    assert result["frames"] == 600 and result["dropped"] == 3
    assert result["dropped_percent"] == 0.5
    assert result["fps"]["mean"] == 60.0
    assert result["worst_interval"]["dropped"] == 3


def kodi_row(t, film, speed=1, screen="1920x1080 @ 23.98Hz - Full Screen"):
    return {
        "t": t,
        "wall": t,
        "player": {"speed": speed, "time": {"hours": 0, "minutes": 0, "seconds": film}},
        "System.ScreenResolution": screen,
        "VideoPlayer.VideoCodec": "hevc",
        "Player.Process(videodecoder)": "hevc-drm_prime",
        "Player.Process(videowidth)": "3840",
        "Player.Process(videoheight)": "2160",
        "System.FPS": "23.98",
    }


def test_plex_summary_finds_stalls_and_the_refresh_switch():
    rows = [{"t": 0, "wall": 0, "System.ScreenResolution": "1920x1080 @ 60.00Hz - Full Screen"}]
    rows += [kodi_row(t, t) for t in range(1, 6)]
    rows += [kodi_row(6, 5.2), kodi_row(7, 6.2)]  # a stall: 0.2 s of film in 1 s
    rows += [kodi_row(8, 100)]  # a seek forward is fine
    result = perf.summarize_plex(rows)
    assert result["stalls"] == 1 and result["worst_stall"]["ratio"] == 0.2
    assert result["screen_refresh_hz"] == [23.98, 60.0]
    assert result["video_size"] == ["3840x2160"]
    assert result["decoder"] == ["hevc-drm_prime"]
    assert (
        perf.kodi_seconds({"hours": 1, "minutes": 2, "seconds": 3, "milliseconds": 500}) == 3723.5
    )


def journal(t, who, msg, **kw):
    return {
        "__MONOTONIC_TIMESTAMP": str(int(t * 1e6)),
        "SYSLOG_IDENTIFIER": who,
        "MESSAGE": msg,
        **kw,
    }


def test_boot_milestones_from_the_journal():
    entries = [
        journal(
            0.9,
            "kernel",
            "simple-framebuffer 3e2fe000.framebuffer: fb0: simplefb registered!",
            _TRANSPORT="kernel",
        ),
        journal(
            3.0,
            "systemd",
            "Starting systemd-fsck-root.service - File System Check on Root Device...",
        ),
        journal(
            3.8, "systemd", "Finished systemd-fsck-root.service - File System Check on Root Device."
        ),
        journal(8.0, "systemd", "Starting NetworkManager.service - Network Manager..."),
        journal(8.4, "systemd", "Started NetworkManager.service - Network Manager."),
        journal(11.1, "systemd", "Reached target network.target - Network."),
        journal(12.0, "couchd", "couchd.daemon: couchd starting version=0.1.0 mode=device"),
        journal(17.4, "systemd", "Started couch-session.service - Couch TV session."),
        journal(24.7, "couchd", "couchd.splash: launcher drew its first frame uptime=24.7"),
        journal(25.2, "couchd", "couchd.splash: splash handed over uptime=25.2 ms=0.1"),
        journal(26.0, "couchd", "couchd.cec_tv: TV power=on showing_couch=True"),
    ]
    lines = perf.journal_lines(entries)
    m = perf.boot_milestones(lines)
    assert m["kernel: firmware framebuffer"] == 0.9
    assert m["network.target"] == 11.1
    assert m["couch-session started (cage)"] == 17.4
    assert m["session splash handed over"] == 25.2
    assert m["TV on"] == 26.0
    assert m["multi-user.target"] is None
    d = perf.unit_durations(lines, [r"systemd-fsck-root", r"NetworkManager\.service", "cloud-init"])
    assert d == {"systemd-fsck-root": 0.8, r"NetworkManager\.service": 0.4, "cloud-init": None}


def test_blame_and_histograms():
    blame = (
        "  2.716s NetworkManager-wait-online.service\n"
        "1min 2.5s slow.service\n"
        "  812ms systemd-fsck@x.service\n"
    )
    assert perf.parse_blame(blame) == [
        (2.716, "NetworkManager-wait-online.service"),
        (62.5, "slow.service"),
        (0.812, "systemd-fsck@x.service"),
    ]
    hist = [
        {"name": "Startup.FirstWebContents.NonEmptyPaint3", "sum": 5400, "count": 1},
        {"name": "Empty", "sum": 0, "count": 0},
    ]
    assert perf.histogram_means(hist) == {"Startup.FirstWebContents.NonEmptyPaint3": 5400.0}


async def test_kodi_json_rpc_skips_notifications_and_split_messages():
    async def serve(reader, writer):
        data = await reader.read(4096)
        request = json.loads(data)
        reply = json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": {"version": {"major": 21}}}
        )
        note = json.dumps({"jsonrpc": "2.0", "method": "Player.OnPlay", "params": {}})
        # A notification first, then the reply in two pieces.
        writer.write((note + reply[:10]).encode())
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(reply[10:].encode())
        await writer.drain()
        await reader.read(1)
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    kodi = perf.Kodi(("127.0.0.1", port))
    try:
        result = await kodi.call("Application.GetProperties", {"properties": ["version"]})
        assert result == {"version": {"major": 21}}
    finally:
        await kodi.close()
        server.close()
        await server.wait_closed()


def test_the_cli_knows_every_check():
    p = perf.parser()
    for argv in (
        ["sample"],
        ["youtube", "--short"],
        ["launcher"],
        ["launch", "--apps", "plex"],
        ["boot"],
        ["plex", "nav", "down", "wait:1", "select"],
    ):
        args = p.parse_args(argv)
        assert args.check in perf.CHECKS
    with pytest.raises(SystemExit):
        p.parse_args(["plex", "dance"])
