#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/hyprial-env.sh"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "${H2B_GUI_APP:-dsh}" != dsh ]]; then
  printf 'Dashboard is retired; use hyprial gui [start|status|stop|upgrade].\n' >&2
  exit 2
fi
exec bash "$repo_root/scripts/start-web.sh" "$@"
