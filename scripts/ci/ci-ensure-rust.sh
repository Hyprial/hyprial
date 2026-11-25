#!/usr/bin/env bash
# E2E-005 needs the real native wheel build, not a skip-native exception.
# Rust 1.93.1 lives in this run's private cargo/rustup homes. Only downloads
# and Cargo registry/git caches are shared in an E2E-specific user cache.
# rustup-init 1.28.2 pins below come from the official versioned archive's
# <host>/rustup-init.sha256 files, fetched 2026-11-08 over verified HTTPS:
# https://static.rust-lang.org/rustup/archive/1.28.2/
# An explicit preinstalled toolchain seed is read-only and version/host checked.
# Official manifest hashes admit only complete archives to the E2E dist cache.
# Version probing must not auto-install before those archives are staged.
# Owner 2026-11-15: the three network fetches (rustup-init download, official
# distribution staging, `rustup toolchain install`) each run under ci_retry
# from ci-retry.sh: at most 3 attempts, short backoff, same pinned version and
# checksums, one CI_RETRY line per failed attempt, non-zero after the last.
# Checksum and version checks are never inside a retry.
set -euo pipefail
umask 077

fail() {
  printf 'e2e-rust: %s\n' "$*" >&2
  exit 1
}

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/ci-retry.sh"

[[ "$#" -eq 0 ]] || fail 'takes no arguments; configure the isolated E2E root instead'
RUST_VERSION="${RUST_VERSION:-1.93.1}"
[[ "$RUST_VERSION" == 1.93.1 ]] || fail 'Rust version must be exactly 1.93.1'
[[ "${HYPRIAL_BUILD_SKIP_NATIVE:-}" != 1 ]] || fail 'E2E native preparation must not skip the native build'
[[ -n "${HYPRIAL_E2E_ROOT:-}" ]] || fail 'HYPRIAL_E2E_ROOT is required'
[[ -n "${HOME:-}" && -d "$HOME" ]] || fail 'HOME must name an existing directory'
physical_home="$(cd "$HOME" && pwd -P)"

private_directory() {
  local value="$1" label="$2" boundary="${3:-}" resolved ancestor suffix="" leaf
  case "$value" in /*) ;; *) fail "$label must be absolute" ;; esac
  case "$value" in *$'\n'*|*$'\r'*) fail "$label must not contain line breaks" ;; esac
  case "$value/" in */../*|*/./*) fail "$label must not contain dot path components" ;; esac
  case "$value" in
    /|"$HOME"|"$physical_home"|"$HOME/.hyprial"|"$HOME/.hyprial/"*|"$HOME/.cargo"|"$HOME/.cargo/"*|"$HOME/.rustup"|"$HOME/.rustup/"*)
      fail "$label must be an E2E directory, not a global user home" ;;
  esac
  ancestor="$value"
  while [[ ! -d "$ancestor" ]]; do
    [[ ! -e "$ancestor" && ! -L "$ancestor" ]] || fail "$label has a non-directory ancestor"
    leaf="${ancestor##*/}"
    suffix="/$leaf$suffix"
    ancestor="${ancestor%/*}"
    [[ -n "$ancestor" ]] || ancestor=/
  done
  resolved="$(cd "$ancestor" && pwd -P)$suffix"
  case "$resolved" in
    /|"$physical_home"|"$physical_home/.hyprial"|"$physical_home/.hyprial/"*|"$physical_home/.cargo"|"$physical_home/.cargo/"*|"$physical_home/.rustup"|"$physical_home/.rustup/"*)
      fail "$label resolves into a global user home" ;;
  esac
  if [[ -n "$boundary" ]]; then
    case "$resolved" in "$boundary/"*) ;; *) fail "$label must stay inside this E2E root" ;; esac
  fi
  mkdir -p "$resolved"
  printf '%s\n' "$resolved"
}

