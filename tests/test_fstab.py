"""system/couch-fstab: adds mount options to /etc/fstab, and undoes exactly that."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("couch_fstab", str(ROOT / "system/couch-fstab"))
_spec = importlib.util.spec_from_loader("couch_fstab", _loader)
cf = importlib.util.module_from_spec(_spec)
_loader.exec_module(cf)

# Raspberry Pi OS Lite's fstab, as the image ships it.
STOCK = (
    "proc            /proc           proc    defaults          0       0\n"
    "PARTUUID=21f4d5e7-01  /boot/firmware  vfat    defaults          0       2\n"
    "PARTUUID=21f4d5e7-02  /               ext4    defaults,noatime  0       1\n"
    "# a swapfile is not a swap partition, no need to use swapon|off for that\n"
)
OURS = ["/=noatime", "/boot/firmware=noatime"]


@pytest.fixture
def fstab(tmp_path):
    path = tmp_path / "fstab"
    path.write_text(STOCK)
    return path


def run(fstab, *args):
    assert cf.main(["--fstab", str(fstab), *args]) == 0


def test_adds_only_what_is_missing_and_reverts_exactly(fstab, capsys):
    run(fstab, "apply", "sd-card", *OURS)
    lines = fstab.read_text().splitlines()
    assert (
        lines[1]
        == "PARTUUID=21f4d5e7-01  /boot/firmware  vfat    defaults,noatime          0       2"
    )
    assert lines[2] == STOCK.splitlines()[2]  # noatime was there before Couch
    assert lines[0] == STOCK.splitlines()[0] and lines[3] == STOCK.splitlines()[3]
    assert (fstab.parent / "fstab.couch-orig").read_text() == STOCK
    state = json.loads((fstab.parent / "fstab.couch.json").read_text())
    assert state == {
        "before": {"/": ["defaults", "noatime"], "/boot/firmware": ["defaults"]},
        "owners": {"sd-card": {"/": ["noatime"], "/boot/firmware": ["noatime"]}},
    }
    assert "/boot/firmware" in capsys.readouterr().out

    run(fstab, "revert", "sd-card")
    assert fstab.read_text() == STOCK
    assert sorted(p.name for p in fstab.parent.iterdir()) == ["fstab"]


def test_second_apply_changes_nothing(fstab, capsys):
    run(fstab, "apply", "sd-card", *OURS)
    capsys.readouterr()
    before = fstab.read_text()
    run(fstab, "apply", "sd-card", *OURS)
    assert fstab.read_text() == before
    assert capsys.readouterr().out == ""


def test_revert_never_removes_an_option_that_was_there_before(fstab):
    run(fstab, "apply", "sd-card", *OURS)
    run(fstab, "revert", "sd-card")
    assert "defaults,noatime" in fstab.read_text().splitlines()[2]


def test_dropping_an_option_from_apply_takes_it_out(fstab):
    run(fstab, "apply", "sd-card", "/boot/firmware=noatime", "/boot/firmware=nodiratime")
    assert "defaults,noatime,nodiratime" in fstab.read_text()
    run(fstab, "apply", "sd-card", "/boot/firmware=noatime")
    assert "defaults,noatime " in fstab.read_text().splitlines()[1]
    assert "nodiratime" not in fstab.read_text()


def test_owners_are_independent(fstab):
    run(fstab, "apply", "sd-card", "/boot/firmware=noatime")
    run(fstab, "apply", "other", "/boot/firmware=noatime", "/proc=hidepid=2")
    run(fstab, "revert", "sd-card")
    text = fstab.read_text()
    assert "defaults,noatime" in text.splitlines()[1]  # "other" still wants it
    assert "defaults,hidepid=2" in text.splitlines()[0]
    run(fstab, "revert", "other")
    assert fstab.read_text() == STOCK
    assert sorted(p.name for p in fstab.parent.iterdir()) == ["fstab"]


def test_unknown_mount_point_changes_nothing(fstab):
    with pytest.raises(SystemExit, match="no entry for /data"):
        run(fstab, "apply", "sd-card", "/data=noatime")
    assert fstab.read_text() == STOCK
    assert sorted(p.name for p in fstab.parent.iterdir()) == ["fstab"]


def test_comments_and_odd_spacing_are_left_alone(tmp_path):
    path = tmp_path / "fstab"
    text = (
        "# / was on /dev/sda1 during installation\n"
        "\n"
        "UUID=abc\t/\text4\terrors=remount-ro\t0\t1\n"
        "tmpfs /mnt/my\\040disk tmpfs defaults\n"
    )
    path.write_text(text)
    run(path, "apply", "sd-card", "/=noatime", "/mnt/my disk=noatime")
    assert path.read_text() == (
        "# / was on /dev/sda1 during installation\n"
        "\n"
        "UUID=abc\t/\text4\terrors=remount-ro,noatime\t0\t1\n"
        "tmpfs /mnt/my\\040disk tmpfs defaults,noatime\n"
    )
    run(path, "revert", "sd-card")
    assert path.read_text() == text


def test_refuses_a_file_that_would_change_shape():
    with pytest.raises(SystemExit, match="different number of lines"):
        cf.check_shape("a / ext4 defaults 0 1\n", "a / ext4 defaults 0 1\nb\n")
    with pytest.raises(SystemExit, match="beyond its options"):
        cf.check_shape("a / ext4 defaults 0 1\n", "a /x ext4 defaults 0 1\n")
    with pytest.raises(SystemExit, match="bad options"):
        cf.check_shape("a / ext4 defaults 0 1\n", "a / ext4 defaults,,x 0 1\n")


def test_refuses_when_findmnt_finds_more_problems(fstab, monkeypatch):
    calls = iter([(0, 2), (1, 2)])
    monkeypatch.setattr(cf, "findmnt_problems", lambda text: next(calls))
    with pytest.raises(SystemExit, match="findmnt --verify"):
        run(fstab, "apply", "sd-card", *OURS)
    assert fstab.read_text() == STOCK


def test_bad_arguments():
    with pytest.raises(SystemExit):
        cf.main(["apply", "sd-card", "noatime"])
    with pytest.raises(SystemExit):
        cf.main(["apply", "sd card", "/=noatime"])
