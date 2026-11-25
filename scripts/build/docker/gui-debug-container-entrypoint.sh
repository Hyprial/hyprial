#!/usr/bin/env bash
# Decision (Z-7 A3): this entrypoint does not start or await a daemon; `hyprial gui`
# ensures the one daemon itself. The container keeps its fabricated settings owner,
# the ingress proxy and loopback-only GUI binding.
set -euo pipefail

mode="${1:-normal}"
case "$mode" in
  normal|dev) ;;
  *) printf 'MODE must be normal or dev\n' >&2; exit 2 ;;
esac

source_root="${HYPRIAL_GUI_SOURCE_ROOT:-/opt/hyprial/source}"
runtime_root="${HYPRIAL_GUI_RUNTIME_ROOT:-/runtime}"
ingress_port="${HYPRIAL_GUI_INGRESS_PORT:-4174}"
app_port="${HYPRIAL_GUI_APP_PORT:-4173}"
public_port="${HYPRIAL_GUI_PUBLIC_PORT:?public port is required}"
python_bin="$source_root/.venv/bin/python"
cli_bin="$source_root/.venv/bin/hyprial"
proxy_script="$source_root/scripts/build/docker/gui-debug-proxy.py"

umask 077
export HYPRIAL_GUI_RUNTIME_ROOT="$runtime_root"
export HYPRIAL_HOME="$runtime_root/hyprial-home"
export HARNESS_STATE_DIR="$runtime_root/state"
export XDG_CONFIG_HOME="$runtime_root/xdg-config"
export XDG_STATE_HOME="$runtime_root/xdg-state"
export XDG_CACHE_HOME="$runtime_root/xdg-cache"
export XDG_DATA_HOME="$runtime_root/xdg-data"
export XDG_RUNTIME_DIR="$runtime_root/xdg-runtime"
export HYPRIAL_NODE_ID=gui-docker-test-node
export HYPRIAL_NETWORK_ISOLATED=1
export HYPRIAL_ZENOH_LISTEN=
export HYPRIAL_ZENOH_CONNECT=
export HYPRIAL_PEER_DISCOVERY=0
export HYPRIAL_GUI_BACKEND_DRIVER=daemon
export npm_config_cache="$runtime_root/npm-cache"
export NO_PROXY=127.0.0.1,localhost,::1
export no_proxy="$NO_PROXY"
mkdir -p "$HYPRIAL_HOME" "$HARNESS_STATE_DIR" "$XDG_CONFIG_HOME" \
  "$XDG_STATE_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" \
  "$XDG_RUNTIME_DIR" "$npm_config_cache"
printf '%s\n' '{"version":1,"owner":"gui-docker-test-owner"}' \
  > "$HYPRIAL_HOME/settings.json"

gui_pid=""
proxy_pid=""
cleanup() {
  if [[ -n "$proxy_pid" ]]; then kill "$proxy_pid" 2>/dev/null || true; fi
  if [[ -n "$gui_pid" ]]; then kill "$gui_pid" 2>/dev/null || true; fi
  if [[ -n "$proxy_pid" ]]; then wait "$proxy_pid" 2>/dev/null || true; fi
  if [[ -n "$gui_pid" ]]; then wait "$gui_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

gui_json="$runtime_root/gui.json"
gui_log="$runtime_root/gui.log"
if [[ "$mode" == normal ]]; then
  (cd "$source_root" && exec "$cli_bin" gui --no-open --json --port "$app_port" \
    > "$gui_json" 2> "$gui_log") &
else
  (cd "$source_root" && exec "$cli_bin" gui dev --gui-home "$source_root/gui" \
    --no-open --json --port "$app_port" > "$gui_json" 2> "$gui_log") &
fi
gui_pid=$!
for (( attempt=0; attempt<1200; attempt++ )); do
  if [[ -s "$gui_json" ]] && grep -q '"handoff_path"' "$gui_json"; then break; fi
  if ! kill -0 "$gui_pid" 2>/dev/null; then
    "$python_bin" -c 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); data=json.loads(p.read_text()) if p.exists() and p.stat().st_size else {}; print("GUI container launcher failed:", data.get("code", "unknown"), file=sys.stderr)' "$gui_json"
    exit 1
  fi
  sleep 0.1
done
if (( attempt == 1200 )); then
  printf 'GUI container launcher timed out\n' >&2
  exit 1
fi
private_handoff="$("$python_bin" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["handoff_path"])' "$gui_json")"
test -f "$private_handoff"

public_origin="http://127.0.0.1:$public_port"
"$python_bin" "$proxy_script" serve --listen-host 0.0.0.0 \
  --port "$ingress_port" --public-origin "$public_origin" \
  --static-origin "http://127.0.0.1:$app_port" --session-origin "http://127.0.0.1:$app_port" \
  > "$runtime_root/proxy.log" 2>&1 &
proxy_pid=$!
for (( attempt=0; attempt<100; attempt++ )); do
  if "$python_bin" -c 'import sys,urllib.request; r=urllib.request.Request(sys.argv[1], headers={"Host":sys.argv[2]}); assert urllib.request.urlopen(r, timeout=1).status == 200' \
    "http://127.0.0.1:$ingress_port/__gui/healthz" "127.0.0.1:$public_port" \
    >/dev/null 2>&1; then break; fi
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    printf 'GUI container ingress exited before readiness\n' >&2
    exit 1
  fi
  sleep 0.1
done
if (( attempt == 100 )); then
  printf 'GUI container ingress did not become healthy\n' >&2
  exit 1
fi
"$python_bin" "$proxy_script" handoff --source "$private_handoff" \
  --target "$runtime_root/public-launch.url" --public-origin "$public_origin"
printf 'GUI_CONTAINER_READY mode=%s\n' "$mode"
wait -n "$gui_pid" "$proxy_pid"
