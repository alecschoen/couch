"""scripts/cec-button-survey.sh: its summary of a cec-ctl monitor log (COUCH-9).

The log lines are what `cec-ctl --monitor --show-raw` prints, each with the script's
millisecond timestamp in front.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "cec-button-survey.sh"

HELD_DOWN = """\
1000 Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):
1000 \tui-cmd: down (0x02)
1000 \tRaw: 04:44:02
1500 Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):
1500 \tui-cmd: down (0x02)
2000 Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):
2000 \tui-cmd: down (0x02)
2950 Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_RELEASED (0x45)
3000 Transmitted by Playback Device 1 to TV (4 to 0): GIVE_DEVICE_POWER_STATUS (0x8f)
3010 Received from TV to Playback Device 1 (0 to 4): REPORT_POWER_STATUS (0x90):
3010 \tpwr-state: on (0x00)
3100 Received from TV to all (0 to 15): VENDOR_COMMAND_WITH_ID (0xa0):
3100 \tvendor-id: 0x080046 (Sony)
"""


def summarise(tmp_path, log: str) -> list[str]:
    path = tmp_path / "monitor.log"
    path.write_text(log)
    out = subprocess.run(
        ["bash", str(SCRIPT), "--summarise", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [cell.strip() for cell in out.strip().split("|")]


def test_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_a_held_button(tmp_path):
    arrived, to, releases, gaps, held, other = summarise(tmp_path, HELD_DOWN)
    assert arrived == "down (0x02) x3"
    assert to == "0 to 4"
    assert releases == "1"
    assert gaps == "500,500"
    assert held == "1950 ms"
    # Power polls and their replies aren't the button's; the TV's vendor command is listed.
    assert other == "VENDOR_COMMAND_WITH_ID (0 to 15) vendor-id: 0x080046 (Sony)"


def test_nothing_arrived(tmp_path):
    arrived, *_ = summarise(tmp_path, "")
    assert arrived == "nothing"


@pytest.mark.skipif(shutil.which("mawk") is None, reason="mawk not installed")
def test_works_with_mawk(tmp_path, monkeypatch):
    # Raspberry Pi OS's awk is mawk.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "awk").symlink_to(shutil.which("mawk"))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert summarise(tmp_path, HELD_DOWN)[0] == "down (0x02) x3"
