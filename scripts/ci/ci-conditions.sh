#!/usr/bin/env bash
# Print one best-effort CI load reading without changing the job result.
# Linux readings describe the OrbStack VM visible from its job container.
# macOS readings describe the native macos-host runner. The two scopes and
# their instruments are deliberately explicit and are not directly comparable.
#
# Phases: start | end | artifact | mover.
# `mover` is not a load reading: it is the release-track audit's ordering key.
# The step that pushes `refs/tags/internal` calls it after a successful push, so
# the push time lives in the run's own conditions file (which the artifact tail
# uploads) instead of being inferred from a dev-push task's `updated_at` -- an
# inference that was only valid while the mover happened to be the unit job's
# last step. Missing-value semantics are stated at the `mover` block below.

set +e

phase=${1:-unknown}
job=${GITHUB_JOB:-unknown}
run_id=${GITHUB_RUN_ID:-unknown}
at=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
[ -n "$at" ] || at=unavailable

fields="phase=${phase} job=${job} run=${run_id}"
errors=""

safe_quoted() {
  printf '%s' "$1" | tr '\n\r\t\"' '____'
}

safe_token() {
  printf '%s' "$1" | tr '\n\r\t\" ' '_____'
}

add_error() {
  item=$(safe_token "$1")
  if [ -n "$errors" ]; then
    errors="${errors},${item}"
  else
    errors=$item
  fi
}

read_one_line() {
  file=$1
  value=$(cat "$file" 2>/dev/null)
  rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$value" ]; then
    return 1
  fi
  printf '%s' "$value"
}

