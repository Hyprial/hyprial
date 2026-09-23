#!/usr/bin/env bash

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/hyprial-env.sh"

# Shared runtime bootstrap for install-local.sh and start-web.sh.
# Sourcing this file does not change the machine; installation only happens
# when h2b_gui_install_node is called explicitly.

H2B_GUI_NODE_VERSION="${H2B_GUI_NODE_VERSION:-24.20.0}"
H2B_GUI_NODE_OFFICIAL="https://nodejs.org/dist"
H2B_GUI_NODE_MIRROR="${H2B_GUI_NODE_MIRROR:-https://npmmirror.com/mirrors/node}"
H2B_GUI_NPM_OFFICIAL="https://registry.npmjs.org"
H2B_GUI_NPM_MIRROR="${H2B_GUI_NPM_MIRROR:-https://registry.npmmirror.com}"
H2B_GUI_RUNTIME_ROOT="${H2B_GUI_RUNTIME_DIR:-${H2B_HOME:-$HOME/.h2b}/apps/gui/runtime}"
H2B_GUI_MANAGED_NODE="$H2B_GUI_RUNTIME_ROOT/node"
H2B_GUI_MANAGED_DSH="$H2B_GUI_RUNTIME_ROOT/dsh"

# The GUI owns its runtime independently of global dsh wrappers. Activation is
# offline: explicit install/upgrade resolves npm latest and verifies a fresh candidate.
h2b_gui_activate_dsh() {
  if [[ -x "$H2B_GUI_MANAGED_DSH/node_modules/.bin/dsh" ]]; then
    export PATH="$H2B_GUI_MANAGED_DSH/node_modules/.bin:$PATH"
    hash -r
  fi
  command -v dsh >/dev/null 2>&1
}

h2b_gui_release_dsh_version() {
  node "$1/../../scripts/dsh-runtime.mjs" resolve
}

