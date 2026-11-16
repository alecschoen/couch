import os

import pytest

from couchd import files
from couchd.files import remove_leftovers, write_atomic
from couchd.state import SavedState


def test_write_atomic_replaces_and_keeps_mode(tmp_path):
    path = tmp_path / "a" / "settings.json"
    write_atomic(path, b"one\n")
    assert path.read_bytes() == b"one\n"
    assert path.stat().st_mode & 0o777 == 0o644
    path.chmod(0o600)
    write_atomic(path, b"two\n")
    assert path.read_bytes() == b"two\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.json"]


def test_write_atomic_fsyncs_the_file_and_the_directory(tmp_path, monkeypatch):
    synced = []
    real = os.fsync

    def fsync(fd):
        synced.append(os.path.isdir(f"/proc/self/fd/{fd}"))
        real(fd)

    monkeypatch.setattr(files.os, "fsync", fsync)
    write_atomic(tmp_path / "x", b"x")
    assert synced == [False, True]  # the file first, then its directory


def test_a_failed_write_leaves_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_atomic(path, b"old")

    def boom(*args):
        raise OSError("card pulled")

    monkeypatch.setattr(files.os, "replace", boom)
    with pytest.raises(OSError):
        write_atomic(path, b"new")
    assert path.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_leftovers_from_a_power_cut_are_removed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"a": 1}')
    left = tmp_path / ".state.json.couch-tmp.k2j3h4"
    left.write_text('{"a": ')
    other = tmp_path / ".settings.json.couch-tmp.zz"
    other.write_text("")
    assert remove_leftovers(path) == [left]
    assert other.exists()
    assert remove_leftovers(tmp_path / "missing" / "x.json") == []


def test_saved_state_ignores_a_half_written_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"display.evening": "sunset"}\n')
    (tmp_path / ".settings.json.couch-tmp.abc").write_text('{"display.ev')
    saved = SavedState(path)
    saved.load()
    assert saved.get("display.evening") == "sunset"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]
