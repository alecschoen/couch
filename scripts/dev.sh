#!/bin/sh
# Run couchd on this PC with fake hardware and open the launcher. Extra args go to couchd.
set -e
cd "$(dirname "$0")/.."
uv sync --quiet
exec uv run couchd --dev "$@"
