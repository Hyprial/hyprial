#!/usr/bin/env bash
# Put a pinned uv on PATH for a CI job, reusing the runner's persistent cache.
#
# Why (infra-ops, 2026-09-28): every unit job fetched uv release assets through
# overseas endpoints.  uv-custom mirrors the same release layout domestically.
# and then the release archive, both through the host proxy to overseas
# endpoints.  Over 7 days that cost 635 runner-minutes (115 fetches > 60 s,
# 18 > 180 s) and at least 13 outright failures before any test ran
# (SSL_ERROR_SYSCALL; 6-minute step deadlines).
#
# Tier order (first one that applies wins):
#   host  : an already-installed uv that is EXACTLY the pin (UV_HOST_BIN, default
#           /opt/homebrew/bin/uv) — no network at all;
#   cache : $CI_CACHE_ROOT/uv/<version>/<archive>, sha256-pinned — Linux only;
#   mirror: the uv-custom published install.sh (live fetch) — the fallback.
# The tier actually used is printed (`ensure-uv: tier=...`), so a green run still
# records which path it took: a log with no astral.sh fetch is the evidence that
# the live-fetch dependence is gone, and one green run alone is not.
#
# With CI_CACHE_ROOT set (every DinD runner mounts hb-ci-cache there):
#   1. the release ARCHIVE is kept at $CI_CACHE_ROOT/uv/<version>/<archive>;
#   2. it is trusted only if its sha256 equals the PINNED digest below, so a
#      half-written or corrupted file on the shared volume is never reused;
#   3. on a miss it is downloaded to a temp file, verified against the same pin,
#      and renamed into place atomically;
#   4. it is extracted into a per-run dir and `uv --version` must name the pin.
# Without CI_CACHE_ROOT the job uses the uv-custom installer fallback.
# With CI_CACHE_ROOT, an unpinned version or arch FAILS (a bump must add its pin),
# and a NON-Linux host under CI_CACHE_ROOT fails loudly instead of downloading a
# Linux archive (the cache tier's pins are Linux-only).
#
# The cache-tier archive download is wrapped by ci_retry_uv (scripts/ci/ci-retry.sh,
# 2026-11-17: the runner's egress failed intermittently for minutes), the
# network fetch only; the pinned-digest check stays outside the retry.
#
# Env: UV_VERSION (default below), UV_BIN_DIR (default /tmp/hyprial-uv-$GITHUB_RUN_ID),
#      UV_HOST_BIN (host tier probe, default /opt/homebrew/bin/uv),
#      UV_DOWNLOAD_BASE and UV_ARCHIVE_SHA256 (overrides, for tests).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/ci-retry.sh"

UV_VERSION="${UV_VERSION:-0.11.7}"
UV_BIN_DIR="${UV_BIN_DIR:-/tmp/hyprial-uv-${GITHUB_RUN_ID:-local}}"
#: Which of the three tiers supplied uv; printed at the end so the log records it.
UV_TIER=""

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

install_from_mirror() {
  curl --fail --location --silent --show-error \
    "${UV_INSTALLER_BASE:-https://uv.agentsmirror.com/github/astral-sh/uv/releases/download}/${UV_VERSION}/uv-installer.sh" \
    | env UV_UNMANAGED_INSTALL="${UV_BIN_DIR}" sh
}

# Host-first tier: an already-installed uv that is EXACTLY the pin costs nothing
# and touches no network.  Set UV_HOST_BIN to override the probed path; an
# absent or mismatched host uv simply falls through to the next tier.
install_from_host() {
  local host_bin host_version
  host_bin="${UV_HOST_BIN:-/opt/homebrew/bin/uv}"
  [[ -x "$host_bin" ]] || return 1
  host_version="$("$host_bin" --version 2>/dev/null || true)"
  case "$host_version" in
    "uv ${UV_VERSION}"|"uv ${UV_VERSION} "*) ;;
    *) return 1 ;;
  esac
  mkdir -p "$UV_BIN_DIR"
  ln -sf "$host_bin" "${UV_BIN_DIR}/uv"
  UV_TIER="host(${host_bin})"
  echo "ensure-uv: tier=host ${host_bin} is ${host_version}; no download"
}

