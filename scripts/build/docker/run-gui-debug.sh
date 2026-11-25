#!/usr/bin/env bash
set -euo pipefail

mode="${1:-normal}"
port="${2:-4173}"
source_kind="${3:-remote}"
case "$mode" in
  normal|dev) ;;
  *) printf 'MODE must be normal or dev\n' >&2; exit 2 ;;
esac
case "$source_kind" in
  remote|acceptance) ;;
  *) printf 'Source must be remote or acceptance\n' >&2; exit 2 ;;
esac
if [[ ! "$port" =~ ^[0-9]{1,5}$ ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
  printf 'GUI debug port must be 1-65535\n' >&2
  exit 2
fi
port="$((10#$port))"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repository="$(git -C "$script_dir" rev-parse --show-toplevel)"
if ! command -v docker >/dev/null 2>&1 || ! docker version >/dev/null 2>&1; then
  printf 'Docker daemon is required for GUI debug mode\n' >&2
  exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
  printf 'tar is required to stage the committed backend source\n' >&2
  exit 1
fi

umask 077
build_root="$(mktemp -d "${TMPDIR:-/tmp}/hyprial-gui-build.XXXXXX")"
container=""
cleanup() {
  if [[ -n "$container" ]]; then docker rm --force "$container" >/dev/null 2>&1 || true; fi
  rm -rf -- "$build_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

backend_sha="$(git -C "$repository" rev-parse HEAD)"

context="$build_root/context"
mkdir -p "$context/scripts/build/docker"
if [[ "$source_kind" == acceptance ]]; then
  git -C "$repository" archive HEAD pyproject.toml uv.lock VERSION README.md \
    src gui sidecar/hyprial-tailcat | tar -x -C "$context"
  gui_sha="$backend_sha"
  dockerfile="$script_dir/Dockerfile.gui-acceptance"
  image_prefix="hyprial-gui-acceptance"
else
  remote="$(git -C "$repository" remote get-url origin)"
  if [[ "$remote" =~ ^https?://[^/]*@ ]]; then
    printf 'Refusing a remote URL with embedded credentials\n' >&2
    exit 2
  fi
  export GIT_TERMINAL_PROMPT=0
  if [[ "$remote" == git@* || "$remote" == ssh://* ]]; then
    export GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=10'
  fi
  gui_source="$build_root/gui-source"
  git clone --quiet --filter=blob:none --depth=1 --single-branch --branch dev \
    --no-checkout "$remote" "$gui_source"
  git -C "$gui_source" sparse-checkout set --cone gui
  git -C "$gui_source" checkout --quiet dev
  test -f "$gui_source/gui/package-lock.json"
  gui_sha="$(git -C "$gui_source" rev-parse HEAD)"
  test -z "$(git -C "$gui_source" status --porcelain)"
  git -C "$repository" archive HEAD pyproject.toml uv.lock VERSION README.md src \
    | tar -x -C "$context"
  cp -a "$gui_source/gui" "$context/gui"
  dockerfile="$script_dir/Dockerfile.gui-live"
  image_prefix="hyprial-gui-live"
fi
cp "$script_dir/gui-debug-proxy.py" "$context/scripts/build/docker/"
cp "$script_dir/gui-debug-container-entrypoint.sh" "$context/scripts/build/docker/"
chmod 755 "$context/scripts/build/docker/gui-debug-container-entrypoint.sh"

image="${image_prefix}:${backend_sha:0:12}-${gui_sha:0:12}"
docker build -f "$dockerfile" -t "$image" "$context"
container="$(docker run --detach \
  --publish "127.0.0.1:${port}:4174" \
  --tmpfs /runtime:rw,nosuid,size=128m,uid=10001,gid=10001 \
  --cap-drop ALL --security-opt no-new-privileges \
  --label "hyprial.gui.debug.backend=${backend_sha}" \
  --label "hyprial.gui.debug.frontend=${gui_sha}" \
  --env "HYPRIAL_GUI_PUBLIC_PORT=${port}" \
  "$image" "$mode")"

for (( attempt=0; attempt<1200; attempt++ )); do
  container_logs="$(docker logs "$container" 2>&1 || true)"
  if [[ "$container_logs" == *"GUI_CONTAINER_READY mode=$mode"* ]]; then break; fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]]; then
    diagnostic="$(mktemp -d "${TMPDIR:-/tmp}/hyprial-gui-diagnostic.XXXXXX")"
    docker logs "$container" > "$diagnostic/container.log" 2>&1 || true
    for name in gui.json gui.log proxy.log; do
      docker exec "$container" cat "/runtime/$name" > "$diagnostic/$name" 2>/dev/null \
        || rm -f -- "$diagnostic/$name"
    done
    docker exec "$container" cat /runtime/state/daemon-launch.log \
      > "$diagnostic/daemon-launch.log" 2>/dev/null || rm -f -- "$diagnostic/daemon-launch.log"
    chmod 600 "$diagnostic"/* 2>/dev/null || true
    code="$(grep -oE 'GUI_[A-Z0-9_]+' "$diagnostic/container.log" | head -n 1 || true)"
    printf 'GUI debug container failed: %s; private diagnostics: %s\n' \
      "${code:-unknown}" "$diagnostic" >&2
    exit 1
  fi
  sleep 0.1
done
if (( attempt == 1200 )); then
  printf 'GUI debug container did not become ready within 120 seconds\n' >&2
  exit 1
fi

handoff="$build_root/public-launch.url"
docker exec "$container" cat /runtime/public-launch.url > "$handoff"
chmod 600 "$handoff"
IFS= read -r launch_url < "$handoff"
if [[ ! "$launch_url" =~ ^http://127\.0\.0\.1:${port}/.*#grant=[^[:space:]]+$ ]]; then
  printf 'GUI container returned an invalid public handoff URL\n' >&2
  exit 1
fi
unset launch_url
printf 'GUI debug ready: mode=%s backend=%s dev=%s handoff=%s\n' \
  "$mode" "$backend_sha" "$gui_sha" "$handoff"
printf 'Open the one-use URL in the private handoff file within 60 seconds; public origin: http://127.0.0.1:%s\n' "$port"
while [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == true ]]; do
  sleep 1
done
printf 'GUI debug container exited; inspect its logs if the exit was unexpected\n' >&2
exit 1
