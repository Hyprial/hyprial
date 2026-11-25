#!/usr/bin/env bash
# Bounded retry for CI network fetches (owner 2026-11-15, CI load).
#
# Decision record (file header; no inline comments by repository rule):
# - Measured 2026-11-15: a gate-python / pytest-shard job failed in 77 s because
#   the pinned Rust toolchain fetch hit a TLS handshake EOF on
#   static.rust-lang.org (channel-rust-1.93.1.toml.sha256); the same step passed
#   minutes later. curl --retry does not retry a TLS handshake failure, so the
#   retry is explicit and visible.
# - Sourced, never executed. ci_retry LABEL COMMAND... runs COMMAND at most 3
#   times with a short backoff (CI_RETRY_BACKOFF, default "2 5" seconds before
#   attempts 2 and 3) and returns the last exit code after the final failure.
# - Wrap ONLY the network fetch itself. Checksum verification, version probes
#   and path validation stay outside so that a wrong digest or version is a
#   single immediate failure, never retried into a pass.
# - Every failed attempt and the exhausted case print one CI_RETRY line on
#   stderr; a first-try success prints nothing.
#
# uv environment creation (owner 2026-11-17, CI load):
# - Measured 2026-11-17 03:10-04:30 UTC: 57 jobs failed in their Python
#   environment step because the runner's egress to pypi.org (simple index and
#   wheels) and to GitHub release downloads was intermittently failing, with
#   successes and failures interleaved in the same minutes. uv's default of 3
#   retries gives up in about 29 s; the blips lasted longer.
# - Two layers, both bounded. Inside uv, UV_HTTP_RETRIES=10 is set next to
#   UV_HTTP_TIMEOUT in every workflow (uv 0.11.7: exponential backoff with
#   jitter, about 62-66 s of waiting in total for 10 retries against a refused
#   connection, measured with the pinned 0.11.7 binary; it also covers the
#   CPython download). Outside uv, ci_retry_uv re-runs one idempotent uv
#   command up to 3 times with CI_RETRY_BACKOFF defaulting to "10 30".
# - ci_retry_uv wraps only commands whose job is to fetch and materialise an
#   environment from a locked resolution (uv sync --locked, uv python install,
#   uv pip install of a pinned overlay) or to warm the cache for a later
#   verification command. It never wraps a test, lint or gate command, and it
#   adds no flag: what is installed is exactly what the unwrapped command
#   installs.
# - uv exits 1 or 2 for network and for non-network failures alike (measured
#   with 0.11.7: a refused connection exits 1 while downloading and 2 while
#   validating a changed lock; an unresolvable requirement exits 1), so the
#   exit code cannot separate them. A non-network failure such as a lock
#   mismatch is therefore retried too, at most 2 extra times, and each attempt
#   is reported by a CI_RETRY line; the cost is about 40 s of backoff.
# - Offline-first (--offline, then online on a miss) was rejected: build
#   requirements such as hatchling are not in uv.lock, so an offline run
#   resolves them from whatever index metadata the shared cache holds and can
#   select a different build backend than the online run would.
ci_retry() {
  local label="$1"
  shift
  local attempts=3 attempt=1 rc delay
  local -a delays
  read -r -a delays <<< "${CI_RETRY_BACKOFF:-2 5}"
  while :; do
    if "$@"; then rc=0; else rc=$?; fi
    if [[ "$rc" -eq 0 ]]; then
      if [[ "$attempt" -gt 1 ]]; then
        printf 'CI_RETRY label=%s attempt=%d/%d status=ok\n' "$label" "$attempt" "$attempts" >&2
      fi
      return 0
    fi
    if [[ "$attempt" -ge "$attempts" ]]; then
      printf 'CI_RETRY label=%s exhausted attempts=%d rc=%d\n' "$label" "$attempts" "$rc" >&2
      return "$rc"
    fi
    delay="${delays[$((attempt - 1))]:-5}"
    printf 'CI_RETRY label=%s attempt=%d/%d status=failed rc=%d next_in=%ss\n' "$label" "$attempt" "$attempts" "$rc" "$delay" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

ci_retry_uv() {
  CI_RETRY_BACKOFF="${CI_RETRY_BACKOFF:-10 30}" ci_retry "$@"
}
