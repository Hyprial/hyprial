#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/hyprial-p1-uv-cache}
export UV_CACHE_DIR

cd "$PROJECT_DIR"
uv run --locked python "$SCRIPT_DIR/queryable.py"
uv run --locked python "$SCRIPT_DIR/liveliness.py"
uv run --locked python "$SCRIPT_DIR/storage.py" "$@"
