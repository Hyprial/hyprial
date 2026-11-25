#!/usr/bin/env sh
# 在本机启动静态 HTTP 服务，打开 http://127.0.0.1:8765/ 查看能力全景页。
# 用法：sh docs/gui/org/serve.sh [port]
set -eu
REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
PORT=${1:-8765}
cd "$REPO_ROOT"
exec uv run --no-project --python 3.12 python -m http.server "$PORT" --bind 127.0.0.1 --directory docs/gui/org