psi_avg10() {
  file=$1
  kind=$2
  line=$(grep "^${kind} " "$file" 2>/dev/null | head -1)
  [ -n "$line" ] || return 1
  value=$(printf '%s\n' "$line" | awk '{for (i=1; i<=NF; i++) if ($i ~ /^avg10=/) {sub(/^avg10=/, "", $i); print $i; exit}}')
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

os_name=${CI_CONDITIONS_OS_NAME:-$(uname -s 2>/dev/null)}
case "$os_name" in
  Linux)
    fields="${fields} scope=vm-container instrument=loadavg,psi,meminfo,cgroup"
    proc_root=${CI_CONDITIONS_PROC_ROOT:-/proc}
    loadavg_path=${CI_CONDITIONS_LOADAVG_PATH:-${proc_root}/loadavg}
    meminfo_path=${CI_CONDITIONS_MEMINFO_PATH:-${proc_root}/meminfo}
    pressure_root=${CI_CONDITIONS_PRESSURE_ROOT:-${proc_root}/pressure}

    if loadavg=$(read_one_line "$loadavg_path"); then
      fields="${fields} loadavg=\"$(safe_quoted "$loadavg")\""
    else
      fields="${fields} loadavg=unavailable"
      add_error "loadavg_read_failed:${loadavg_path}"
    fi

    nproc_value=$(nproc 2>/dev/null)
    if [ -n "$nproc_value" ]; then
      fields="${fields} nproc=${nproc_value}"
    else
      fields="${fields} nproc=unavailable"
      add_error "nproc_read_failed"
    fi

    mem_available=$(awk '/^MemAvailable:/ {print $2; exit}' "$meminfo_path" 2>/dev/null)
    mem_total=$(awk '/^MemTotal:/ {print $2; exit}' "$meminfo_path" 2>/dev/null)
    if [ -n "$mem_available" ] && [ -n "$mem_total" ]; then
      fields="${fields} mem_available_kb=${mem_available} mem_total_kb=${mem_total}"
    else
      fields="${fields} mem_available_kb=unavailable mem_total_kb=unavailable"
      add_error "meminfo_read_failed:${meminfo_path}"
    fi

    psi_fields=""
    psi_ok=1
    for resource in cpu memory io; do
      file="${pressure_root}/${resource}"
      some=$(psi_avg10 "$file" some)
      if [ -n "$some" ]; then
        psi_fields="${psi_fields} psi_${resource}_some_avg10=${some}"
      else
        psi_ok=0
      fi
      if [ "$resource" = cpu ]; then
        # CPU full is pinned, not read. Older kernels omit the line entirely and
        # newer ones (observed on 6.14) emit one that is structurally always 0, so
        # reading it would let the runner's kernel decide whether
        # `psi_cpu_full_avg10` is a number or the word `unavailable` -- and
        # tests/tooling/ci/execution/test_ci_conditions.py asserts the documented field set on the
        # reading host. Keep the field present, keep it `unavailable`, and keep
        # kernel variance away from psi_ok.
        psi_fields="${psi_fields} psi_cpu_full_avg10=unavailable"
      else
        full=$(psi_avg10 "$file" full)
        if [ -n "$full" ]; then
          psi_fields="${psi_fields} psi_${resource}_full_avg10=${full}"
        else
          psi_ok=0
        fi
      fi
    done
    if [ "$psi_ok" -eq 1 ]; then
      fields="${fields} psi=available${psi_fields}"
    else
      fields="${fields} psi=unavailable"
    fi

    cgroup_root=${CI_CONDITIONS_CGROUP_ROOT:-/sys/fs/cgroup}
    if [ -r "${cgroup_root}/cgroup.controllers" ]; then
      cpu_max=$(read_one_line "${cgroup_root}/cpu.max")
      nr_throttled=$(awk '/^nr_throttled / {print $2; exit}' "${cgroup_root}/cpu.stat" 2>/dev/null)
      throttled_usec=$(awk '/^throttled_usec / {print $2; exit}' "${cgroup_root}/cpu.stat" 2>/dev/null)
      if [ -n "$cpu_max" ] && [ -n "$nr_throttled" ] && [ -n "$throttled_usec" ]; then
        fields="${fields} cgroup=v2 cpu.max=\"$(safe_quoted "$cpu_max")\" nr_throttled=${nr_throttled} throttled_usec=${throttled_usec}"
      else
        fields="${fields} cgroup=unavailable"
      fi
    elif [ -r "${cgroup_root}/cpu/cpu.cfs_quota_us" ] || [ -r "${cgroup_root}/cpu,cpuacct/cpu.cfs_quota_us" ]; then
      fields="${fields} cgroup=v1"
    else
      fields="${fields} cgroup=unavailable"
    fi
    ;;
  Darwin)
    fields="${fields} scope=macos-runner-host instrument=vm.loadavg,memory_pressure,hw.memsize"
    sysctl_bin=${CI_CONDITIONS_SYSCTL_BIN:-sysctl}
    memory_pressure_bin=${CI_CONDITIONS_MEMORY_PRESSURE_BIN:-memory_pressure}

    if loadavg=$("$sysctl_bin" -n vm.loadavg 2>/dev/null) && [ -n "$loadavg" ]; then
      fields="${fields} loadavg=\"$(safe_quoted "$loadavg")\""
    else
      fields="${fields} loadavg=unavailable"
      add_error "vm.loadavg_read_failed:${sysctl_bin}"
    fi

    if nproc_value=$("$sysctl_bin" -n hw.ncpu 2>/dev/null) && [ -n "$nproc_value" ]; then
      fields="${fields} nproc=${nproc_value}"
    else
      fields="${fields} nproc=unavailable"
      add_error "hw.ncpu_read_failed:${sysctl_bin}"
    fi

    if mem_total_bytes=$("$sysctl_bin" -n hw.memsize 2>/dev/null) && [ -n "$mem_total_bytes" ]; then
      fields="${fields} mem_total_kb=$((mem_total_bytes / 1024))"
    else
      fields="${fields} mem_total_kb=unavailable"
      add_error "hw.memsize_read_failed:${sysctl_bin}"
    fi

    mem_free_percent=$("$memory_pressure_bin" -Q 2>/dev/null | awk -F': ' '/System-wide memory free percentage:/ {sub(/%$/, "", $2); print $2; exit}')
    if [ -n "$mem_free_percent" ]; then
      fields="${fields} mem_free_percent=${mem_free_percent}"
    else
      fields="${fields} mem_free_percent=unavailable"
      add_error "memory_pressure_read_failed:${memory_pressure_bin}"
    fi
    fields="${fields} psi=unavailable"
    ;;
  *)
    fields="${fields} scope=unknown instrument=unavailable loadavg=unavailable nproc=unavailable mem_available_kb=unavailable mem_total_kb=unavailable psi=unavailable"
    add_error "unsupported_os:${os_name:-unavailable}"
    ;;
