#!/usr/bin/env bash
# Source at the start of a CI step; preserve its exit status, including failures.
set -euo pipefail
ci_stage=${1:?stage name required}
ci_started=$SECONDS
ci_snapshot() {
  local metric
  for metric in /proc/loadavg /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.current /sys/fs/cgroup/io.stat; do
    if [[ -r "$metric" ]]; then
      printf 'CI_RESOURCE %s %s\n' "$ci_stage" "$metric"
      cat "$metric" || true
    fi
  done
}
ci_finish() {
  local result=$? elapsed=$((SECONDS - ci_started))
  trap - EXIT
  printf 'CI_TIMING stage=%s seconds=%s exit=%s\n' "$ci_stage" "$elapsed" "$result"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '\n- `%s`: %ss, exit %s\n' "$ci_stage" "$elapsed" "$result" >> "$GITHUB_STEP_SUMMARY" || true
  fi
  ci_snapshot
  exit "$result"
}
trap ci_finish EXIT
ci_snapshot
