#!/usr/bin/env bash
# Pull dev, build the GUI and Tailcat from the same checkout, then launch a
# disposable daemon-backed GUI. The previous run owned by this script is
# stopped and removed before a new run is created.

set -euo pipefail
umask 077

ROOT="$(git rev-parse --show-toplevel)"
ORIGINAL_HOME="${HOME}"

usage() {
  cat <<'USAGE'
Usage: scripts/build/run-gui-dev-isolated.sh [options]

Build the current dev checkout and launch a fresh daemon-backed GUI in an
isolated home. Environment variables remain supported; options take
precedence so a new machine needs no preconfigured shell.

Options:
  --network-mode MODE   networked (default), labrelay, or isolated
  --port PORT           GUI port, or 0 for an available port
  --run-root PATH       parent directory for the disposable run
  --go-cache-home PATH  Go module/build cache parent
  --no-open             do not open the official browser handoff
  --open                open the official browser handoff (default)
  -h, --help            show this help
USAGE
}

NO_OPEN="${HYPRIAL_GUI_NO_OPEN:-0}"
REQUESTED_PORT="${HYPRIAL_GUI_PORT:-0}"
NETWORK_MODE="${HYPRIAL_GUI_NETWORK_MODE:-networked}"
BASE_ROOT="${HYPRIAL_GUI_RUN_ROOT:-}"
GO_CACHE_HOME="${HYPRIAL_GUI_GO_CACHE_HOME:-${ORIGINAL_HOME}}"

