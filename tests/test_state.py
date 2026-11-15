import asyncio
import json

from couchd.bus import EventBus
from couchd.state import LiveState, SavedState


def drain(sub):
    messages = []
    while not sub._queue.empty():
        messages.append(sub._queue.get_nowait())
    return messages


async def test_update_publishes_only_changed_keys():
    bus = EventBus()
    state = LiveState(bus)
    sub = bus.subscribe()
    state.update("audio", {"output": "tv", "volume": 30})
    state.update("audio", {"output": "tv", "volume": 35})
    state.update("audio", {"output": "tv"})
    assert drain(sub) == [
        {"type": "state", "full": False, "data": {"audio": {"output": "tv", "volume": 30}}},
        {"type": "state", "full": False, "data": {"audio": {"volume": 35}}},
    ]
    assert state.snapshot() == {"audio": {"output": "tv", "volume": 35}}


async def test_snapshot_and_messages_are_copies():
    bus = EventBus()
    state = LiveState(bus)
    sub = bus.subscribe()
    devices = [{"name": "Speaker"}]
    state.update("bluetooth", {"devices": devices})
    devices.append({"name": "sneaky"})
    state.snapshot()["bluetooth"]["devices"].clear()
    (message,) = drain(sub)
    message["data"]["bluetooth"]["devices"].clear()
    assert state.get("bluetooth") == {"devices": [{"name": "Speaker"}]}


async def test_saved_state_round_trip(tmp_path):
    path = tmp_path / "sub" / "state.json"
    saved = SavedState(path, save_delay=0)
    saved.set("audio.preferred", "bluetooth:AA:BB")
    saved.flush()
    assert json.loads(path.read_text()) == {"audio.preferred": "bluetooth:AA:BB"}

    again = SavedState(path)
    again.load()
    assert again.get("audio.preferred") == "bluetooth:AA:BB"
    assert again.get("missing", "default") == "default"


async def test_saves_are_debounced(tmp_path):
    path = tmp_path / "state.json"
    saved = SavedState(path, save_delay=0.05)
    for volume in range(10):
        saved.set("volume", volume)
    assert not path.exists()
    await asyncio.sleep(0.1)
    assert json.loads(path.read_text()) == {"volume": 9}


async def test_flush_without_changes_does_not_write(tmp_path):
    path = tmp_path / "state.json"
    SavedState(path).flush()
    assert not path.exists()


async def test_corrupt_file_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    saved = SavedState(path)
    saved.load()
    assert saved.get("anything") is None
    assert (tmp_path / "state.json.bad").read_text() == "{not json"
    assert not path.exists()


def test_set_outside_a_loop_writes_immediately(tmp_path):
    path = tmp_path / "state.json"
    SavedState(path).set("a", 1)
    assert json.loads(path.read_text()) == {"a": 1}
