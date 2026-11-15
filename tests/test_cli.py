import pytest

from couchd import __version__
from couchd.__main__ import main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_check_config_defaults(capsys):
    assert main(["--dev", "--check-config"]) == 0
    out = capsys.readouterr().out
    assert "config: (defaults)" in out
    assert "server.port = 8765" in out


def test_check_config_reports_errors(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 'x'\n[extra]\n")
    assert main(["--check-config", "--config", str(path)]) == 1
    assert "error:" in capsys.readouterr().out


def test_check_config_reports_warnings(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 9000\nhost = 'x'\n")
    assert main(["--check-config", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "warning:" in out
    assert "server.port = 9000" in out