while (($#)); do
  case "$1" in
    --network-mode|--mode)
      [[ $# -ge 2 ]] || { echo "$1 requires a value" >&2; exit 2; }
      NETWORK_MODE="$2"; shift 2 ;;
    --port)
      [[ $# -ge 2 ]] || { echo "$1 requires a value" >&2; exit 2; }
      REQUESTED_PORT="$2"; shift 2 ;;
    --run-root)
      [[ $# -ge 2 ]] || { echo "$1 requires a value" >&2; exit 2; }
      BASE_ROOT="$2"; shift 2 ;;
    --go-cache-home)
      [[ $# -ge 2 ]] || { echo "$1 requires a value" >&2; exit 2; }
      GO_CACHE_HOME="$2"; shift 2 ;;
    --no-open) NO_OPEN=1; shift ;;
    --open) NO_OPEN=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (use --help)" >&2; exit 2 ;;
  esac
done

OS_NAME="$(uname -s)"
case "${OS_NAME}" in
  Darwin)
    : "${BASE_ROOT:=/private/tmp/hyprial-nightly-gui}"
    DEFAULT_GO_CACHE="${ORIGINAL_HOME}/Library/Caches/go-build"
    DEFAULT_BREW_BIN="/opt/homebrew/bin"
    [[ -d /usr/local/bin ]] && DEFAULT_BREW_BIN="${DEFAULT_BREW_BIN}:/usr/local/bin"
    ;;
  Linux)
    : "${BASE_ROOT:=${TMPDIR:-/tmp}/hyprial-nightly-gui}"
    DEFAULT_GO_CACHE="${ORIGINAL_HOME}/.cache/go-build"
    DEFAULT_BREW_BIN="${ORIGINAL_HOME}/.linuxbrew/bin"
    ;;
  *)
    echo "unsupported host OS: ${OS_NAME}; this launcher supports macOS and Linux" >&2
    exit 2
    ;;
esac

# Rustup is normally installed in ~/.cargo/bin on Linux and via the keg-only
# Homebrew formula on macOS.  Put both standard locations on PATH before the
# native preflight so the canonical build helper can select Rust 1.93.1
# without requiring the caller to export a toolchain-specific variable.
RUSTUP_PATHS=("${ORIGINAL_HOME}/.cargo/bin")
if [[ "${OS_NAME}" == "Darwin" ]]; then
  RUSTUP_PATHS+=(/opt/homebrew/opt/rustup/bin /usr/local/opt/rustup/bin)
fi
for rustup_path in "${RUSTUP_PATHS[@]}"; do
  [[ -d "${rustup_path}" ]] && export PATH="${rustup_path}:${PATH}"
done

for required in git uv node npm go cargo rustc; do
  command -v "${required}" >/dev/null || {
    echo "missing required executable: ${required}" >&2
    exit 2
  }
done

export UV_CACHE_DIR="${UV_CACHE_DIR:-$(uv cache dir)}"
if [[ ! -d "${GO_CACHE_HOME}/go/pkg/mod" ]]; then
  GO_CACHE_HOME="${ORIGINAL_HOME}"
fi
export GOMODCACHE="${GOMODCACHE:-${GO_CACHE_HOME}/go/pkg/mod}"
export GOCACHE="${GOCACHE:-${DEFAULT_GO_CACHE}}"
# Use the public module proxy by default so a fresh isolated run can build the
# native sidecars. Set GOPROXY=off explicitly when a complete local cache is
# available and an offline build is required.
export GOPROXY="${GOPROXY:-https://proxy.golang.org,direct}"
export GOTOOLCHAIN="${GOTOOLCHAIN:-local}"
# A source checkout has no wheel-provided native actor engine.  Always opt in
# to the repository's canonical Rust build hook so a zero-argument launch is a
# real native daemon run rather than an editable environment that fails later
# on its first actor operation.  The hook enforces the repository's pinned
# Rust toolchain and fails closed when it is unavailable.
export HYPRIAL_BUILD_NATIVE=1
# Keep the runtime root short: macOS AF_UNIX paths are limited to 103 bytes,
# and daemon.sock sits below this directory plus the generated run id.
CURRENT_FILE="${BASE_ROOT}/current"

cd "${ROOT}"

CURRENT_BRANCH="$(git branch --show-current)"
git fetch origin dev
if [[ "${CURRENT_BRANCH}" == "dev" ]]; then
  git pull --ff-only origin dev
else
  # The launcher may live on a short-lived ops branch while it is being
  # reviewed.  Refuse stale branches, but do not require the launcher commit
  # itself to be merged before it can launch the current dev base.
  git merge-base --is-ancestor origin/dev HEAD || {
    echo "checkout is behind origin/dev; update it before launching" >&2
    exit 2
  }
fi
dirty="$(git status --porcelain | grep -vE '^\?\? scripts/build/run-gui-dev-isolated\.sh$' || true)"
if [[ -n "${dirty}" ]]; then
  echo "working tree is dirty; refusing to update dev" >&2
  exit 2
fi

# A clean checkout may not have a virtual environment yet.  Prepare the
# locked Python entry point before any `uv run --no-sync` preflight below.
uv sync --extra test --locked

install -d -m 700 "${BASE_ROOT}"

stop_tree() {
  local pid="$1" child
  for child in $(pgrep -P "${pid}" 2>/dev/null || true); do
    stop_tree "${child}"
  done
  kill "${pid}" 2>/dev/null || true
}

remove_tree() {
  uv run --no-project --python 3.12 python - "$1" <<'PY'
from pathlib import Path
import shutil
import sys

path = Path(sys.argv[1])
if path.exists():
    shutil.rmtree(path)
PY
}

if [[ -f "${CURRENT_FILE}" ]]; then
  OLD_ROOT="$(cat "${CURRENT_FILE}")"
  case "${OLD_ROOT}" in
    "${BASE_ROOT}"/run.*) ;;
    *) echo "refusing to clean an out-of-scope GUI root: ${OLD_ROOT}" >&2; exit 2 ;;
  esac
  [[ -d "${OLD_ROOT}" && ! -L "${OLD_ROOT}" ]] || { echo "refusing to clean an unsafe GUI root" >&2; exit 2; }
  if [[ -f "${OLD_ROOT}/gui.pid" ]]; then
    OLD_PID="$(cat "${OLD_ROOT}/gui.pid")"
    if kill -0 "${OLD_PID}" 2>/dev/null; then
      OLD_CMD="$(ps -p "${OLD_PID}" -o command= 2>/dev/null || true)"
      # Older launcher revisions recorded their wrapper PID, whose command
      # may be a temporary absolute path rather than this script's basename.
      # Accept that PID only when its process tree still contains this run
      # root; otherwise refuse to signal an unrelated process.
      OLD_TREE_MATCH="$(ps -axo pid=,command= 2>/dev/null | grep -F "${OLD_ROOT}" | head -1 || true)"
      [[ "${OLD_CMD}" == *"run-gui-dev-isolated"* || -n "${OLD_TREE_MATCH}" ]] || {
        echo "refusing to signal unrelated PID ${OLD_PID}" >&2
        exit 2
      }
      stop_tree "${OLD_PID}"
      sleep 0.5
      kill -9 "${OLD_PID}" 2>/dev/null || true
    fi
  fi
  remove_tree "${OLD_ROOT}"
  rm -f "${CURRENT_FILE}"
