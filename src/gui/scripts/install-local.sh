#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$repo_root/scripts/runtime.sh"
check_only=0

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
fi
[[ $# -eq 0 ]] || {
  printf 'Usage: %s [--check]\n' "$0" >&2
  exit 2
}

fail() {
  printf 'DSH H2B setup failed: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

repair_profile_pnpm_store() {
  local profile_dir="${DSH_HOME:-$HOME/.dsh}/profiles/web"
  local modules_file="$profile_dir/node_modules/.modules.yaml"
  [[ -f "$modules_file" ]] || return 0

  local profile_major current_major
  profile_major="$(sed -n 's/.*packageManager.*pnpm@\([0-9][0-9]*\).*/\1/p' "$modules_file" | head -n 1)"
  current_major="$(pnpm --version)"
  current_major="${current_major%%.*}"
  [[ "$profile_major" =~ ^[0-9]+$ && "$current_major" =~ ^[0-9]+$ ]] || return 0
  [[ "$profile_major" != "$current_major" ]] || return 0

  printf 'Migrating the DSH web profile from pnpm %s to pnpm %s...\n' \
    "$profile_major" "$current_major"
  local args=(--dir "$profile_dir" install --no-frozen-lockfile)
  case "${H2B_INSTALL_MIRROR:-auto}" in
    cn)
      env CI=true pnpm "${args[@]}" --registry="$H2B_GUI_NPM_MIRROR"
      ;;
    official)
      env CI=true pnpm "${args[@]}" --registry="$H2B_GUI_NPM_OFFICIAL"
      ;;
    auto)
      env CI=true pnpm "${args[@]}" || {
        printf 'pnpm profile migration failed; retrying through %s...\n' \
          "$H2B_GUI_NPM_MIRROR" >&2
        env CI=true pnpm "${args[@]}" --registry="$H2B_GUI_NPM_MIRROR"
      }
      ;;
    *) return 2 ;;
  esac
}

for command_name in hyprial; do
  require_command "$command_name"
done

if ! h2b_gui_activate_node; then
  if ((check_only)); then
    printf 'A supported Node.js runtime is missing; setup will install Node.js v%s under %s.\n' \
      "$H2B_GUI_NODE_VERSION" "$H2B_GUI_MANAGED_NODE"
    printf 'Setup will then install pnpm and DSH as needed. No changes were made.\n'
    exit 0
  fi
  printf 'Installing a managed Node.js runtime...\n'
  h2b_gui_install_node || fail "could not install Node.js; supported platforms are Linux/macOS x64/arm64 and a checksum tool is required"
fi
require_command node
require_command npm

if command -v pnpm >/dev/null 2>&1; then
  pnpm --version >/dev/null 2>&1 || fail "existing pnpm command cannot run"
elif ((check_only)); then
  printf 'pnpm is not installed; setup will run: npm install -g pnpm@latest\n'
else
  printf 'Installing pnpm for DSH profile management...\n'
  h2b_gui_npm_install_global pnpm@latest || fail "npm could not install pnpm; check the npm global prefix, permissions, and registry access"
  hash -r
  require_command pnpm
fi

if ((check_only)); then
  release_dsh="$(h2b_gui_release_dsh_version "$repo_root/packages/dsh-runtime")" || fail "could not read the GUI release DSH version"
  printf 'Setup will install and verify GUI release DSH %s, including when an older dsh already exists.\n' "$release_dsh"
else
  cleanup_dsh_candidate() {
    if [[ -n "${H2B_GUI_CANDIDATE_DSH:-}" && "${dsh_promoted:-0}" != "1" ]]; then
      rm -rf -- "$H2B_GUI_CANDIDATE_DSH"
    fi
  }
  trap cleanup_dsh_candidate EXIT
  h2b_gui_prepare_dsh_release "$repo_root/packages/dsh-runtime" || fail "could not install GUI release DSH; the active runtime was preserved"
fi

node "$repo_root/scripts/gui-source.mjs"

printf 'Verifying the bundled Codex compatibility package...\n'
node "$repo_root/scripts/codex-package.mjs" verify
node "$repo_root/scripts/gui-layout-package.mjs" verify

printf 'Checking H2B installation...\n'
hyprial version --json >/dev/null || fail "hyprial version check failed"

printf 'Installing pinned plugin dependencies...\n'
npm --prefix "$repo_root" ci --ignore-scripts --no-audit --no-fund

printf 'Building the static DSH client...\n'
npm --prefix "$repo_root" run build:static

node "$repo_root/scripts/gui-source.mjs"

printf 'Running the isolated repository test suite...\n'
npm --prefix "$repo_root" test
node "$repo_root/scripts/gui-source.mjs"

if ((check_only)); then
  printf 'DSH H2B setup preflight passed; Dashboard dependency installation/build and plugin registration (H2B Talk, GUI layout and dsh-codex) were skipped.\n'
  exit 0
fi

printf 'Installing and building the independent Dashboard...\n'
npm --prefix "$repo_root/dashboard" ci --no-audit --no-fund
npm --prefix "$repo_root" run build:dashboard

printf 'Testing the released DSH with the shipped GUI in an isolated browser profile...\n'
node "$repo_root/scripts/ci-browser-install.mjs" || fail "could not prepare Chromium for the required DSH compatibility check"
DSH_LATEST_ARTIFACTS="$H2B_GUI_RUNTIME_ROOT/dsh-check" \
  node "$repo_root/scripts/verify-dsh-latest.mjs" --runtime "$H2B_GUI_CANDIDATE_DSH" --release \
  || fail "GUI failed its released DSH compatibility check; the active DSH runtime was preserved"

node "$repo_root/scripts/gui-source.mjs"

printf 'Registering the static plugin in the DSH web profile...\n'
repair_profile_pnpm_store || fail "could not migrate the DSH web profile to the active pnpm store"
node "$repo_root/scripts/codex-package.mjs" install
node "$repo_root/scripts/gui-layout-package.mjs" install
dsh plugin --profile web add --ignore-scripts "$repo_root"

# Older installs inserted h2b-talk directly in the user patch. The bundle now
# owns that row; migrate only its legacy insertion, with a recoverable backup.
node "$repo_root/scripts/gui-layout-package.mjs" migrate-profile \
  || fail "could not migrate the legacy GUI profile insertion; original user configuration was preserved"

# Do not print the composed configuration: it can include private user settings.
printf 'Validating the DSH web profile configuration...\n'
dsh --profile web --dump-config | node "$repo_root/scripts/gui-layout-package.mjs" verify-profile \
  || fail "the DSH web profile configuration could not load exactly one GUI layout provider after plugin registration"

h2b_gui_promote_dsh || fail "could not activate the verified GUI release DSH runtime"
dsh_promoted=1
printf 'GUI now uses managed DSH %s.\n' "$H2B_GUI_DSH_VERSION"

cat <<EOF

DSH H2B Talk, GUI layout and dsh-codex are installed for the web profile.
In DSH, open Settings -> OpenAI Codex to sign in with ChatGPT if needed.
Existing login, proxy, and saved model settings are preserved; setup does not start OAuth.

Start it with:
  hyprial gui                        # Dashboard
  hyprial gui dsh                    # DSH developer workspace
  hyprial gui all                    # Both independent applications

Optional machine-specific settings:
  cp .env.example .env.local
EOF
