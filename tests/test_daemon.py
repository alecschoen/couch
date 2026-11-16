import asyncio
import json
from dataclasses import replace

import aiohttp

from couchd.config import defaults
from couchd.daemon import Daemon
from couchd.services import Service


class Flaky(Service):
    name = "bluetooth"

    async def start(self):
        raise RuntimeError("no adapter")


class Fine(Service):
    name = "audio"

    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def make_config(tmp_path):
    config = defaults(dev=True)
    return replace(
        config,
        server=replace(config.server, port=0),
        paths=replace(config.paths, state=tmp_path / "state.json"),
    )


def make_daemon(tmp_path, *services):
    return Daemon(make_config(tmp_path), dev=True, services=services)


async def test_daemon_end_to_end(tmp_path):
    fine = Fine()
    daemon = make_daemon(tmp_path, Flaky(), fine)
    runner = asyncio.create_task(daemon.run())
    for _ in range(100):
        if daemon.port and daemon.state.get("services").get("audio") == "running":
            break
        await asyncio.sleep(0.02)

    base = f"http://127.0.0.1:{daemon.port}"
    async with aiohttp.ClientSession() as http, http.ws_connect(f"{base}/ws") as ws:
        snapshot = await ws.receive_json(timeout=2)
        assert snapshot["full"] is True
        assert snapshot["data"]["system"]["mode"] == "dev"
        assert snapshot["data"]["services"] == {"bluetooth": "failed", "audio": "running"}

        async with http.get(f"{base}/api/health") as resp:
            assert (await resp.json())["status"] == "degraded"

        daemon.saved.set("volume", 40)
        daemon.stopping.set()
        await asyncio.wait_for(runner, 5)

    assert fine.stopped
    assert json.loads((tmp_path / "state.json").read_text()) == {"volume": 40}


async def test_dev_mode_runs_the_fakes(tmp_path):
    daemon = Daemon(make_config(tmp_path), dev=True)
    await daemon.start()
    try:
        snap = daemon.state.snapshot()
        assert set(snap["services"].values()) == {"running"}
        assert {"tv", "audio", "bluetooth"} <= snap.keys()
        assert "remote.press" in daemon.commands.names()
    finally:
        await daemon.stop()


async def test_device_mode_has_the_tv_and_the_remote(tmp_path):
    daemon = Daemon(make_config(tmp_path), dev=False)
    assert [s.name for s in daemon.services] == ["tv", "remote"]
