import logging
import os
import socket
import sys

from couchd.logs import ConsoleFormatter, JournalFormatter, under_journald
from couchd.systemd import notify, watchdog_interval


def test_notify_without_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify("READY=1") is False


def test_notify_sends_datagram(tmp_path, monkeypatch):
    path = tmp_path / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        assert notify("READY=1") is True
        assert server.recv(64) == b"READY=1"


def test_watchdog_interval(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog_interval() is None
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert watchdog_interval() == 15.0
    monkeypatch.setenv("WATCHDOG_PID", "1")
    assert watchdog_interval() is None


def record(level, msg, **extra):
    rec = logging.LogRecord("couchd.audio", level, __file__, 1, msg, (), None)
    rec.__dict__.update(extra)
    return rec


def test_journal_formatter_sets_priority_and_fields():
    text = JournalFormatter().format(
        record(logging.WARNING, "output lost", output="bluetooth", name_="My Speaker")
    )
    assert text == "<4>couchd.audio: output lost output=bluetooth name_='My Speaker'"


def test_journal_formatter_prefixes_every_traceback_line():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = record(logging.ERROR, "failed")
        rec.exc_info = sys.exc_info()
    lines = JournalFormatter().format(rec).splitlines()
    assert len(lines) > 2
    assert all(line.startswith("<3>") for line in lines)


def test_under_journald_checks_the_actual_stream(tmp_path, monkeypatch):
    with open(tmp_path / "out", "w") as f:
        st = os.fstat(f.fileno())
        monkeypatch.setenv("JOURNAL_STREAM", "1:2")  # inherited from a desktop session
        assert not under_journald(f)
        monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
        assert under_journald(f)


def test_console_formatter():
    text = ConsoleFormatter().format(record(logging.INFO, "hello", peer="127.0.0.1"))
    assert text.endswith("INFO    couchd.audio: hello peer=127.0.0.1")
