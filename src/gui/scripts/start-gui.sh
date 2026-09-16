#!/usr/bin/env bash
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/hyprial-env.sh"

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ -n "${H2B_GUI_LAUNCH_INFO_FILE:-}" && -z "${H2B_GUI_APP:-}" ]]; then
  printf 'Managed dual-app GUI launch requires the H2B GUI application lifecycle; update H2B before upgrading the GUI package.\n' >&2
  exit 2
fi
case "${H2B_GUI_APP:-dashboard}" in
  dashboard)
    source "$repo_root/scripts/runtime.sh"
    h2b_gui_activate_node || { printf 'Dashboard requires a supported Node.js runtime; rerun h2b install gui.\n' >&2; exit 1; }
    exec node "$repo_root/dashboard/server.mjs" "$@"
    ;;
  dsh) exec bash "$repo_root/scripts/start-web.sh" "$@" ;;
  *) printf 'H2B_GUI_APP must be dashboard or dsh; use h2b gui all to start both.\n' >&2; exit 2 ;;
esac
