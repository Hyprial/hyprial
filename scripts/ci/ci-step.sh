#!/usr/bin/env bash
# Source at the start of a CI step; preserve its exit status, including failures.
set -euo pipefail
ci_stage=${1:?stage name required}
ci_started=$SECONDS
# RAM-disk peak (infra-ops, 2026-09-28): unit jobs put pytest basetemp, TMPDIR
# and the uv cache on the 2 GiB /ci-tmpfs. An ENOSPC there fails a test in a
# way that looks like a flake, so every step reports the highest usage it saw
# (sampled every 5 s) next to its timing. Absent mount => nothing is sampled.
ci_tmpfs=${CI_TMPFS_PATH:-/ci-tmpfs}
ci_tmpfs_log=""
ci_tmpfs_sampler=""
if awk -v m="$ci_tmpfs" '$2 == m && $3 == "tmpfs" { found = 1 } END { exit !found }' /proc/mounts 2>/dev/null \
  || [[ -n "${CI_TMPFS_PATH:-}" && -d "$ci_tmpfs" ]]; then
  # A diagnostic must never fail the step it measures.
  ci_tmpfs_log=$(mktemp "${TMPDIR:-/tmp}/ci-tmpfs-peak.XXXXXX" 2>/dev/null) || ci_tmpfs_log=""
fi
if [[ -n "$ci_tmpfs_log" ]]; then
  (
    set +e
    while :; do
      df -Pk "$ci_tmpfs" 2>/dev/null | awk 'NR == 2 { print $3, $2 }'
      sleep "${CI_TMPFS_INTERVAL:-5}"
    done
  ) >"$ci_tmpfs_log" 2>/dev/null &
  ci_tmpfs_sampler=$!
fi
ci_tmpfs_report() {
  [[ -n "$ci_tmpfs_sampler" ]] || return 0
  kill "$ci_tmpfs_sampler" 2>/dev/null || true
  wait "$ci_tmpfs_sampler" 2>/dev/null || true
  df -Pk "$ci_tmpfs" 2>/dev/null | awk 'NR == 2 { print $3, $2 }' >>"$ci_tmpfs_log" || true
  awk -v stage="$ci_stage" -v path="$ci_tmpfs" '
    { n++; if ($1 + 0 > peak) peak = $1 + 0; size = $2 }
    END { if (n) printf "CI_TMPFS stage=%s path=%s peak_kb=%d size_kb=%d samples=%d\n", stage, path, peak, size, n }
  ' "$ci_tmpfs_log" || true
  rm -f "$ci_tmpfs_log"
}
ci_snapshot() {
  local metric
  for metric in /proc/loadavg /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.current /sys/fs/cgroup/io.stat; do
    if [[ -r "$metric" ]]; then
      printf 'CI_RESOURCE %s %s\n' "$ci_stage" "$metric"
      cat "$metric" || true
    fi
  done
}
ci_cgroup_root=${CI_CGROUP_ROOT:-/sys/fs/cgroup}
ci_oom_start=""
ci_cgroup_value() {
  local value=""
  [[ -r "$1" ]] && IFS= read -r value <"$1" 2>/dev/null || true
  if [[ "$value" == max || "$value" =~ ^[0-9]+$ ]]; then
    printf '%s' "$value"
  else
    printf '%s' "-"
  fi
}
ci_cgroup_oom_kill() {
  local file key count rest
  for file in "$ci_cgroup_root/memory.events" "$ci_cgroup_root/memory/memory.oom_control"; do
    [[ -r "$file" ]] || continue
    while read -r key count rest; do
      if [[ "$key" == oom_kill && "$count" =~ ^[0-9]+$ && -z "$rest" ]]; then
        printf '%s' "$count"
        return 0
      fi
    done <"$file" 2>/dev/null || true
  done
  printf '%s' "-"
}
ci_memory() {
  local phase=$1 max current peak oom delta=""
  max=$(ci_cgroup_value "$ci_cgroup_root/memory.max")
  [[ "$max" == - ]] && max=$(ci_cgroup_value "$ci_cgroup_root/memory/memory.limit_in_bytes")
  current=$(ci_cgroup_value "$ci_cgroup_root/memory.current")
  [[ "$current" == - ]] && current=$(ci_cgroup_value "$ci_cgroup_root/memory/memory.usage_in_bytes")
  peak=$(ci_cgroup_value "$ci_cgroup_root/memory.peak")
  [[ "$peak" == - ]] && peak=$(ci_cgroup_value "$ci_cgroup_root/memory/memory.max_usage_in_bytes")
  oom=$(ci_cgroup_oom_kill)
  if [[ "$phase" == start ]]; then
    ci_oom_start=$oom
  elif [[ "$oom" != - && "${ci_oom_start:--}" != - ]]; then
    delta=" oom_kill_delta=$((oom - ci_oom_start))"
  fi
  [[ "$max$current$peak$oom" == "----" ]] && return 0
  printf 'CI_MEMORY stage=%s phase=%s max=%s current=%s peak=%s oom_kill=%s%s\n' \
    "$ci_stage" "$phase" "$max" "$current" "$peak" "$oom" "$delta"
}
ci_finish() {
  local result=$? elapsed=$((SECONDS - ci_started))
  trap - EXIT
  ci_memory end || true
  printf 'CI_TIMING stage=%s seconds=%s exit=%s\n' "$ci_stage" "$elapsed" "$result"
  ci_tmpfs_report
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '\n- `%s`: %ss, exit %s\n' "$ci_stage" "$elapsed" "$result" >> "$GITHUB_STEP_SUMMARY" || true
  fi
  ci_snapshot
  exit "$result"
}
trap ci_finish EXIT
ci_snapshot
ci_memory start || true