fi

RUN_ROOT="$(mktemp -d "${BASE_ROOT}/run.XXXXXX")"
install -d -m 700 "${RUN_ROOT}/home" "${RUN_ROOT}/state" "${RUN_ROOT}/os-home" \
  "${RUN_ROOT}/xdg-config" "${RUN_ROOT}/xdg-cache" "${RUN_ROOT}/xdg-data" \
  "${RUN_ROOT}/xdg-state" "${RUN_ROOT}/runtime" "${RUN_ROOT}/bin" \
  "${RUN_ROOT}/tmp"

directory_mode() {
  case "${OS_NAME}" in
    Darwin) stat -f '%Lp' "$1" ;;
    Linux) stat -c '%a' "$1" ;;
  esac
}

for private_dir in \
  "${RUN_ROOT}" "${RUN_ROOT}/home" "${RUN_ROOT}/state" "${RUN_ROOT}/os-home" \
  "${RUN_ROOT}/xdg-config" "${RUN_ROOT}/xdg-cache" "${RUN_ROOT}/xdg-data" \
  "${RUN_ROOT}/xdg-state" "${RUN_ROOT}/runtime" "${RUN_ROOT}/bin" "${RUN_ROOT}/tmp"; do
  [[ -d "${private_dir}" && ! -L "${private_dir}" ]] || {
    echo "refusing an unsafe isolated directory: ${private_dir}" >&2
    exit 2
  }
  private_mode="$(directory_mode "${private_dir}")"
  [[ "${private_mode}" == "700" ]] || {
    echo "isolated directory is not mode 700: ${private_dir} (${private_mode})" >&2
    exit 2
  }
done
printf '%s\n' "${RUN_ROOT}" > "${CURRENT_FILE}"
printf '%s\n' "$$" > "${RUN_ROOT}/gui.pid"

cleanup() {
  if [[ -n "${GUI_PID:-}" ]] && kill -0 "${GUI_PID}" 2>/dev/null; then
    stop_tree "${GUI_PID}"
    wait "${GUI_PID}" 2>/dev/null || true
  fi
  if [[ -n "${LABRELAY_PID:-}" ]] && kill -0 "${LABRELAY_PID}" 2>/dev/null; then
    kill "${LABRELAY_PID}" 2>/dev/null || true
    wait "${LABRELAY_PID}" 2>/dev/null || true
  fi
  exec 9>&- 2>/dev/null || true
  rm -f "${RUN_ROOT}/gui.pid" "${CURRENT_FILE}"
  remove_tree "${RUN_ROOT}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Remove inherited runtime selectors, endpoints and provider credentials before
# injecting only the values owned by this isolated instance.
for var in $(compgen -e); do
  case "${var}" in
    HYPRIAL_*|HARNESS_*|XDG_*|*_API_KEY|*_API_TOKEN|*_TOKEN|*_SECRET)
      unset "${var}"
      ;;
  esac
done