install_from_cache() {
  local arch pin archive dir cached tmp work
  # The pinned archives are Linux-only.  On any other host the cache tier would
  # download a Linux binary that cannot run, so refuse it loudly instead of
  # letting `uv --version` fail later with a message that hides the real cause.
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "ensure-uv: refusing the cache tier on $(uname -s): the pinned archives are Linux-only." >&2
    echo "ensure-uv: set UV_HOST_BIN, install a matching uv on the host, or unset CI_CACHE_ROOT." >&2
    return 1
  fi
  case "$(uname -m)" in
    aarch64|arm64) arch=aarch64-unknown-linux-gnu ;;
    x86_64|amd64) arch=x86_64-unknown-linux-gnu ;;
    *) echo "ensure-uv: no pinned digest for arch $(uname -m); add a pin before using the cache" >&2; return 1 ;;
  esac
  # Published by astral-sh/uv and mirrored by uv-custom for this exact version.
  case "${UV_VERSION}/${arch}" in
    0.11.7/aarch64-unknown-linux-gnu) pin=f2ee1cde9aabb4c6e43bd3f341dadaf42189a54e001e521346dc31547310e284 ;;
    0.11.7/x86_64-unknown-linux-gnu) pin=6681d691eb7f9c00ac6a3af54252f7ab29ae72f0c8f95bdc7f9d1401c23ea868 ;;
    *) pin="" ;;
  esac
  pin="${UV_ARCHIVE_SHA256:-$pin}"
  # A version bump without a new pin must fail loudly, not silently drop to an
  # unverified install.sh (and lose the cache): the pin is part of the bump.
  if [[ -z "$pin" ]]; then
    echo "ensure-uv: no pinned digest for ${UV_VERSION}/${arch}; add it to scripts/ci/ci-ensure-uv.sh" >&2
    return 1
  fi
  archive="uv-${arch}.tar.gz"
  dir="${CI_CACHE_ROOT}/uv/${UV_VERSION}"
  cached="${dir}/${archive}"
  mkdir -p "$dir"
  # Temp files left by a job killed mid-download are never read; reap day-old ones.
  find "$dir" -maxdepth 1 -name ".${archive}.*" -mmin +1440 -delete 2>/dev/null || true
  # Everything below verifies and extracts a PRIVATE copy, so what is hashed is
  # exactly what is extracted, whatever happens to the shared path meanwhile.
  work="$(mktemp -d)"
  if [[ -f "$cached" ]] && cp "$cached" "${work}/${archive}" && [[ "$(sha256_of "${work}/${archive}")" == "$pin" ]]; then
    echo "ensure-uv: cache hit ${cached}"
  else
    [[ -f "$cached" ]] && echo "ensure-uv: cached archive digest mismatch; re-downloading"
    tmp="$(mktemp "${dir}/.${archive}.XXXXXX")"
    if ! ci_retry_uv uv-archive-download curl --fail --location --silent --show-error --retry 2 -o "$tmp" \
      "${UV_DOWNLOAD_BASE:-https://uv.agentsmirror.com/github/astral-sh/uv/releases/download}/${UV_VERSION}/${archive}"; then
      rm -f "$tmp"; rm -rf "$work"
      return 1
    fi
    cp "$tmp" "${work}/${archive}"
    if [[ "$(sha256_of "${work}/${archive}")" != "$pin" ]]; then
      rm -f "$tmp"; rm -rf "$work"
      echo "ensure-uv: downloaded ${archive} does not match the pinned sha256" >&2
      return 1
    fi
    chmod 0644 "$tmp"
    mv -f "$tmp" "$cached"
    echo "ensure-uv: cached ${cached}"
  fi
  mkdir -p "$UV_BIN_DIR"
  tar -xzf "${work}/${archive}" -C "$UV_BIN_DIR" --strip-components=1
  rm -rf "$work"
}

# Tier order: host (no network) -> cache (pinned) -> uv-custom installer (live fetch).
# The chosen tier is printed, so a green run still records WHICH path it took.
if install_from_host; then
  :
elif [[ -n "${CI_CACHE_ROOT:-}" ]]; then
  UV_TIER="cache(${CI_CACHE_ROOT})"
  install_from_cache
else
  UV_TIER="mirror"
  install_from_mirror
fi

version="$("${UV_BIN_DIR}/uv" --version)"
echo "$version"
case "$version" in
  "uv ${UV_VERSION}"|"uv ${UV_VERSION} "*) ;;
  *) echo "ensure-uv: expected uv ${UV_VERSION}, got: ${version}" >&2; exit 1 ;;
esac
if [[ -n "${GITHUB_PATH:-}" ]]; then echo "${UV_BIN_DIR}" >> "$GITHUB_PATH"; fi
echo "ensure-uv: tier=${UV_TIER} -> ${UV_BIN_DIR}"
