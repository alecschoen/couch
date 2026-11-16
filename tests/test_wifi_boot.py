"""system/couch-wifi-boot and the files around it: Wi-Fi off survives a reboot (#31)."""

import importlib.machinery
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_loader = importlib.machinery.SourceFileLoader("wifiboot", str(ROOT / "system/couch-wifi-boot"))
_spec = importlib.util.spec_from_loader("wifiboot", _loader)
assert _spec is not None
wifiboot = importlib.util.module_from_spec(_spec)
_loader.exec_module(wifiboot)


def nm_state(tmp_path, text):
    path = tmp_path / "NetworkManager.state"
    path.write_text(text)
    return path


def radios(tmp_path, *kinds):
    """A fake /sys/class/rfkill: (type, soft) per radio."""
    root = tmp_path / "rfkill"
    for i, (kind, soft) in enumerate(kinds):
        radio = root / f"rfkill{i}"
        radio.mkdir(parents=True)
        (radio / "type").write_text(f"{kind}\n")
        (radio / "soft").write_text(f"{soft}\n")
    return root


def test_reads_networkmanagers_choice(tmp_path):
    off = "[main]\nNetworkingEnabled=true\nWirelessEnabled=false\nWWANEnabled=true\n"
    assert wifiboot.wifi_off(nm_state(tmp_path, off)) is True
    assert wifiboot.wifi_off(nm_state(tmp_path, "[main]\nWirelessEnabled=true\n")) is False


def test_no_or_broken_state_means_on(tmp_path):
    assert wifiboot.wifi_off(tmp_path / "missing") is False
    assert wifiboot.wifi_off(nm_state(tmp_path, "not an ini file")) is False
    assert wifiboot.wifi_off(nm_state(tmp_path, "[main]\nNetworkingEnabled=true\n")) is False


def test_blocks_only_wifi(tmp_path):
    root = radios(tmp_path, ("bluetooth", 0), ("wlan", 0))
    assert wifiboot.block_wlan(root) == ["rfkill1"]
    assert (root / "rfkill0/soft").read_text().strip() == "0"  # Bluetooth stays on
    assert (root / "rfkill1/soft").read_text().strip() == "1"


def test_already_blocked_is_left_alone(tmp_path):
    root = radios(tmp_path, ("wlan", 1))
    assert wifiboot.block_wlan(root) == []


def test_a_radio_that_cant_be_written_doesnt_stop_it(tmp_path, capsys):
    root = radios(tmp_path, ("wlan", 0), ("wlan", 0))
    (root / "rfkill0/soft").unlink()
    (root / "rfkill0/soft").mkdir()  # writing it fails
    assert wifiboot.block_wlan(root) == ["rfkill1"]
    assert "rfkill0" in capsys.readouterr().err


def test_main_never_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(wifiboot, "NM_STATE", nm_state(tmp_path, "[main]\nWirelessEnabled=false\n"))
    monkeypatch.setattr(wifiboot, "RFKILL", tmp_path / "no-rfkill")
    assert wifiboot.main() == 0


def test_runs_when_wlan0_appears():
    rule = (ROOT / "system/70-couch-wifi.rules").read_text()
    assert re.search(r'SUBSYSTEM=="net", KERNEL=="wlan\*".*"couch-wifi-boot.service"', rule)
    unit = (ROOT / "system/couch-wifi-boot.service").read_text()
    assert "DefaultDependencies=no" in unit
    assert "ExecStart=/opt/couch/bin/couch-wifi-boot" in unit
    assert not re.search(r"^\[Install\]", unit, re.M)  # only the udev rule starts it


def test_systemd_rfkill_forgets_only_wifi():
    dropin = (ROOT / "system/systemd-rfkill-couch.conf").read_text()
    pre = re.findall(r"^ExecStartPre=(.*)$", dropin, re.M)
    assert pre == ["-/bin/sh -c 'rm -f /var/lib/systemd/rfkill/*:wlan'"]
    assert "bluetooth" not in pre[0]


def test_install_sh_installs_and_removes_them():
    install = (ROOT / "install.sh").read_text()
    assert '"$SRC/system/couch-wifi-boot" "$PREFIX/bin/couch-wifi-boot" 755' in install
    assert "RFKILL_DROPIN=$UNIT_DIR/systemd-rfkill.service.d/couch.conf" in install
    assert 'rm -f "$WIFI_RULE" "$WIFI_BOOT_UNIT" "$RFKILL_DROPIN"' in install
    # It never switches Wi-Fi itself: couchd does, from the setting.
    assert not re.search(r"nmcli radio|rfkill (un)?block wlan|WirelessEnabled", install)


def test_polkit_lets_couchd_switch_wifi_only():
    rule = (ROOT / "system/couch.rules").read_text()
    nm = set(re.findall(r'"(org\.freedesktop\.NetworkManager\.[\w-]+)"', rule))
    assert nm == {"org.freedesktop.NetworkManager.enable-disable-wifi"}
