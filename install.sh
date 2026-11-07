#!/bin/sh
# Public wheel installer. HYPRIAL_DL_BASE is reserved for isolated tests.
set -eu
fail() { printf 'hyprial install: %s\n' "$*" >&2; exit 1; }
track=${HYPRIAL_TRACK-stable}
case "$track" in stable|nightly|internal) ;; *) fail "Invalid HYPRIAL_TRACK: $track (use stable, nightly or internal)" ;; esac
base=${HYPRIAL_DL_BASE-https://dl.hyprial.com}
command -v curl >/dev/null 2>&1 || fail 'curl is required.'
if command -v sha256sum >/dev/null 2>&1; then
    hash_tool=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    hash_tool=shasum
else
    fail 'sha256sum or shasum is required.'
fi
system=$(uname -s)
arch=$(uname -m)
case "$system:$arch" in
    Darwin:arm64|Darwin:x86_64) platform="macosx_[0-9]+_[0-9]+_${arch}" ;;
    Linux:x86_64|Linux:aarch64) platform="manylinux_[0-9]+_[0-9]+_${arch}([.]manylinux[0-9]+_${arch})?" ;;
    *) fail "Unsupported platform: $system/$arch" ;;
esac
umask 077
tmp=$(mktemp -d "${TMPDIR:-/tmp}/hyprial-install.XXXXXXXX")
trap 'rm -rf "$tmp"' 0
trap 'exit 1' HUP INT TERM
if ! command -v uv >/dev/null 2>&1; then
    uv_dir=${UV_INSTALL_DIR:-$HOME/.local/bin}
    printf 'Installing uv from https://astral.sh/uv/install.sh into %s (no shell profile changes)\n' "$uv_dir"
    if ! curl --disable --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 10 --max-time 120 --retry 2 --output "$tmp/uv-install.sh" https://astral.sh/uv/install.sh; then
        fail 'uv bootstrap download failed; see https://docs.astral.sh/uv/'
    fi
    UV_UNMANAGED_INSTALL="$uv_dir" sh "$tmp/uv-install.sh" || fail 'uv bootstrap failed'
    PATH="$uv_dir:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || fail 'uv bootstrap finished but uv is unavailable; see https://docs.astral.sh/uv/'
fi
url="${base%/}/wheel/$track"
# Do not use --fail: retain HTTP status to distinguish an unmirrored track.
if ! status=$(curl --disable --silent --show-error --location --connect-timeout 10 \
    --max-time 120 --retry 2 --output "$tmp/SHA256SUMS" --write-out '%{http_code}' "$url/SHA256SUMS"); then
    fail "SHA256SUMS download failed for track $track"
fi
case "$status" in
    200) ;;
    404) fail "SHA256SUMS missing for track $track (HTTP 404); ask infra-ops to mirror this track." ;;
    *) fail "SHA256SUMS download failed for track $track (HTTP $status)" ;;
esac
# Only safe basenames and full platform suffixes are accepted. Count lines, not
# unique names: even a duplicated entry is an ambiguous manifest.
awk -v platform="$platform" '
    { sub(/\r$/, "") }
    NF == 2 && length($1) == 64 && $1 !~ /[^0-9a-fA-F]/ {
        name = $2; sub(/^\*/, "", name)
        if (name ~ ("^hyprial-[0-9][A-Za-z0-9.!+_]*-py3-none-" platform "\\.whl$"))
            print tolower($1), name
    }
' "$tmp/SHA256SUMS" > "$tmp/matches"
count=$(wc -l < "$tmp/matches" | tr -d ' ')
[ "$count" = 1 ] || fail "Expected exactly one wheel for $system/$arch in track $track; found $count"
read -r expected wheel < "$tmp/matches"
encoded_wheel=$(printf '%s' "$wheel" | sed 's/+/%2B/g')
if ! curl --disable --fail --silent --show-error --location --connect-timeout 10 \
    --max-time 300 --retry 2 --output "$tmp/$wheel" "$url/$encoded_wheel"; then
    fail "Wheel download failed: $wheel"
fi
if [ "$hash_tool" = sha256sum ]; then
    digest=$(sha256sum "$tmp/$wheel")
else
    digest=$(shasum -a 256 "$tmp/$wheel")
fi
actual=${digest%% *}
[ "$actual" = "$expected" ] || fail "SHA-256 mismatch: $wheel (nothing installed)"
printf 'Installing verified %s from track %s with uv tool install --force\n' "$wheel" "$track"
uv tool install --force "$tmp/$wheel" || fail 'uv tool install failed'
version=${wheel#hyprial-}
version=${version%%-py3-none-*}
installed_list=$(uv tool list) || fail 'Cannot read installed version with uv tool list'
installed=$(printf '%s\n' "$installed_list" | awk '$1 == "hyprial" { sub(/^v/, "", $2); print $2 }')
[ "$installed" = "$version" ] || fail "Installed version mismatch: selected $version, installed ${installed:-unknown}"
# Preserve the website installer's schema:1 envelope and useful wheel evidence.
# Values below are validated basenames/digests/track/version, never arbitrary URLs.
receipt_dir="${XDG_CONFIG_HOME:-$HOME/.config}/hyprial"
mkdir -p "$receipt_dir" || fail 'Installed, but could not create receipt directory'
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '{"schema":1,"install":{"track":"%s","method":"wheel","installed_at":"%s","selected_version":"%s","installed_version":"%s","wheel_version":"%s","wheel_asset_name":"%s","wheel_sha256":"%s","wheel_sha256_index":"%s","wheel_sha256_match":"true","digest_verification":"pass","degraded":"false"}}\n' \
    "$track" "$installed_at" "$version" "$installed" "$installed" "$wheel" "$actual" "$expected" > "$tmp/install.json"
# Copy to the destination filesystem first, then rename atomically.
receipt_tmp=$(mktemp "$receipt_dir/.install.XXXXXXXX") || fail 'Installed, but could not create receipt'
if ! cp "$tmp/install.json" "$receipt_tmp" || ! mv -f "$receipt_tmp" "$receipt_dir/install.json"; then
    rm -f "$receipt_tmp"
    fail 'Installed, but could not write receipt'
fi
printf 'Installed hyprial %s\nReceipt: %s/install.json\n' "$installed" "$receipt_dir"
printf 'If hyprial is not on PATH, use uv tool update-shell, then open a new terminal.\n'
