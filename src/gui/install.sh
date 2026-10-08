#!/usr/bin/env bash
# Installer entry named by hyprial-install.json. The product composition root
# has no hidden defaults: this only installs locked dependencies, runs the
# architecture check and builds the assets. It never reads user credentials,
# npm configuration or any daemon state; the caller's environment owns the
# transport/principal configuration the started server will require.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$node_major" -lt 24 ]; then
  echo "hyprial gui install: Node >= 24 is required (found $(node -p process.versions.node))" >&2
  exit 1
fi

npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm run build
