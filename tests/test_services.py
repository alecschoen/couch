import pytest

from couchd.bus import EventBus
from couchd.services import Service, ServiceError, ServiceRegistry
from couchd.state import LiveState


class Recorder(Service):
    def __init__(self, name, log, *, fail=False, critical=False):
        self.name = name
        self.critical = critical
        self._log = log
        self._fail = fail

    async def start(self):
        self._log.append(f"start {self.name}")
        if self._fail:
            raise RuntimeError("no adapter")

    async def stop(self):
        self._log.append(f"stop {self.name}")


def registry():
    state = LiveState(EventBus())
    return ServiceRegistry(state), state


async def test_starts_in_order_and_stops_in_reverse():
    reg, state = registry()
    log = []
    for name in ("cec", "audio", "bluetooth"):
        reg.add(Recorder(name, log))
    await reg.start_all()
    assert state.get("services") == {"cec": "running", "audio": "running", "bluetooth": "running"}
    await reg.stop_all()
    assert log == [
        "start cec",
        "start audio",
        "start bluetooth",
        "stop bluetooth",
        "stop audio",
        "stop cec",
    ]
    assert set(state.get("services").values()) == {"stopped"}


async def test_failed_service_does_not_stop_the_others():
    reg, state = registry()
    log = []
    reg.add(Recorder("bluetooth", log, fail=True))
    reg.add(Recorder("audio", log))
    await reg.start_all()
    assert state.get("services") == {"bluetooth": "failed", "audio": "running"}
    await reg.stop_all()
    assert "stop bluetooth" not in log


async def test_critical_failure_unwinds_and_raises():
    reg, state = registry()
    log = []
    reg.add(Recorder("audio", log))
    reg.add(Recorder("cec", log, fail=True, critical=True))
    reg.add(Recorder("bluetooth", log))
    with pytest.raises(ServiceError, match="cec"):
        await reg.start_all()
    assert log == ["start audio", "start cec", "stop audio"]
    assert state.get("services")["cec"] == "failed"


def test_duplicate_names_rejected():
    reg, _ = registry()
    reg.add(Recorder("audio", []))
    with pytest.raises(ValueError, match="duplicate"):
        reg.add(Recorder("audio", []))
