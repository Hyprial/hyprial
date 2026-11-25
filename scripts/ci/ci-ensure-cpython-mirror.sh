#!/usr/bin/env bash
# Pre-stage uv's managed CPython (python-build-standalone, --python 3.12) into
# the runner's persistent cache and point uv at it as a local file:// mirror.
#
# Why (infra-ops, 2026-09-29): CI jobs run `uv sync/run --python 3.12`; when
# the interpreter is not cached, uv downloads a python-build-standalone
# CPython from github.com through the host proxy, and the proxy exit drops TLS
# mid-handshake -- the #986 A/B preparation died fetching
# cpython-3.12.13+20260414-aarch64-unknown-linux-gnu-install_only_stripped.
#
# Reading (2026-09-29, uv 0.11.7): UV_PYTHON_INSTALL_MIRROR accepts a file://
# URL; uv resolves <mirror>/<release-tag>/<filename> to a local path and
# installs fully offline.  Verified locally:
#   $ mkdir -p "$T/mirror/20260414" && curl -o "$T/mirror/20260414/<archive>" \
#       https://github.com/astral-sh/python-build-standalone/releases/download/20260414/<archive>
#   $ UV_PYTHON_INSTALL_MIRROR="file://$T/mirror" UV_PYTHON_INSTALL_DIR="$T/install" \
#       uv python install 3.12 --managed-python --reinstall -v
#   DEBUG Downloading file:///tmp/.../mirror/20260414/cpython-3.12.13%2B20260414-aarch64-apple-darwin-install_only_stripped.tar.gz
#   Installed Python 3.12.13 in 631ms
#   $ "$T/install/cpython-3.12.13-macos-aarch64-none/bin/python3.12" -c 'import sys; print(sys.version)'
#   3.12.13 (main, Apr 14 2026, 14:33:40) [Clang 22.1.3 ]
#
# With CI_CACHE_ROOT set (every DinD runner mounts hb-ci-cache there) this
# script keeps the same #941 semantics as ci-ensure-uv.sh / ci-ensure-go.sh:
#   1. the archive lives at $CI_CACHE_ROOT/cpython/<release-tag>/<archive>
#      (the exact layout uv expects under the mirror base);
#   2. it is trusted only if its sha256 equals the PIN below;
#   3. on a miss it prints one loud `CI_TOOL_CACHE miss ...` line, downloads
#      to a temp file, verifies against the same pin and renames atomically;
#   4. it then writes UV_PYTHON_INSTALL_MIRROR=file://$CI_CACHE_ROOT/cpython
#      to $GITHUB_ENV (and echoes it) so every later `uv ... --python 3.12`
#      step in the job installs the interpreter from the local mirror.
# Without CI_CACHE_ROOT: no-op; uv downloads from github.com as before.
#
# Pins: SHA256SUMS published with the python-build-standalone 20260414 release,
# from https://github.com/astral-sh/python-build-standalone/releases/download/20260414/SHA256SUMS
# (recorded 2026-09-29):
#   cpython-3.12.13+20260414-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
#     3c3427e5628648478da2aa227472c350475a68bc58109f1b43849636a4aecb89
#   cpython-3.12.13+20260414-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
#     953dcf67ff395d5baff77ef60bc852aaadad4c169e9b8d65dbc40ab9461a13e3
# Do NOT derive a pin by hashing your own download.
#
# Free-threaded interpreters (owner 2026-11-17, CI load): `uv python install
# 3.14t` / `3.15t` in native-conformance used to `unset
# UV_PYTHON_INSTALL_MIRROR` and fetch from github.com on every run, which is
# where the 6 cpython-3... download failures of 2026-11-17 03:10-04:30 UTC came
# from (the 3.12 mirror holds only the 3.12 archive). Run with one argument,
# `3.14t` or `3.15t`, the script stages that single archive the same way
# (pinned sha256, atomic rename, same mirror directory); without an argument it
# stages 3.12 exactly as before. The pair uv 0.11.7 resolves, read from its own
# `uv python list --show-urls`, is 3.14.4 and 3.15.0a8 in release 20260414; the
# pins are the digests published in that release's SHA256SUMS (recorded
# 2026-11-17):
#   cpython-3.14.4+20260414-x86_64-unknown-linux-gnu-freethreaded-install_only_stripped.tar.gz
#     5c539d5421ce145e10ea455f5b50729db1f7a5591198a75eabed87a8a8b9d30e
#   cpython-3.14.4+20260414-aarch64-unknown-linux-gnu-freethreaded-install_only_stripped.tar.gz
#     629dd91192c64255af852150f1aea16e98c8cc9a8a106dec9a9655c86906a920
#   cpython-3.15.0a8+20260414-x86_64-unknown-linux-gnu-freethreaded-install_only_stripped.tar.gz
#     555122ff5e95beabc7935977f528445e07806ce4caeff2052d22daf83b2b7d57
#   cpython-3.15.0a8+20260414-aarch64-unknown-linux-gnu-freethreaded-install_only_stripped.tar.gz
#     c6fc434b6af1f1bd9630ce3b4ad80079c684f23654ae926e3f064f1fa85058d3
# The archive download is wrapped by ci_retry_uv (scripts/ci/ci-retry.sh), the
# network fetch only; the digest check stays outside the retry. Jobs that need
# a free-threaded interpreter on a host without CI_CACHE_ROOT (the macOS host
# runner) keep `unset UV_PYTHON_INSTALL_MIRROR`.
#
# Env: CPYTHON_VERSION / CPYTHON_RELEASE (defaults below; the pair uv 0.11.7
#      resolves for `--python 3.12`), CPYTHON_DOWNLOAD_BASE and
#      CPYTHON_ARCHIVE_SHA256 (overrides, for tests).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/ci-retry.sh"

