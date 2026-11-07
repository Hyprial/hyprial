#!/usr/bin/env bash
# Merge preconditions for the owner-review flow (A base freshness + B merged-tree
# verification). It SPEAKS: any step that did not actually run exits non-zero with
# an explicit A-NOT-RUN/B-NOT-RUN marker — "the command never ran" must never look
# like "the command ran and passed" -- and, because a caller who reads a not-run as
# a red chases a failure that never happened, it must not look like "the command
# ran and failed" either.  pytest's own not-run family (interrupted / internal
# error / usage error / nothing collected = exit 2/3/4/5) is therefore reported as
# B-NOT-RUN, never as B-FAIL; only exit 1 is a red.
#
# B1 exists because the integration-batch light plan SELECTS tests by scope, so
# tree-level gates (terminology lint, native tripwires, structural/boundary gates)
# can go unrun on a green CI. B1 runs them explicitly on the merged tree.
#
# A freshness is measured against the branch this work will actually be merged
# into: $PREMRECHECK_BASE, defaulting to origin/dev. Name it explicitly (env var)
# when the target is not dev. A transient integration branch is never a safe
# default -- a default naming a deleted branch turns every run into A-NOT-RUN.
#
# Usage: scripts/premerge-check.sh <worktree> [pytest paths for B2...]
# Last line: PRECHECK=PASS or PRECHECK=FAIL; exit 0/1 (2 = could not run A).
set -uo pipefail

WT="${1:?usage: premerge-check.sh <worktree> [pytest paths...]}"; shift || true
cd "$WT" || { echo "A-NOT-RUN (worktree missing: $WT)"; exit 2; }

fail=0
run() { # run <label> <cmd...>; distinguishes not-run from red
  local label="$1"; shift
  local log="/tmp/pm-${label}.log"
  "$@" >"$log" 2>&1
  local rc=$?
  if [ $rc -ne 0 ] && grep -qiE 'error: (failed to spawn|argument|no such option)|^Usage:' "$log"; then
    echo "B-NOT-RUN ($label: invocation failed; see $log)"; fail=1; return
  fi
  case "$rc" in
    2|3|4|5) # pytest: interrupted / internal error / usage error / nothing collected
      echo "B-NOT-RUN ($label: did not run, rc=$rc; see $log)"; fail=1; return ;;
  esac
  [ $rc -eq 0 ] || { echo "B-FAIL ($label rc=$rc; see $log)"; fail=1; return; }
}

BASE_REF="${PREMRECHECK_BASE:-origin/dev}"
echo "== A: branch contains current base HEAD (${BASE_REF}) =="
git fetch -q origin "${BASE_REF#origin/}" \
  || { echo "A-NOT-RUN (fetch failed: ${BASE_REF})"; exit 2; }
INTG=$(git rev-parse --verify -q "$BASE_REF") \
  || { echo "A-NOT-RUN (no such ref after fetch: ${BASE_REF})"; exit 2; }
if git merge-base --is-ancestor "$INTG" HEAD; then
  echo "A-OK (base $INTG is an ancestor of $(git rev-parse --short HEAD))"
else
  echo "A-FAIL: run: git merge $BASE_REF"; fail=1
fi

echo "== B1: tree-level gates (the ones the light plan can skip) =="
run struct uv run --no-sync python scripts/ci/check_structural_limits.py
run bounds uv run --no-sync python scripts/ci/check_module_boundaries.py
run lint-term uv run --with pytest python -m pytest tests/tooling/architecture/lint/test_provider_term_lint.py -q
run tripwires uv run --with pytest python -m pytest tests/integration/native_harnesses/test_native_resident_tripwires.py -q
[ $fail -eq 0 ] && echo "B1-OK (structure, boundaries, provider-term lint, native tripwires)"

echo "== B2: targeted tests =="
if [ $# -gt 0 ]; then
  run tests uv run --with pytest python -m pytest "$@" -q
  [ $fail -eq 0 ] && echo "B2-OK ($* )"
else
  echo "B-NOT-RUN (no pytest paths given — B is incomplete by construction)"; fail=1
fi

if [ $fail -eq 0 ]; then echo "PRECHECK=PASS"; exit 0; else echo "PRECHECK=FAIL"; exit 1; fi
