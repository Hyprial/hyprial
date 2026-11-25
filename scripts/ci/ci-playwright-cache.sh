#!/usr/bin/env bash
# Provision a persistent pinned-Chromium cache for the gate-gui browser gates.
#
# Decisions (repo policy keeps decision records in this header; no inline
# comments below):
# - Replaces the per-job `npx -y playwright@<lockfile> install --with-deps
#   chromium` cost of gate-gui.yml "Provision pinned Chromium". Contract kept:
#   exit 0 with PLAYWRIGHT_BROWSERS_PATH pointing at a verified browser cache,
#   or exit non-zero and never hand back an unverified one.
# - Cache key = exact playwright-core version read from gui/package-lock.json
#   (never a floating tag) + OS VERSION_CODENAME (browser builds link against
#   distro system libs) + dpkg architecture; any mismatch reruns the installer.
# - persistent=yes only when CI_CACHE_ROOT is provably a directory on a
#   non-root mount, writable, and that mount is not a volatile fs type (same
#   evidence discipline as scripts/ci/ci-uv-cache.sh: an unmounted
#   CI_CACHE_ROOT is a fresh container layer that would silently cache
#   nothing). tmpfs / ramfs / overlay ancestors keep their contents only for
#   the current job, so they are never treated as a cross-job cache: they are
#   reported with their real mount target and fs type and fall back to the
#   job-local <fallback-dir> with an explicit volatile warning. Other fs
#   types keep the standing repo convention that CI_CACHE_ROOT is a dedicated
#   persistent volume; that convention is trusted, remote persistence is NOT
#   measured here. Every non-persistent outcome prints ::warning:: with
#   mount, fstype, writable and a reason; correctness never depends on
#   persistence.
# - A "hit" is only claimed after a real probe: chromium is launched from the
#   versioned PLAYWRIGHT_BROWSERS_PATH (executablePath prefix check blocks
#   silently reusing ~/.cache/ms-playwright), loads a local HTML file and
#   asserts a DOM read. Deliberately no flag skips the probe.
# - Miss or failed probe => the original `npx -y playwright@<version> install
#   --with-deps chromium` runs (no reimplementation), then the same real probe
#   must pass again. Final failure renames the version dir to
#   .invalid-<run-id>-<key> for evidence and exits non-zero.
# - Playwright's --with-deps shells out to apt-get but ignores arbitrary apt
#   state dirs, so a PATH wrapper execs the REAL apt-get with
#   -o Dir::State::lists / -o Dir::Cache::archives injected under the version
#   dir (index + .deb reuse). The wrapper only forwards args and exit codes;
#   it cannot fake success. Root is not required here: CI runs as root in
#   node:24-bookworm, a non-root user hits honest permission failures.
# - Cold installs pull Debian bookworm indexes inside the runner container,
#   where BOTH direct paths are slow in the measured environment: http timed
#   out (25 s for 401164 of 8790396 bytes) and https without a proxy ran at
#   ~11 KB/s (curl --noproxy: 20 s for 229145 bytes), while the same
#   Packages.xz downloaded fully in 2.8 s when curl used the runner's
#   uppercase HTTPS proxy (same node:24-bookworm container). Root cause
#   measured locally: the workflow exports only uppercase HTTP_PROXY /
#   HTTPS_PROXY, which apt ignores (apt-config shell shows empty
#   Acquire::*::Proxy there), so apt downloads directly. Two fixes together:
#   the https same-source rewrite below AND scoped proxy injection for the
#   Debian hosts (next bullet). The script therefore generates a temporary
#   apt sources layout under
#   RUNNER_TEMP (files mode 0600, parts dir 0700; seam
#   CI_PLAYWRIGHT_CACHE_APT_ETC replaces /etc/apt): the main one-line
#   sourcelist comes ONLY from /etc/apt/sources.list (empty file when
#   absent), and every .list/.sources under sources.list.d is rewritten to
#   its own file of the SAME name and extension in the sourceparts dir.
#   one-line and deb822 formats are never concatenated: real apt-get
#   (node:24-bookworm, indextargets) rejected the earlier single combined
#   file with "E: Type 'Types:' is not known" (exit 100); the split layout
#   parses with exit 0. Only http://deb.debian.org and
#   http://security.debian.org are rewritten to the same hosts over https;
#   every other source stays byte-identical. apt consumes it via -o
#   Dir::Etc::sourcelist=<file> -o Dir::Etc::sourceparts=<parts dir>, so the
#   original http sources cannot fire again. /etc/apt files are never
#   modified and source contents are never printed; without readable source
#   files no source flags are added (previous wrapper behavior). The
#   remote-CI win is expected but NOT yet measured on a real runner.
# - Proxy injection is host-scoped and evidence-driven, never global:
#   lowercase http_proxy / https_proxy win over uppercase HTTP_PROXY /
#   HTTPS_PROXY, and https falls back to the http proxy when no https value
#   exists. Flags are limited to Acquire::http(s)::Proxy::deb.debian.org and
#   ::security.debian.org — custom/internal mirrors keep their own routing
#   because a global Acquire::Proxy would hijack them. NO_PROXY / no_proxy
#   matching debian.org or '*' suppresses the injection; without any proxy
#   value no proxy flags are added at all. Proxy values are never printed or
#   stored in the repo; the wrapper stays in the controlled RUNNER_TEMP dir.
# - flock serializes concurrent jobs sharing one cache root. No `set -e`:
#   install/probe failures must reach the quarantine path instead of dying
#   mid-way; every critical command is checked explicitly.
# - Test seams (env only): CI_PLAYWRIGHT_CACHE_PROC_MOUNTS replaces
#   /proc/mounts, CI_PLAYWRIGHT_CACHE_OS_RELEASE replaces /etc/os-release,
#   CI_PLAYWRIGHT_CACHE_NODE / CI_PLAYWRIGHT_CACHE_NPX replace node / npx.
# - One greppable summary line, always printed:
#   CI_PLAYWRIGHT_CACHE dir=... persistent=yes|no mount=... fstype=...
#   writable=... version=... cache=hit|miss|repaired|failed
#   mount= keeps the real ancestor mount target even when persistent=no
#   (mount=none only when no non-root ancestor mount exists); fstype mirrors
#   the matching third /proc/mounts column (none without a mount).
#   (failed always exits non-zero; the version dir is quarantined).
# Usage: bash scripts/ci/ci-playwright-cache.sh <fallback-dir>   (from repo root)
set -uo pipefail

