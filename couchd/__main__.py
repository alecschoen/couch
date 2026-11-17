"""Command-line entry point: `couchd` or `python -m couchd`."""

import argparse
import asyncio
import logging
from pathlib import Path

from couchd import __version__
from couchd.config import DEVICE_CONFIG_PATH, Config, ConfigError, defaults, load_config
from couchd.daemon import Daemon, run_daemon
from couchd.logs import setup_logging

log = logging.getLogger("couchd")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="couchd", description=__doc__)
    parser.add_argument("--version", action="version", version=f"couchd {__version__}")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="run on a PC with fake CEC, audio and Bluetooth backends",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"config file (default: {DEVICE_CONFIG_PATH}, or none with --dev)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="load the config, print the result and exit",
    )
    return parser


def _config_path(args: argparse.Namespace) -> Path | None:
    if args.config is not None:
        return args.config
    return None if args.dev else DEVICE_CONFIG_PATH


def check_config(path: Path | None, dev: bool) -> int:
    try:
        config = load_config(path, dev=dev)
    except ConfigError as e:
        print(f"error: {e}")
        return 1
    for warning in config.warnings:
        print(f"warning: {warning}")
    print(f"config: {path if path and path.exists() else '(defaults)'}")
    print(f"server.port = {config.server.port}")
    print(f"paths.state = {config.paths.state}")
    print(f"log.level   = {config.log.level}")
    return 0


def _load(path: Path | None, dev: bool) -> Config:
    # A broken config must not leave the TV on a black screen: fall back to defaults,
    # log loudly, and let the user fix it.
    try:
        config = load_config(path, dev=dev)
    except ConfigError as e:
        setup_logging()
        log.error("config ignored, using defaults", extra={"error": str(e)})
        return defaults(dev=dev)
    setup_logging(config.log.level)
    for warning in config.warnings:
        log.warning(warning)
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = _config_path(args)
    if args.check_config:
        return check_config(path, args.dev)
    config = _load(path, args.dev)
    daemon = Daemon(config, dev=args.dev)
    return asyncio.run(run_daemon(daemon))


if __name__ == "__main__":
    raise SystemExit(main())