h2b_gui_prepare_dsh_release() {
  local release_directory="$1"
  [[ "$H2B_GUI_RUNTIME_ROOT" = /* ]] || return 2
  H2B_GUI_DSH_VERSION="$(h2b_gui_release_dsh_version "$release_directory")" || return 1
  [[ "$H2B_GUI_DSH_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]] || return 1
  mkdir -p "$H2B_GUI_RUNTIME_ROOT/dsh-releases" || return 1
  H2B_GUI_CANDIDATE_DSH="$(mktemp -d "$H2B_GUI_RUNTIME_ROOT/dsh-releases/$H2B_GUI_DSH_VERSION.XXXXXX")" || return 1
  node "$release_directory/../../scripts/dsh-runtime.mjs" prepare "$release_directory" "$H2B_GUI_CANDIDATE_DSH" "$H2B_GUI_DSH_VERSION" || return 1
  printf 'Installing npm latest DSH %s with this candidate dependency lock...\n' "$H2B_GUI_DSH_VERSION"
  local args=(ci --prefix "$H2B_GUI_CANDIDATE_DSH" --no-audit --no-fund)
  # Mirrors can serve the locked artifacts, never choose a different release.
  case "${H2B_INSTALL_MIRROR:-auto}" in
    cn) npm "${args[@]}" --registry="$H2B_GUI_NPM_MIRROR" || return 1 ;;
    official) npm "${args[@]}" --registry="$H2B_GUI_NPM_OFFICIAL" || return 1 ;;
    auto) npm "${args[@]}" --registry="$H2B_GUI_NPM_OFFICIAL" || {
      printf 'DSH install failed; retrying through %s for the same version...\n' "$H2B_GUI_NPM_MIRROR" >&2
      npm "${args[@]}" --registry="$H2B_GUI_NPM_MIRROR" || return 1
    } ;;
    *) return 2 ;;
  esac
  [[ "$("$H2B_GUI_CANDIDATE_DSH/node_modules/.bin/dsh" --version)" == "$H2B_GUI_DSH_VERSION" ]] || return 1
  export PATH="$H2B_GUI_CANDIDATE_DSH/node_modules/.bin:$PATH"
  hash -r
}

h2b_gui_promote_dsh() {
  [[ "$("$H2B_GUI_CANDIDATE_DSH/node_modules/.bin/dsh" --version)" == "$H2B_GUI_DSH_VERSION" ]] || return 1
  node - "$H2B_GUI_CANDIDATE_DSH" "$H2B_GUI_MANAGED_DSH" <<'JS'
const fs = require('node:fs');
const [candidate, target] = process.argv.slice(2);
if (fs.existsSync(target) && !fs.lstatSync(target).isSymbolicLink()) throw new Error('Managed DSH target must be a symlink');
const temporary = target + '.next-' + process.pid;
try { fs.symlinkSync(candidate, temporary, 'dir'); fs.renameSync(temporary, target); }
finally { try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; } }
JS
}

h2b_gui_node_works() {
  command -v node >/dev/null 2>&1 || return 1
  command -v npm >/dev/null 2>&1 || return 1
  local version major minor
  version="$(node -p 'process.versions.node' 2>/dev/null)" || return 1
  IFS=. read -r major minor _ <<<"$version"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
  ((major >= 24 || (major == 22 && minor >= 19)))
}

h2b_gui_activate_node() {
  if [[ -x "$H2B_GUI_MANAGED_NODE/bin/node" && -x "$H2B_GUI_MANAGED_NODE/bin/npm" ]]; then
    local previous_path="$PATH"
    export PATH="$H2B_GUI_MANAGED_NODE/bin:$PATH"
    if h2b_gui_node_works; then
      return 0
    fi
    export PATH="$previous_path"
  fi
  if h2b_gui_node_works; then
    return 0
  fi
  return 1
}

h2b_gui_download() {
  local url="$1" destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 1 --connect-timeout 8 --max-time 300 \
      --speed-limit 10240 --speed-time 15 -o "$destination" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget --timeout=20 --tries=2 -O "$destination" "$url"
  else
    return 127
  fi
}

h2b_gui_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    return 127
  fi
}

h2b_gui_node_sources() {
  if [[ -n "${H2B_GUI_NODE_DIST_URL:-}" ]]; then
    printf '%s\n' "${H2B_GUI_NODE_DIST_URL%/}"
    return
  fi
  case "${H2B_INSTALL_MIRROR:-auto}" in
    cn) printf '%s\n' "$H2B_GUI_NODE_MIRROR" ;;
    official) printf '%s\n' "$H2B_GUI_NODE_OFFICIAL" ;;
    auto) printf '%s\n' "$H2B_GUI_NODE_OFFICIAL" "$H2B_GUI_NODE_MIRROR" ;;
    *) return 2 ;;
  esac
}

h2b_gui_node_expected_sha256() {
  if [[ -n "${H2B_GUI_NODE_SHA256:-}" ]]; then
    printf '%s\n' "$H2B_GUI_NODE_SHA256"
    return
  fi
  case "$1" in
    node-v24.20.0-linux-x64.tar.xz) printf '%s\n' 2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2 ;;
    node-v24.20.0-linux-arm64.tar.xz) printf '%s\n' 5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7 ;;
    node-v24.20.0-darwin-x64.tar.gz) printf '%s\n' 9e5b2644cf107befb6aefca676b96d3296bc10138096f022ed378d6233ed81f4 ;;
    node-v24.20.0-darwin-arm64.tar.gz) printf '%s\n' 40e5607e5ecb3db9192723776da2d75d966260fc74a7a9e731c1bd67dda96bc8 ;;
    *) return 1 ;;
  esac
}

h2b_gui_install_node() {
  [[ "$H2B_GUI_NODE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 2
  [[ "$H2B_GUI_RUNTIME_ROOT" = /* ]] || return 2
  [[ ! -e "$H2B_GUI_MANAGED_NODE" ]] || return 3

  local os arch platform extension filename stage base expected actual extracted
  case "$(uname -s)" in
    Linux) os=linux; extension=tar.xz ;;
    Darwin) os=darwin; extension=tar.gz ;;
    *) return 4 ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch=x64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) return 4 ;;
  esac
  platform="$os-$arch"
  filename="node-v${H2B_GUI_NODE_VERSION}-${platform}.${extension}"
  expected="$(h2b_gui_node_expected_sha256 "$filename")" || return 5
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || return 5

  mkdir -p "$H2B_GUI_RUNTIME_ROOT"
  chmod 700 "$H2B_GUI_RUNTIME_ROOT"
  stage="$(mktemp -d "$H2B_GUI_RUNTIME_ROOT/.node-install.XXXXXX")" || return 1
  local archive="$stage/$filename" installed=0

  while IFS= read -r base; do
    [[ -n "$base" ]] || continue
    printf 'Downloading Node.js v%s from %s...\n' "$H2B_GUI_NODE_VERSION" "$base"
    if h2b_gui_download "$base/v${H2B_GUI_NODE_VERSION}/$filename" "$archive"; then
      actual="$(h2b_gui_sha256 "$archive" 2>/dev/null || true)"
      if [[ "$actual" == "$expected" ]]; then
        installed=1
        break
      fi
      printf 'Node.js checksum verification failed for %s; trying the next source.\n' "$base" >&2
    else
      printf 'Node.js download failed from %s; trying the next source.\n' "$base" >&2
    fi
    rm -f -- "$archive"
  done < <(h2b_gui_node_sources)

  if ((installed == 0)); then
    rm -rf -- "$stage"
    return 1
  fi
  tar -xf "$archive" -C "$stage" || {
    rm -rf -- "$stage"
    return 1
  }
  extracted="$stage/node-v${H2B_GUI_NODE_VERSION}-${platform}"
  if [[ ! -x "$extracted/bin/node" || ! -x "$extracted/bin/npm" ]]; then
    rm -rf -- "$stage"
    return 1
  fi
  mv "$extracted" "$H2B_GUI_MANAGED_NODE" || {
    rm -rf -- "$stage"
    return 1
  }
  rm -rf -- "$stage"
  export PATH="$H2B_GUI_MANAGED_NODE/bin:$PATH"
  h2b_gui_node_works
}

h2b_gui_npm_install_global() {
  local package="$1" allow_scripts="${2:-}"
  local args=(install -g "$package")
  if [[ -n "$allow_scripts" ]]; then
    args+=("--allow-scripts=$allow_scripts")
  fi
  case "${H2B_INSTALL_MIRROR:-auto}" in
    cn)
      npm "${args[@]}" --registry="$H2B_GUI_NPM_MIRROR"
      ;;
    official)
      npm "${args[@]}" --registry="$H2B_GUI_NPM_OFFICIAL"
      ;;
    auto)
      npm "${args[@]}" || {
        printf 'npm install failed with the configured registry; retrying through %s...\n' "$H2B_GUI_NPM_MIRROR" >&2
        npm "${args[@]}" --registry="$H2B_GUI_NPM_MIRROR"
      }
      ;;
    *) return 2 ;;
  esac
}