LOCAL_BIN="${HYPRIAL_GUI_LOCAL_BIN:-${ORIGINAL_HOME}/.local/bin}"
BREW_BIN="${HYPRIAL_GUI_BREW_BIN:-${DEFAULT_BREW_BIN}}"
export PATH="${LOCAL_BIN}:${BREW_BIN}:${PATH}"
export HOME="${RUN_ROOT}/os-home"
export HYPRIAL_HOME="${RUN_ROOT}/home"
export HARNESS_STATE_DIR="${RUN_ROOT}/state"
export XDG_CONFIG_HOME="${RUN_ROOT}/xdg-config"
export XDG_CACHE_HOME="${RUN_ROOT}/xdg-cache"
export XDG_DATA_HOME="${RUN_ROOT}/xdg-data"
export XDG_STATE_HOME="${RUN_ROOT}/xdg-state"
export XDG_RUNTIME_DIR="${RUN_ROOT}/runtime"
# Authorization workers use tempfile.TemporaryDirectory for their private
# result channel. Keep it under the platform's real temporary filesystem and
# do not inherit the host temp location into the isolated daemon or children.
export TMPDIR="${RUN_ROOT}/tmp"
export TMP="${TMPDIR}"
export TEMP="${TMPDIR}"
export HYPRIAL_GUI_BACKEND_DRIVER=daemon
export HYPRIAL_PEER_DISCOVERY=0
export HYPRIAL_ZENOH_CONNECT=
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="${NO_PROXY}"

# New isolated homes need the canonical node identity before the GUI admission
# check can attach to the daemon. Mint only into this fresh home; never copy a
# host node id or any login/device material into the disposable environment.
if [[ ! -s "${HYPRIAL_HOME}/node-id" ]]; then
  uv run --no-sync python - "${HYPRIAL_HOME}" <<'PY'
from pathlib import Path
import sys
from hyprial.kernel.impl.primitives.node_identity import mint_node_id

home = Path(sys.argv[1])
home.mkdir(parents=True, exist_ok=True)
mint_node_id(home)
PY
fi

case "${NETWORK_MODE}" in
  isolated)
    # This mode is for local UI-only work.  It deliberately disables the
    # forwarding sidecar, so org invite/join is unavailable by design.
    export HYPRIAL_NETWORK_ISOLATED=1
    export HYPRIAL_ZENOH_LISTEN=
    ;;
  labrelay)
    # Keep the daemon, state, device material and forwarding backend private,
    # while retaining the full org/Tailcat path through an owned loopback DERP.
    unset HYPRIAL_NETWORK_ISOLATED
    # Let the daemon start in bootstrap before login provisions this isolated
    # home's device key; auto enables forwarding as soon as that key exists.
    export HYPRIAL_FORWARDING=auto
    install -d -m 700 "${RUN_ROOT}/home/net"
    export HYPRIAL_ZENOH_LISTEN="unixsock-stream/${RUN_ROOT}/home/net/zenoh.sock"
    ;;
  networked)
    # Keep the home/state/socket private, but use the real Tailcat forwarding
    # path so this disposable device can reach an existing organization.  No
    # host daemon or host state is reused; login/device provisioning happens in
    # this run's isolated home.
    unset HYPRIAL_NETWORK_ISOLATED
    export HYPRIAL_FORWARDING=auto
    export HYPRIAL_USAGE_DISABLE=1
    export HYPRIAL_ZENOH_GOSSIP=0
    install -d -m 700 "${RUN_ROOT}/home/net"
    export HYPRIAL_ZENOH_LISTEN="unixsock-stream/${RUN_ROOT}/home/net/zenoh.sock"
    ;;
  *)
    echo "HYPRIAL_GUI_NETWORK_MODE must be isolated, labrelay, or networked" >&2
    exit 2
    ;;
esac

git fetch origin dev
if [[ "${CURRENT_BRANCH}" == "dev" ]]; then
  git pull --ff-only origin dev
else
  git merge-base --is-ancestor origin/dev HEAD || {
    echo "checkout is behind origin/dev; update it before launching" >&2
    exit 2
  }
fi

echo "native_core=$(uv run --no-sync python -c 'import hyprial._core.actors as m; print(f"{m.__file__} abi={m.ENGINE_ABI_VERSION}")')" >&2

NODE_BIN="$(command -v node)"
NODE_VERSION="$("${NODE_BIN}" --version)"
NODE_MAJOR="${NODE_VERSION#v}"
NODE_MAJOR="${NODE_MAJOR%%.*}"
if (( NODE_MAJOR < 24 )); then
  echo "Node 24+ is required; found ${NODE_VERSION}" >&2
  exit 2