fallback="${1:?usage: ci-playwright-cache.sh <fallback-dir>}"
mounts_file="${CI_PLAYWRIGHT_CACHE_PROC_MOUNTS:-/proc/mounts}"
os_release_file="${CI_PLAYWRIGHT_CACHE_OS_RELEASE:-/etc/os-release}"
node_bin="${CI_PLAYWRIGHT_CACHE_NODE:-node}"
npx_bin="${CI_PLAYWRIGHT_CACHE_NPX:-npx}"
cache_root="${CI_CACHE_ROOT:-}"
run_id="${GITHUB_RUN_ID:-local}-$$"

die() {
  printf 'ci-playwright-cache: %s\n' "$1" >&2
  exit 1
}

[[ -f gui/package-lock.json ]] ||
  die "not called from repo root (gui/package-lock.json missing)"
[[ -f gui/node_modules/playwright-core/index.mjs ]] ||
  die "gui/node_modules/playwright-core/index.mjs missing (run npm --prefix gui ci first)"

version="$("$node_bin" -p "require('./gui/package-lock.json').packages['node_modules/playwright-core'].version")" ||
  die "cannot read playwright-core version from gui/package-lock.json"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "unexpected playwright-core version '$version' in gui/package-lock.json"

codename="$(sed -n 's/^VERSION_CODENAME=//p' "$os_release_file" | head -1 | tr -d '"')"
[[ -n "$codename" ]] || die "VERSION_CODENAME not found in ${os_release_file}"
arch="$(dpkg --print-architecture 2>/dev/null)" || die "dpkg --print-architecture failed"
key="${version}-${codename}-${arch}"