e2e_root="$(private_directory "$HYPRIAL_E2E_ROOT" HYPRIAL_E2E_ROOT)"
cache="${HYPRIAL_E2E_RUST_CACHE:-${CI_CACHE_ROOT:-$HOME/.cache/hyprial-e2e}/e2e/rust}"
cache="$(private_directory "$cache" HYPRIAL_E2E_RUST_CACHE)"
tool_root="$(private_directory "${e2e_root}/toolchain/rust" E2E_RUST_ROOT "$e2e_root")"
CARGO_HOME="$(private_directory "${tool_root}/cargo" CARGO_HOME "$e2e_root")"
RUSTUP_HOME="$(private_directory "${tool_root}/rustup" RUSTUP_HOME "$e2e_root")"
export CARGO_HOME RUSTUP_HOME
export RUSTUP_TOOLCHAIN="$RUST_VERSION"
export RUSTUP_DIST_SERVER=https://static.rust-lang.org
export RUSTUP_UPDATE_ROOT=https://static.rust-lang.org/rustup
export RUSTUP_MAX_RETRIES=2
private_directory "$RUSTUP_HOME/toolchains" RUSTUP_TOOLCHAINS "$e2e_root" >/dev/null
for name in bootstrap registry git dist; do
  [[ ! -L "$cache/$name" ]] || fail "Rust $name cache directory symlink refused"
  private_directory "$cache/$name" "E2E_RUST_CACHE_$name" "$cache" >/dev/null
done

case "$(uname -s)/$(uname -m)" in
  Linux/x86_64|Linux/amd64) host=x86_64-unknown-linux-gnu; pin=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c ;;
  Linux/aarch64|Linux/arm64) host=aarch64-unknown-linux-gnu; pin=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c ;;
  Darwin/arm64|Darwin/aarch64) host=aarch64-apple-darwin; pin=20ef5516c31b1ac2290084199ba77dbbcaa1406c45c1d978ca68558ef5964ef5 ;;
  Darwin/x86_64) host=x86_64-apple-darwin; pin=9c331076f62b4d0edeae63d9d1c9442d5fe39b37b05025ec8d41c5ed35486496 ;;
  *) fail 'unsupported E2E Rust host' ;;
esac
toolchain="${RUST_VERSION}-${host}"
tool_dir="$RUSTUP_HOME/toolchains/$toolchain"

verify_tools() {
  local directory="$1" compiler cargo
  [[ -x "$directory/rustc" && -x "$directory/cargo" ]] || fail 'rustc/cargo toolchain executables are absent'
  compiler="$("$directory/rustc" -vV)" || fail 'rustc version probe failed'
  case "$compiler" in "rustc ${RUST_VERSION} ("*) ;; *) fail "expected rustc ${RUST_VERSION}" ;; esac
  printf '%s\n' "$compiler" | grep -Fxq "host: $host" || fail "rustc host does not match $host"
  cargo="$("$directory/cargo" --version)" || fail 'cargo version probe failed'
  case "$cargo" in "cargo ${RUST_VERSION} ("*) ;; *) fail "expected cargo ${RUST_VERSION}" ;; esac
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

bootstrap() {
  local archive="$cache/bootstrap/rustup-init-1.28.2-$host"
  [[ ! -L "$archive" ]] || fail 'cached rustup-init symlink refused'
  [[ ! -e "$archive" || -f "$archive" ]] || fail 'cached rustup-init must be a regular file'
  if [[ ! -f "$archive" ]]; then
    downloaded="$(mktemp "$cache/bootstrap/.rustup-init.XXXXXX")"
    trap 'rm -f "${downloaded:-}"' EXIT
    printf 'CI_TOOL_CACHE miss tool=rustup version=1.28.2 host=%s\n' "$host" >&2
    if ! ci_retry rustup-init-download curl --fail --location --max-redirs 0 --proto '=https' --proto-redir '=https' \
      --silent --show-error --connect-timeout 10 --max-time 180 --speed-time 30 --speed-limit 16384 \
      --retry 2 --retry-delay 2 --retry-max-time 300 \
      --write-out 'e2e-rust-bootstrap: http=%{http_code} bytes=%{size_download} seconds=%{time_total}\n' \
      --output "$downloaded" "https://static.rust-lang.org/rustup/archive/1.28.2/$host/rustup-init" >&2; then
      fail 'official rustup-init download failed'
    fi
    [[ "$(sha256_of "$downloaded")" == "$pin" ]] || fail 'official rustup-init checksum mismatch; refusing execution'
    chmod 0700 "$downloaded"
    mv -f "$downloaded" "$archive"
  fi
  [[ "$(sha256_of "$archive")" == "$pin" ]] || fail 'cached rustup-init checksum mismatch; refusing execution'
  printf '%s\n' "$archive"
}