CPYTHON_VERSION="${CPYTHON_VERSION:-3.12.13}"
CPYTHON_RELEASE="${CPYTHON_RELEASE:-20260414}"
variant="default"
flavor=""
case "${1:-}" in
  "") ;;
  3.14t) variant=3.14t; CPYTHON_VERSION=3.14.4; flavor="-freethreaded" ;;
  3.15t) variant=3.15t; CPYTHON_VERSION=3.15.0a8; flavor="-freethreaded" ;;
  *) echo "ensure-cpython-mirror: unknown variant '${1}' (expected none, 3.14t or 3.15t)" >&2; exit 2 ;;
esac

if [[ -z "${CI_CACHE_ROOT:-}" ]]; then
  echo "ensure-cpython-mirror: CI_CACHE_ROOT unset; uv will download CPython ${CPYTHON_VERSION} from github.com as before"
  exit 0
fi

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

case "$(uname -m)" in
  x86_64|amd64) arch=x86_64-unknown-linux-gnu ;;
  aarch64|arm64) arch=aarch64-unknown-linux-gnu ;;
  *) echo "ensure-cpython-mirror: unpinned arch $(uname -m); uv will download as before"; exit 0 ;;
esac

case "${CPYTHON_VERSION}+${CPYTHON_RELEASE}/${arch}${flavor}" in
  3.12.13+20260414/x86_64-unknown-linux-gnu)
    pin=3c3427e5628648478da2aa227472c350475a68bc58109f1b43849636a4aecb89 ;;
  3.12.13+20260414/aarch64-unknown-linux-gnu)
    pin=953dcf67ff395d5baff77ef60bc852aaadad4c169e9b8d65dbc40ab9461a13e3 ;;
  3.14.4+20260414/x86_64-unknown-linux-gnu-freethreaded)
    pin=5c539d5421ce145e10ea455f5b50729db1f7a5591198a75eabed87a8a8b9d30e ;;
  3.14.4+20260414/aarch64-unknown-linux-gnu-freethreaded)
    pin=629dd91192c64255af852150f1aea16e98c8cc9a8a106dec9a9655c86906a920 ;;
  3.15.0a8+20260414/x86_64-unknown-linux-gnu-freethreaded)
    pin=555122ff5e95beabc7935977f528445e07806ce4caeff2052d22daf83b2b7d57 ;;
  3.15.0a8+20260414/aarch64-unknown-linux-gnu-freethreaded)
    pin=c6fc434b6af1f1bd9630ce3b4ad80079c684f23654ae926e3f064f1fa85058d3 ;;
  *) pin="" ;;
esac
pin="${CPYTHON_ARCHIVE_SHA256:-$pin}"
if [[ -z "$pin" ]]; then
  echo "ensure-cpython-mirror: no pinned digest for ${CPYTHON_VERSION}+${CPYTHON_RELEASE}/${arch}; uv will download as before"
  exit 0
fi

archive="cpython-${CPYTHON_VERSION}+${CPYTHON_RELEASE}-${arch}${flavor}-install_only_stripped.tar.gz"
base="${CPYTHON_DOWNLOAD_BASE:-https://github.com/astral-sh/python-build-standalone/releases/download}"
url="${base}/${CPYTHON_RELEASE}/${archive}"
dir="${CI_CACHE_ROOT}/cpython/${CPYTHON_RELEASE}"
cached="${dir}/${archive}"
mkdir -p "$dir"
if [[ -f "$cached" && "$(sha256_of "$cached")" == "$pin" ]]; then
  echo "ensure-cpython-mirror: cache hit ${cached}"
else
  [[ -f "$cached" ]] && echo "ensure-cpython-mirror: cached archive digest mismatch; re-downloading"
  echo "CI_TOOL_CACHE miss tool=cpython version=${CPYTHON_VERSION}+${CPYTHON_RELEASE} source=${url}"
  tmp="$(mktemp "${dir}/.${archive}.XXXXXX")"
  if ! ci_retry_uv "cpython-download-${variant}" curl --fail --location --silent --show-error --retry 2 -o "$tmp" "$url"; then
    rm -f "$tmp"
    exit 1
  fi
  got="$(sha256_of "$tmp")"
  if [[ "$got" != "$pin" ]]; then
    rm -f "$tmp"
    echo "ensure-cpython-mirror: downloaded ${archive} sha256=${got} does not match the pinned sha256 ${pin}" >&2
    exit 1
  fi
  chmod 0644 "$tmp"
  mv -f "$tmp" "$cached"
  echo "ensure-cpython-mirror: cached ${cached}"
fi

mirror="file://${CI_CACHE_ROOT}/cpython"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "UV_PYTHON_INSTALL_MIRROR=${mirror}" >> "$GITHUB_ENV"
fi
echo "ensure-cpython-mirror: UV_PYTHON_INSTALL_MIRROR=${mirror}"
