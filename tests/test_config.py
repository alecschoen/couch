from pathlib import Path

import pytest

from couchd.config import (
    BUNDLED_UI_DIR,
    DEVICE_STATE_PATH,
    ConfigError,
    defaults,
    load_config,
)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_missing_file_gives_defaults(tmp_path):
    config = load_config(tmp_path / "nope.toml")
    assert config == defaults()
    assert config.server.port == 8765
    assert config.paths.ui == BUNDLED_UI_DIR
    assert config.paths.state == DEVICE_STATE_PATH
    assert config.log.level == "info"


def test_no_path_gives_defaults():
    assert load_config(None) == defaults()


def test_empty_file_gives_defaults(tmp_path):
    assert load_config(write(tmp_path, "")) == defaults()


def test_dev_mode_keeps_state_out_of_var_lib(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert load_config(None, dev=True).paths.state == tmp_path / "couch" / "state.json"


def test_values_override_defaults(tmp_path):
    path = write(
        tmp_path,
        """
        [server]
        port = 9000
        [paths]
        state = "~/couch-state.json"
        [log]
        level = "debug"
        """,
    )
    config = load_config(path)
    assert config.server.port == 9000
    assert config.paths.state == Path("~/couch-state.json").expanduser()
    assert config.paths.ui == BUNDLED_UI_DIR
    assert config.log.level == "debug"
    assert config.warnings == ()


def test_unknown_keys_warn_but_load(tmp_path):
    path = write(tmp_path, "[server]\nport = 9000\nhost = '0.0.0.0'\n[bluetoth]\nx = 1\n")
    config = load_config(path)
    assert config.server.port == 9000
    assert len(config.warnings) == 2
    assert any("server.host" in w for w in config.warnings)
    assert any("[bluetoth]" in w for w in config.warnings)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[server]\nport = 'abc'", "server.port must be an integer"),
        ("[server]\nport = true", "server.port must be an integer"),
        ("[server]\nport = 70000", "server.port must be 1-65535"),
        ("[log]\nlevel = 'loud'", "log.level must be one of"),
        ("[paths]\nui = ''", "paths.ui must be a path string"),
        ("server = 1", "[server] must be a table"),
        ("[server\nport = 1", "config.toml"),
    ],
)
def test_bad_values_raise(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_config(write(tmp_path, text))