for name in registry git; do
  destination="$CARGO_HOME/$name"
  if [[ ! -e "$destination" && ! -L "$destination" ]]; then
    ln -s "$cache/$name" "$destination"
  else
    [[ -L "$destination" && "$(readlink "$destination")" == "$cache/$name" ]] || fail "foreign Cargo $name cache in this E2E root"
  fi
done

if [[ -e "$tool_dir" || -L "$tool_dir" ]]; then
  verify_tools "$tool_dir/bin"
elif [[ -n "${HYPRIAL_E2E_RUST_SEED:-}" ]]; then
  seed="$HYPRIAL_E2E_RUST_SEED"
  case "$seed" in /*) ;; *) fail 'toolchain seed must be absolute' ;; esac
  [[ -d "$seed" ]] || fail 'toolchain seed directory is absent'
  verify_tools "$seed/bin"
  ln -s "$seed" "$tool_dir"
else
  rustup="${HYPRIAL_RUSTUP:-$(command -v rustup || true)}"
  if [[ -n "$rustup" ]]; then
    [[ -x "$rustup" ]] || fail 'configured rustup is not executable'
  else
    installer="$(bootstrap)"
    "$installer" -y --no-modify-path --profile minimal --default-host "$host" --default-toolchain none
    rustup="$CARGO_HOME/bin/rustup"
    [[ -x "$rustup" ]] || fail 'verified bootstrap did not install rustup'
  fi
  manager_version="$(cd "$RUSTUP_HOME" && env -u RUSTUP_TOOLCHAIN RUSTUP_AUTO_INSTALL=0 "$rustup" --version)" || fail 'rustup version probe failed'
  case "$manager_version" in "rustup 1.28.2 ("*) ;; *) fail 'rustup version must be exactly 1.28.2' ;; esac
  printf '%s\n' "$manager_version"
  private_directory "$RUSTUP_HOME/downloads" RUSTUP_DOWNLOADS "$e2e_root" >/dev/null
  script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  ci_retry rust-dist-staging uv run --no-project --python 3.12 python "$script_root/ci-rust-dist.py" \
    --host "$host" --version "$RUST_VERSION" --run-root "$e2e_root" \
    --cache-root "$cache" --downloads "$RUSTUP_HOME/downloads" \
    || fail 'rust-dist-staging failed after 3 attempts'
  ci_retry rustup-toolchain-install "$rustup" toolchain install "$RUST_VERSION" --profile minimal --no-self-update \
    || fail 'rustup-toolchain-install failed after 3 attempts'
  verify_tools "$tool_dir/bin"
fi

export HYPRIAL_CARGO="$tool_dir/bin/cargo"
export PATH="$tool_dir/bin:$PATH"
if [[ -n "${GITHUB_PATH:-}" ]]; then printf '%s\n' "$tool_dir/bin" >> "$GITHUB_PATH"; fi
if [[ -n "${GITHUB_ENV:-}" ]]; then
  printf 'CARGO_HOME=%s\nRUSTUP_HOME=%s\nRUSTUP_TOOLCHAIN=%s\nHYPRIAL_CARGO=%s\n' \
    "$CARGO_HOME" "$RUSTUP_HOME" "$RUSTUP_TOOLCHAIN" "$HYPRIAL_CARGO" >> "$GITHUB_ENV"
fi
printf 'e2e-rust: ready version=%s host=%s cargo=%s\n' "$RUST_VERSION" "$host" "$HYPRIAL_CARGO"
"$tool_dir/bin/rustc" --version
"$HYPRIAL_CARGO" --version
