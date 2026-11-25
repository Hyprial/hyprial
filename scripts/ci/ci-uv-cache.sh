#!/usr/bin/env bash
# Choose UV_CACHE_DIR for a job and say, on one greppable line, why.
#
# Persistent: "$CI_CACHE_ROOT/uv-wheels" on the runner's hb-ci-cache volume,
# so `uv sync` stops re-downloading every locked wheel through the host proxy
# in every job (the per-job tmpfs cache was always empty; the locked Zenoh
# wheel download is what timed out, main-integration diagnosis 2026-10-04).
# NOT "$CI_CACHE_ROOT/uv": ci-ensure-uv.sh keeps the uv release archives there.
#
# The volume is used only when ALL hold, each checked here rather than assumed
# from the variable (a repo comment or an env var does not prove a mount):
#   1. CI_CACHE_ROOT is set and is (inside) a mount point listed in /proc/mounts
#      other than the container root -- an unmounted directory is a fresh
#      container layer and would silently cache nothing;
#   2. a probe file can be created and removed there;
# otherwise the cache falls back to the job tmpfs exactly as before, with a
# ::warning:: so the fallback is visible.
#
# Evidence line (the main integrator reads real job logs; the Forgejo 15 API
# has no log endpoint):
#   CI_UV_CACHE dir=<path> persistent=yes|no mount=<target|none> writable=yes|no entries_before=<n>
# entries_before > 0 on a fresh job is the cross-job visibility witness.
#
# Usage: ci-uv-cache.sh <fallback-dir>; appends UV_CACHE_DIR / UV_LINK_MODE to $GITHUB_ENV.
# Test seam: CI_UV_CACHE_PROC_MOUNTS replaces /proc/mounts.
set -uo pipefail

fallback="${1:?usage: ci-uv-cache.sh <fallback-dir>}"
mounts="${CI_UV_CACHE_PROC_MOUNTS:-/proc/mounts}"
root="${CI_CACHE_ROOT:-}"
mount_target=none
writable=no
dir="$fallback"
persistent=no

if [[ -n "$root" && -d "$root" ]]; then
  # Longest mount target that is the cache root or one of its parents, excluding "/".
  real="$(cd "$root" && pwd -P)"
  while read -r _ target _; do
    [[ "$target" == "/" ]] && continue
    if [[ "$real" == "$target" || "$real" == "$target"/* ]]; then
      if (( ${#target} > ${#mount_target} )) || [[ "$mount_target" == none ]]; then
        mount_target="$target"
      fi
    fi
  done < "$mounts"
  if [[ "$mount_target" != none ]]; then
    candidate="$root/uv-wheels"
    if mkdir -p "$candidate" 2>/dev/null && probe="$(mktemp "$candidate/.probe.XXXXXX" 2>/dev/null)"; then
      rm -f "$probe"
      writable=yes
      dir="$candidate"
      persistent=yes
    fi
  fi
fi

if [[ "$persistent" != yes ]]; then
  mkdir -p "$dir"
  echo "::warning::uv cache is NOT persistent (CI_CACHE_ROOT='${root}' mount=${mount_target} writable=${writable}); using ${dir}"
fi
entries="$(find "$dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')"
echo "CI_UV_CACHE dir=${dir} persistent=${persistent} mount=${mount_target} writable=${writable} entries_before=${entries}"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "UV_CACHE_DIR=${dir}"
    # The venv is on the checkout mount: hardlinks across filesystems cannot work.
    echo "UV_LINK_MODE=copy"
  } >> "$GITHUB_ENV"
fi
