"""Command-line entry point: `couchd` or `python -m couchd`."""

import argparse

from couchd import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="couchd", description=__doc__)
    parser.add_argument("--version", action="version", version=f"couchd {__version__}")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="run on a PC with fake CEC, audio and Bluetooth backends",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "dev" if args.dev else "device"
    print(f"couchd {__version__} ({mode} mode): nothing to run yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
