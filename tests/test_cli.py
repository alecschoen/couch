import pytest

from couchd import __version__
from couchd.__main__ import main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize(("argv", "mode"), [([], "device"), (["--dev"], "dev")])
def test_mode(capsys, argv, mode):
    assert main(argv) == 0
    assert f"({mode} mode)" in capsys.readouterr().out
