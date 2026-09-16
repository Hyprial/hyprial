#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

[[ $# -eq 0 ]] || {
  printf 'Usage: %s\n' "$0" >&2
  exit 2
}

exec bash "$repo_root/scripts/install-local.sh"