mount_target=none
mount_fstype=none
writable=no
volatile=no
persistent=no
if [[ -n "$cache_root" && -d "$cache_root" ]]; then
  real_root="$(cd "$cache_root" && pwd -P)"
  while read -r _ target fstype _; do
    [[ "$target" == "/" ]] && continue
    if [[ "$real_root" == "$target" || "$real_root" == "$target"/* ]]; then
      if [[ "$mount_target" == none || ${#target} -gt ${#mount_target} ]]; then
        mount_target="$target"
        mount_fstype="$fstype"
      fi
    fi
  done < "$mounts_file"
  if [[ "$mount_target" != none ]]; then
    if probe_file="$(mktemp "$real_root/.probe.XXXXXX" 2>/dev/null)"; then
      rm -f "$probe_file"
      writable=yes
    fi
    case "$mount_fstype" in
      tmpfs|ramfs|overlay)
        volatile=yes
        ;;
      *)
        if [[ "$writable" == yes ]]; then
          persistent=yes
        fi
        ;;
    esac
  fi
fi
if [[ "$persistent" == yes ]]; then
  cache_dir="$real_root"
else
  mkdir -p "$fallback" || die "cannot create fallback dir ${fallback}"
  cache_dir="$(cd "$fallback" && pwd -P)"
  if [[ "$volatile" == yes ]]; then
    reason=volatile-mount
  elif [[ "$mount_target" == none ]]; then
    reason=no-mount
  else
    reason=not-writable
  fi
  echo "::warning::playwright cache NOT persistent across jobs (CI_CACHE_ROOT='${cache_root}' mount=${mount_target} fstype=${mount_fstype} writable=${writable} reason=${reason}); using ${cache_dir}"
fi

version_dir="$cache_dir/$key"
browsers_dir="$version_dir/ms-playwright"
apt_lists="$version_dir/apt/lists"
apt_archives="$version_dir/apt/archives"
mkdir -p "$browsers_dir" || die "cannot create ${browsers_dir}"
mkdir -p "$apt_lists/partial" "$apt_archives/partial" ||
  die "cannot create apt cache dirs under ${version_dir}"

export PLAYWRIGHT_BROWSERS_PATH="$browsers_dir"

run_tmp="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ci-playwright-cache-run.XXXXXX")" ||
  die "cannot create run temp dir"
trap 'rm -rf "$run_tmp"' EXIT

real_apt_get="$(command -v apt-get 2>/dev/null || true)"
wrapper_bin="$run_tmp/bin"
mkdir -p "$wrapper_bin"
apt_etc="${CI_PLAYWRIGHT_CACHE_APT_ETC:-/etc/apt}"
apt_sources_file="$run_tmp/apt-sources.list"
apt_sources_parts="$run_tmp/apt-sourceparts"
apt_sources_ready=no
apt_parts_count=0
if [[ -d "$apt_etc" ]]; then
  mkdir -p "$apt_sources_parts"
  chmod 700 "$apt_sources_parts"
  : > "$apt_sources_file"
  chmod 600 "$apt_sources_file"
  if [[ -r "$apt_etc/sources.list" ]]; then
    sed -e 's|http://deb\.debian\.org|https://deb.debian.org|g' \
      -e 's|http://security\.debian\.org|https://security.debian.org|g' \
      "$apt_etc/sources.list" > "$apt_sources_file"
    apt_sources_ready=yes
  fi
  if [[ -d "$apt_etc/sources.list.d" ]]; then
    for f in "$apt_etc"/sources.list.d/*.list "$apt_etc"/sources.list.d/*.sources; do
      [[ -r "$f" ]] || continue
      part="$apt_sources_parts/$(basename "$f")"
      sed -e 's|http://deb\.debian\.org|https://deb.debian.org|g' \
        -e 's|http://security\.debian\.org|https://security.debian.org|g' \
        "$f" > "$part"
      chmod 600 "$part"
      apt_parts_count=$((apt_parts_count + 1))
      apt_sources_ready=yes
    done
  fi
fi
proxy_http="${http_proxy:-${HTTP_PROXY:-}}"
proxy_https="${https_proxy:-${HTTPS_PROXY:-${proxy_http:-}}}"
no_proxy_list="${NO_PROXY:-${no_proxy:-}}"
apt_proxy_scoped=no
case "$no_proxy_list" in
  *debian.org*|*\*) ;;
  *)
    if [[ -n "$proxy_http" || -n "$proxy_https" ]]; then
      apt_proxy_scoped=yes
    fi
    ;;
esac
if [[ -n "$real_apt_get" ]]; then
  apt_opts=("Dir::State::lists=$apt_lists" "Dir::Cache::archives=$apt_archives")
  if [[ "$apt_sources_ready" == yes ]]; then
    apt_opts+=("Dir::Etc::sourcelist=$apt_sources_file" "Dir::Etc::sourceparts=$apt_sources_parts")
  fi
  if [[ "$apt_proxy_scoped" == yes ]]; then
    if [[ -n "$proxy_http" ]]; then
      apt_opts+=("Acquire::http::Proxy::deb.debian.org=$proxy_http" "Acquire::http::Proxy::security.debian.org=$proxy_http")
    fi
    if [[ -n "$proxy_https" ]]; then
      apt_opts+=("Acquire::https::Proxy::deb.debian.org=$proxy_https" "Acquire::https::Proxy::security.debian.org=$proxy_https")
    fi
  fi
  {
    printf '#!/bin/sh\n'
    printf 'exec "%s"' "$real_apt_get"
    for opt in "${apt_opts[@]}"; do
      printf ' -o "%s"' "$opt"
    done
    printf ' "$@"\n'
  } > "$wrapper_bin/apt-get"
  chmod +x "$wrapper_bin/apt-get"
  export CI_PW_REAL_APT_GET="$real_apt_get"
  export CI_PW_APT_LISTS="$apt_lists"
  export CI_PW_APT_ARCHIVES="$apt_archives"
  if [[ "$apt_sources_ready" == yes ]]; then
    export CI_PW_APT_SOURCELIST="$apt_sources_file"
    echo "ci-playwright-cache: apt sources rewritten to https for deb.debian.org/security.debian.org (sourcelist=$apt_sources_file sourceparts=$apt_sources_parts part_files=$apt_parts_count)" >&2
  fi
  if [[ "$apt_proxy_scoped" == yes ]]; then
    echo "ci-playwright-cache: scoped apt proxy enabled for deb.debian.org/security.debian.org from standard proxy env (values not logged)" >&2
  fi
fi
export PATH="$wrapper_bin:$PATH"

cat > "$run_tmp/probe.html" <<'HTML_EOF'
<!doctype html><html><body><div id="ok">ok</div></body></html>
HTML_EOF

cat > "$run_tmp/probe.mjs" <<'PROBE_EOF'
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';
import { resolve as pathResolve, sep } from 'node:path';
const mod = await import(pathToFileURL(process.env.PW_CORE_ENTRY).href);
const { chromium } = mod;
const exePath = chromium.executablePath();
const base = pathResolve(process.env.PW_BROWSERS_PATH);
if (!exePath.startsWith(base + sep) && exePath !== base) {
  console.error(`probe: executablePath ${exePath} is not under ${base}`);
  process.exit(1);
}
if (!existsSync(exePath)) {
  console.error(`probe: executable ${exePath} does not exist`);
  process.exit(1);
}
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
try {
  const page = await browser.newPage();
  await page.goto(pathToFileURL(process.env.PW_PROBE_HTML).href);
  const text = await page.textContent('#ok');
  if (text !== 'ok') {
    console.error(`probe: DOM assertion failed, got ${JSON.stringify(text)}`);
    process.exit(1);
  }
} finally {
  await browser.close();
}
console.log(`probe: ok executable=${exePath}`);
PROBE_EOF

run_probe() {
  PW_CORE_ENTRY="$PWD/gui/node_modules/playwright-core/index.mjs" \
    PW_BROWSERS_PATH="$browsers_dir" \
    PW_PROBE_HTML="$run_tmp/probe.html" \
    "$node_bin" "$run_tmp/probe.mjs"
}

has_cached_browser() {
  local d
  for d in "$browsers_dir"/chromium-*; do
    [[ -d "$d" ]] && return 0
  done
  return 1
}

lock_file="$cache_dir/.${key}.lock"
touch "$lock_file" 2>/dev/null || die "cannot create lock file ${lock_file}"
exec 9>"$lock_file" || die "cannot open lock file ${lock_file}"
flock -w 900 9 || die "timed out waiting for playwright cache lock (${lock_file})"

cache_state=failed
fail_reason=""
if has_cached_browser && run_probe; then
  cache_state=hit
else
  had_browser_before_install=""
  if has_cached_browser; then
    had_browser_before_install=yes
    echo "ci-playwright-cache: cached browser present but real probe failed; reinstalling" >&2
  fi
  if "$npx_bin" -y "playwright@${version}" install --with-deps chromium; then
    if run_probe; then
      if has_cached_browser; then
        if [[ "$had_browser_before_install" == yes ]]; then
          cache_state=repaired
        else
          cache_state=miss
        fi
      else
        fail_reason="installer finished but no chromium cache dir exists"
      fi
    else
      fail_reason="installer finished but browser probe still fails"
    fi
  else
    fail_reason="npx playwright install --with-deps chromium failed"
  fi
fi

if [[ -n "$fail_reason" ]]; then
  invalid_dir="$cache_dir/.invalid-${run_id}-${key}"
  rm -rf "$invalid_dir" 2>/dev/null || true
  if mv "$version_dir" "$invalid_dir" 2>/dev/null; then
    quarantined="$invalid_dir"
  else
    quarantined="${version_dir} (rename failed)"
  fi
  printf 'ci-playwright-cache: %s; quarantined at %s\n' "$fail_reason" "$quarantined" >&2
fi

if [[ "$cache_state" != failed && -n "${GITHUB_ENV:-}" ]]; then
  printf 'PLAYWRIGHT_BROWSERS_PATH=%s\n' "$browsers_dir" >> "$GITHUB_ENV"
fi

echo "CI_PLAYWRIGHT_CACHE dir=${version_dir} persistent=${persistent} mount=${mount_target} fstype=${mount_fstype} writable=${writable} version=${version} cache=${cache_state}"
[[ "$cache_state" != failed ]] || exit 1