esac

if [ "$phase" = mover ]; then
  # The mover records its OWN push time. Before this, the audit that decides
  # where `refs/tags/internal` points read a dev-push task's `updated_at` as the
  # moment the tag was actually pushed, and ordered the green dev-push runs by
  # it. That reading is correct only while the mover is the unit job's LAST
  # step: the condition tail added 2026-09-14 is `if: always()` and sits after
  # it, so the ordering silently absorbed the tail's duration.
  #
  # ⚠️ MISSING PUSH TIME: the tail is `if: always()`, so when the mover did not
  # run this record is simply absent. "Absent" is NOT "the push did not happen"
  # and NOT "the audit read the file before the upload" -- three different
  # states with three different repairs. The audit must therefore REFUSE to
  # order without this record (fail closed), never skip the run: skipping would
  # silently re-order the green dev pushes around a value it never saw.
  mover_ref=${CI_MOVER_REF:-refs/tags/internal}
  fields="phase=mover job=${job} run=${run_id} ref=$(safe_token "$mover_ref") outcome=pushed"
  errors=""
fi

if [ "$phase" = artifact ]; then
  add_error "artifact-upload-failed"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf -- '- artifact upload failed/timeout: %s\n' "$job" >> "$GITHUB_STEP_SUMMARY" || true
  fi
  # Where the upload client was sent. Only macos-host jobs fail here ("Create
  # Artifact Container failed: socket hang up") while container jobs upload
  # fine, and the runner reaches Forgejo over a different origin than the
  # containers do -- but nothing recorded the origin the server handed this
  # job, so the failure could not be told apart from a network fault. Scheme,
  # host and port only: no path, no query, and never ACTIONS_RUNTIME_TOKEN.
  runtime_url=${ACTIONS_RUNTIME_URL:-}
  if [ -n "$runtime_url" ]; then
    runtime_scheme=${runtime_url%%://*}
    runtime_rest=${runtime_url#*://}
    runtime_authority=${runtime_rest%%/*}
    # Strip userinfo before splitting query/fragment: a malformed credential
    # such as ``user:p?x@host`` would otherwise be cut at ? and leave the
    # credential fragment in the origin.  Same boundary as
    # netretry._URL_CREDENTIAL; the last @ in the authority separates userinfo.
    runtime_authority=${runtime_authority##*@}
    runtime_authority=${runtime_authority%%\?*}
    runtime_authority=${runtime_authority%%#*}
    runtime_origin="${runtime_scheme}://${runtime_authority}"
  else
    runtime_origin=unset
  fi
  fields="${fields} runtime_origin=$(safe_token "$runtime_origin")"
fi

render_line() {
  rendered=$fields
  if [ -n "$errors" ]; then
    rendered="${rendered} error=${errors}"
  fi
  printf 'CI_CONDITIONS %s at=%s' "$rendered" "$at"
}

line=$(render_line)
if [ "$phase" = start ] || [ "$phase" = end ] || [ "$phase" = mover ]; then
  job_file=$(printf '%s' "$job" | sed 's/[^A-Za-z0-9_.-]/_/g')
  conditions_file=${CI_CONDITIONS_FILE:-${RUNNER_TEMP:-/tmp}/ci-conditions-${job_file}.txt}
  if ! printf '%s\n' "$line" >> "$conditions_file" 2>/dev/null; then
    add_error "conditions_file_write_failed:${conditions_file}"
    line=$(render_line)
  fi
fi
printf '%s\n' "$line"
exit 0
