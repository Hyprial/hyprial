#!/usr/bin/env bash
# Start entry named by hyprial-install.json. Configuration is explicit
# environment only; product/server.mjs fails closed with a named error when the
# transport driver or the trusted principal is missing.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
exec node product/server.mjs