fi
npm --prefix gui ci --ignore-scripts --no-audit --no-fund
npm --prefix gui run build

TAILCAT="${RUN_ROOT}/bin/hyprial-tailcat"
(
  cd "${ROOT}/sidecar/hyprial-tailcat"
  go build -mod=readonly -trimpath -o "${TAILCAT}" ./cmd/hyprial-tailcat
)
chmod 700 "${TAILCAT}"
export HYPRIAL_TAILCAT_BINARY="${TAILCAT}"
if ! TAILCAT_VERSION="$("${TAILCAT}" version 2>&1)"; then
  echo "Tailcat version probe failed: ${TAILCAT_VERSION}" >&2
  exit 2
fi
if [[ "${TAILCAT_VERSION}" != *'"v":6'* ]]; then
  echo "unexpected Tailcat protocol version: ${TAILCAT_VERSION}" >&2
  exit 2
fi

if [[ "${NETWORK_MODE}" == "labrelay" ]]; then
  LABRELAY="${RUN_ROOT}/bin/hyprial-tailcat-labrelay"
  (
    cd "${ROOT}/sidecar/hyprial-tailcat"
    go build -mod=readonly -trimpath -o "${LABRELAY}" ./cmd/hyprial-tailcat-labrelay
  )
  chmod 700 "${LABRELAY}"
  LABRELAY_STDOUT="${RUN_ROOT}/labrelay.stdout"
  LABRELAY_STDERR="${RUN_ROOT}/labrelay.stderr"
  # Keep stdin open: labrelay intentionally serves until stdin closes, and a
  # background process launched from a non-interactive shell otherwise exits
  # immediately when its inherited stdin is closed.
  mkfifo "${RUN_ROOT}/labrelay.stdin"
  exec 9<>"${RUN_ROOT}/labrelay.stdin"
  "${LABRELAY}" <&9 >"${LABRELAY_STDOUT}" 2>"${LABRELAY_STDERR}" &
  LABRELAY_PID=$!
  export LABRELAY_PID
  for _ in $(seq 1 150); do
    [[ -s "${LABRELAY_STDOUT}" ]] && break
    kill -0 "${LABRELAY_PID}" 2>/dev/null || {
      echo "Tailcat labrelay exited before readiness" >&2
      cat "${LABRELAY_STDERR}" >&2 || true
      exit 2
    }
    sleep 0.1
  done
  DERP_JSON="$(uv run --no-project --python 3.12 python - "${LABRELAY_STDOUT}" <<'PY'
import json
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        value = json.loads(line)
    except ValueError:
        continue
    derp = value.get("derp") if isinstance(value, dict) else None
    if isinstance(derp, dict) and all(isinstance(derp.get(k), (str, int)) for k in ("host", "derpPort", "stunPort")):
        print(json.dumps({**derp, "insecureForTests": True}, separators=(",", ":")))
        raise SystemExit(0)
raise SystemExit("labrelay did not publish a DERP configuration")
PY
)"
  export HYPRIAL_TAILCAT_DERP="${DERP_JSON}"
fi

for tool in uv node npm claude codex pi; do
  command -v "${tool}" >/dev/null || { echo "missing executable: ${tool}" >&2; exit 2; }
done

echo "source_commit=$(git rev-parse HEAD)" >&2
echo "isolated_root=${RUN_ROOT}" >&2
echo "gui_home=${ROOT}/gui" >&2
echo "network_mode=${NETWORK_MODE}" >&2
echo "node=${NODE_VERSION}" >&2
echo "tailcat=${TAILCAT_VERSION}" >&2
echo "PATH tools: claude=$(command -v claude) codex=$(command -v codex) pi=$(command -v pi)" >&2

GUI_ARGS=(dev --gui-home "${ROOT}/gui" --port "${REQUESTED_PORT}" --json)
if [[ "${NO_OPEN}" == "1" ]]; then
  GUI_ARGS+=(--no-open)
fi
uv run --no-sync hyprial gui "${GUI_ARGS[@]}" &
GUI_PID=$!
wait "${GUI_PID}"
