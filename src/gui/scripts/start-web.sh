#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$repo_root/scripts/runtime.sh"
env_file="${H2B_DSH_ENV_FILE:-$repo_root/.env.local}"
check_only=0

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
fi

fail() {
  printf 'DSH H2B start failed: %s\n' "$*" >&2
  exit 1
}

h2b_gui_activate_node || fail "no supported Node.js runtime found; rerun h2b install gui"
h2b_gui_activate_dsh || fail "no DSH runtime found; rerun h2b install gui"

for command_name in dsh h2b node pnpm; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command not found: $command_name"
done

dsh --version >/dev/null 2>&1 || fail "dsh command cannot run"

if [[ -f "$env_file" ]]; then
  set -a
  # This is an operator-owned shell environment file; do not place credentials
  # in the tracked .env.example template.
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
elif [[ "$env_file" != "$repo_root/.env.local" ]]; then
  fail "H2B_DSH_ENV_FILE does not exist: $env_file"
fi

# Apply new-name settings declared by the operator environment file as well.
source "$repo_root/scripts/hyprial-env.sh"
source "$repo_root/scripts/kanban-env.sh"
h2b_gui_load_kanban_environment

# The launcher may itself run inside Codex/DSH/Claude. Its session identity
# belongs to the parent, not to this multi-session DSH Host. Keep credentials,
# PATH and H2B transport configuration; DSH creates its own execution identity.
unset CODEX_SESSION_ID CODEX_THREAD_ID DSH_SESSION_ID CLAUDE_SESSION_ID CLAUDECODE

# Restrict the inherited network proxy to the isolated Codex transport.
source "$repo_root/scripts/codex-proxy-env.sh"

h2b_state_root="${H2B_HOME:-$HOME/.h2b}"
socket_path="${HARNESS_SOCKET_PATH:-$h2b_state_root/state/daemon.sock}"
[[ "$socket_path" = /* ]] || fail "HARNESS_SOCKET_PATH must be absolute"
[[ -S "$socket_path" ]] || fail "H2B daemon socket is unavailable: $socket_path"
export HARNESS_SOCKET_PATH="$socket_path"

if [[ -n "${H2B_DSH_DEMO_CWD:-}" ]]; then
  [[ "$H2B_DSH_DEMO_CWD" = /* ]] || fail "H2B_DSH_DEMO_CWD must be absolute"
  [[ -d "$H2B_DSH_DEMO_CWD" ]] || fail "H2B_DSH_DEMO_CWD does not exist on this machine: $H2B_DSH_DEMO_CWD"
fi

h2b doctor --json >/dev/null || fail "h2b daemon health check failed"

host="${DSH_H2B_HOST:-127.0.0.1}"
port="${DSH_H2B_PORT:-3080}"
if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
  fail "DSH_H2B_PORT must be between 1 and 65535"
fi

plugin_list_json="$(dsh plugin --profile web list --json)" || fail "could not inspect the DSH web profile"
plugin_state="$(printf '%s' "$plugin_list_json" | node -e '
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  try {
    const profiles = JSON.parse(input);
    const installed = Array.isArray(profiles) && profiles.some((profile) =>
      profile && profile.dependencies && profile.dependencies["@hyprial/dsh-h2b-talk"]
    );
    const layoutInstalled = Array.isArray(profiles) && profiles.some((profile) =>
      profile?.dependencies?.["@hyprial/dsh-gui-layout"]
    );
    process.stdout.write(!layoutInstalled ? "layout-missing" : installed ? "installed" : "absent");
  } catch {
    process.exitCode = 1;
  }
});
')" || fail "DSH returned an invalid web profile plugin list"

[[ "$plugin_state" != "layout-missing" ]] || fail "GUI layout package is missing; run npm run setup:local before starting"

dsh_args=(--profile web)
if [[ "$plugin_state" == "installed" ]]; then
  printf 'Using the H2B Talk plugin already registered in the DSH web profile.\n'
else
  printf 'Using the repository H2B Talk patch for this launch.\n'
  dsh_args+=(--patch "$repo_root/dsh-web.patch.yml")
fi
dsh "${dsh_args[@]}" --dump-config | node "$repo_root/scripts/gui-layout-package.mjs" verify-profile \
  || fail "DSH must load exactly one GUI layout provider; rerun setup or check profile overrides"
dsh_args+=(--host "$host" --port "$port")
if [[ "${DSH_H2B_NO_OPEN:-0}" == "1" ]]; then
  dsh_args+=(--no-open)
fi

if ((check_only)); then
  printf 'DSH H2B start preflight passed for http://%s:%s\n' "$host" "$port"
  exit 0
fi

node "$repo_root/scripts/maintain-session-state.mjs" migrate --apply \
  || fail "session state migration failed; preserve the ledger and resolve its diagnostic before starting"

if [[ -n "${H2B_GUI_LAUNCH_INFO_FILE:-}" ]]; then
  [[ "$H2B_GUI_LAUNCH_INFO_FILE" = /* ]] \
    || fail "H2B_GUI_LAUNCH_INFO_FILE must be absolute"
  launch_info_parent="$(dirname -- "$H2B_GUI_LAUNCH_INFO_FILE")"
  [[ -d "$launch_info_parent" ]] \
    || fail "H2B_GUI_LAUNCH_INFO_FILE parent does not exist: $launch_info_parent"

fi

printf 'Starting DSH H2B Talk at http://%s:%s\n' "$host" "$port"
if [[ -n "${H2B_GUI_LAUNCH_INFO_FILE:-}" ]]; then
  export H2B_GUI_EXPECTED_ORIGIN="http://$host:$port"
  exec node "$repo_root/scripts/launch-dsh.mjs" "${dsh_args[@]}" "$@"
fi
exec dsh "${dsh_args[@]}" "$@"
