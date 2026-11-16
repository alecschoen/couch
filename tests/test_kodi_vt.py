"""system/couch-kodi-vt: back to tty1 only once Kodi has let go of the screen."""

import importlib.machinery
import importlib.util
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("couch_kodi_vt", str(ROOT / "system/couch-kodi-vt"))
_spec = importlib.util.spec_from_loader("couch_kodi_vt", _loader)
vt = importlib.util.module_from_spec(_spec)
_loader.exec_module(vt)

CAGE = "framebuffer[728]:\n\tallocated by = cage\n"
KODI = "framebuffer[724]:\n\tallocated by = kodi.bin\n"


@pytest.fixture
def dri(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "FRAMEBUFFERS", str(tmp_path))
    monkeypatch.setattr(vt, "POLL", 0.005)
    for card in ("0", "1"):
        (tmp_path / card).mkdir()
    (tmp_path / "0" / "framebuffer").write_text("")
    return tmp_path / "1" / "framebuffer"


def test_kodi_framebuffers(dri):
    dri.write_text(CAGE + KODI)
    assert vt.kodi_framebuffers() is True
    dri.write_text(CAGE)
    assert vt.kodi_framebuffers() is False


def test_no_debugfs_cant_tell(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "FRAMEBUFFERS", str(tmp_path / "missing"))
    assert vt.kodi_framebuffers() is None


def test_leaving_waits_until_kodis_framebuffers_are_gone(dri):
    dri.write_text(CAGE + KODI)
    threading.Timer(0.1, lambda: dri.write_text(CAGE)).start()
    started = time.monotonic()
    vt.wait_for_kodi_to_let_go()
    assert 0.08 < time.monotonic() - started < 1.0


def test_leaving_gives_up_waiting(dri, monkeypatch, capsys):
    monkeypatch.setattr(vt, "WAIT", 0.05)
    dri.write_text(KODI)
    vt.wait_for_kodi_to_let_go()
    assert "switching back anyway" in capsys.readouterr().err


def test_enter_and_leave_switch_the_console(dri, monkeypatch):
    calls = []
    monkeypatch.setattr(vt, "set_mode", lambda n, mode: calls.append(("mode", n, mode)))
    monkeypatch.setattr(vt, "switch_to", lambda n: calls.append(("switch", n)))
    dri.write_text(CAGE)
    monkeypatch.setattr("sys.argv", ["couch-kodi-vt", "enter"])
    assert vt.main() == 0
    monkeypatch.setattr("sys.argv", ["couch-kodi-vt", "leave"])
    assert vt.main() == 0
    monkeypatch.setattr("sys.argv", ["couch-kodi-vt"])
    assert vt.main() == 2
    # graphics mode before tty2 shows; text mode again before leaving it, or the kernel
    # ignores the switch
    assert calls == [
        ("mode", 2, vt.KD_GRAPHICS),
        ("switch", 2),
        ("mode", 2, vt.KD_TEXT),
        ("switch", 1),
    ]
