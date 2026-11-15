"""couchd: the Couch background service (CEC, input, audio, Bluetooth, apps)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("couchd")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+dev"
