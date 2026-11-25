#!/usr/bin/env bash
# Put a pinned Go toolchain on PATH for a CI job, reusing the runner's
# persistent cache -- the #941 ci-ensure-uv.sh pattern applied to Go.
#
# Why (infra-ops, 2026-09-29): the retired tsnet sidecar's e2e Go installer
# used to
# stream `curl https://go.dev/dl/go<v>.linux-<arch>.tar.gz | tar -xz` with no
# pin, no cache and no retry, and the host proxy exit drops TLS mid-handshake:
# task 42710 (#976) died with `curl: (35) SSL_ERROR_SYSCALL` to go.dev:443,
# task 42688 (#1019) with the same error against dl.google.com:443.  Both went
# green only on re-run.
#
# With CI_CACHE_ROOT set (every DinD runner mounts hb-ci-cache there):
#   1. the tarball lives at $CI_CACHE_ROOT/go/<version>/go<version>.linux-<arch>.tar.gz;
#   2. it is trusted only if its sha256 equals the PINNED digest below, so a
#      half-written or corrupted file on the shared volume is never reused;
#   3. on a miss it prints one loud `CI_TOOL_CACHE miss ...` line (so a cold
#      cache is told apart from an egress failure), tries the official mirrors
#      in order (go.dev, then dl.google.com), downloads to a temp file,
#      verifies against the same pin and renames into place atomically.
# Without CI_CACHE_ROOT the job behaves as before (download + extract into a
# per-run dir) but the tarball is STILL verified against the pin -- no more
# unverified `curl | tar`.  The tarball is always verified BEFORE extraction.
#
# Pins: Go's OFFICIAL published digests for release go1.26.8 (kind=archive),
# from https://go.dev/dl/?mode=json&include=all (recorded 2026-09-29):
#   go1.26.8.linux-amd64.tar.gz d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b
#   go1.26.8.linux-arm64.tar.gz 211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0
# Go 1.27.1 archive pins from the same official JSON (recorded 2026-10-03):
#   go1.27.1.linux-amd64.tar.gz 63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445
#   go1.27.1.linux-arm64.tar.gz 3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec
# Go 1.27.1 darwin-arm64 from the same official JSON (2026-10-08):
#   go1.27.1.darwin-arm64.tar.gz ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12
# Do NOT derive a pin by hashing your own download.
#
# Env: GO_VERSION (default below), GO_ROOT_DIR (default /tmp/hyprial-go-$GITHUB_RUN_ID),
#      GO_DOWNLOAD_BASES (space-separated mirror bases, default the two
#      official mirrors) and GO_ARCHIVE_SHA256 (override, for tests).
set -euo pipefail

GO_VERSION="${GO_VERSION:-1.26.8}"
GO_ROOT_DIR="${GO_ROOT_DIR:-/tmp/hyprial-go-${GITHUB_RUN_ID:-local}}"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "ensure-go: unsupported runner architecture: $(uname -m)" >&2; exit 1 ;;
esac

case "$(uname -s)" in
  Linux) go_os=linux ;;
  Darwin) go_os=darwin ;;
  *) echo "ensure-go: unsupported runner OS: $(uname -s)" >&2; exit 1 ;;
esac

case "${GO_VERSION}/${go_os}/${arch}" in
  1.26.8/linux/amd64) pin=d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b ;;
  1.26.8/linux/arm64) pin=211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0 ;;
  1.27.1/linux/amd64) pin=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445 ;;
  1.27.1/linux/arm64) pin=3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec ;;
  1.27.1/darwin/arm64) pin=ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12 ;;
  *) pin="" ;;
esac
pin="${GO_ARCHIVE_SHA256:-$pin}"
if [[ -z "$pin" ]]; then
  echo "ensure-go: no pinned digest for ${GO_VERSION}/${go_os}-${arch}" >&2
  exit 1
fi

archive="go${GO_VERSION}.${go_os}-${arch}.tar.gz"
bases="${GO_DOWNLOAD_BASES:-https://go.dev/dl https://dl.google.com/go}"

# download_verified <dest>: try each mirror in order; a transport failure
# falls through to the next mirror, a digest mismatch fails loudly (the bytes
# came through but are not what the official pin promises -- never silently
# shop the next mirror for a different-looking release).  Prints the source
# URL on success.
download_verified() {
  local dest="$1" base url got
  for base in $bases; do
    url="${base}/${archive}"
    if curl --fail --location --silent --show-error --retry 2 -o "$dest" "$url"; then
      got="$(sha256_of "$dest")"
      if [[ "$got" == "$pin" ]]; then
        echo "$url"
        return 0
      fi
      echo "ensure-go: downloaded ${archive} from ${url} sha256=${got} does not match the pinned sha256 ${pin}" >&2
      return 1
    fi
    echo "ensure-go: download failed from ${url}; trying next mirror" >&2
  done
  return 1
}

fetch_into_cache() {
  local dir cached tmp source
  dir="${CI_CACHE_ROOT}/go/${GO_VERSION}"
  cached="${dir}/${archive}"
  mkdir -p "$dir"
  if [[ -f "$cached" && "$(sha256_of "$cached")" == "$pin" ]]; then
    echo "ensure-go: cache hit ${cached}"
  else
    [[ -f "$cached" ]] && echo "ensure-go: cached archive digest mismatch; re-downloading"
    echo "CI_TOOL_CACHE miss tool=go version=${GO_VERSION} source=${bases%% *}/${archive}"
    tmp="$(mktemp "${dir}/.${archive}.XXXXXX")"
    if ! source="$(download_verified "$tmp")"; then
      rm -f "$tmp"
      return 1
    fi
    chmod 0644 "$tmp"
    mv -f "$tmp" "$cached"
    echo "ensure-go: cached ${cached} (from ${source})"
  fi
  mkdir -p "$GO_ROOT_DIR"
  tar -xzf "$cached" -C "$GO_ROOT_DIR"
}

fetch_without_cache() {
  local tmp source
  echo "ensure-go: CI_CACHE_ROOT unset; downloading ${archive} without caching (pin still enforced)"
  mkdir -p "$GO_ROOT_DIR"
  tmp="$(mktemp "${GO_ROOT_DIR}/.${archive}.XXXXXX")"
  if ! source="$(download_verified "$tmp")"; then
    rm -f "$tmp"
    return 1
  fi
  tar -xzf "$tmp" -C "$GO_ROOT_DIR"
  rm -f "$tmp"
  echo "ensure-go: extracted ${archive} (from ${source})"
}

if [[ -n "${CI_CACHE_ROOT:-}" ]]; then
  fetch_into_cache
else
  fetch_without_cache
fi

version="$("${GO_ROOT_DIR}/go/bin/go" version)"
echo "$version"
case "$version" in
  "go version go${GO_VERSION} "*) ;;
  *) echo "ensure-go: expected go${GO_VERSION}, got: ${version}" >&2; exit 1 ;;
esac
if [[ -n "${GITHUB_PATH:-}" ]]; then echo "${GO_ROOT_DIR}/go/bin" >> "$GITHUB_PATH"; fi
