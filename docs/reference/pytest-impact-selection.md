# Pytest impact selection

The public merge gate uses `scripts/ci/pytest_impact.py` to choose tracked test files for pull requests. The selector reads the checked-out two-parent merge commit and compares its first parent with the merge commit. It accepts the pull-request head SHA from `pull_request.head.sha` and the target branch from `pull_request.base.ref`.

Push, schedule, workflow-dispatch, malformed pull-request data, `main`, and `release/**` targets use the complete tracked test set. Any `src/**` change, environment-sensitive script, workflow gate input, test support or fixture input, dependency lock input, or unclassified path also uses the complete set.

Unreferenced `docs/**/*.md` changes map to `tests/tooling/`. A documentation path is referenced when its full path or basename occurs in the runtime corpus. The corpus covers tests, active scripts, contract files, source Python with tokenizer comments removed, `.github`, `pyproject.toml`, and `GLOSSARY.md`. Scan failures use the complete suite.

Pure test changes are selected conservatively. An unreferenced test module may run alone. A helper, fixture, deleted module, referenced module, unsafe dynamic import, unreadable source, or cross-directory dependency expands the selection through the reverse reference closure. Any Python change adds the six cross-repository guard tests. A missing guard uses the complete suite.

The selector enumerates tests with `git ls-files -z` and validates every returned path as a regular non-symlink file. `--plan-only` prints the plan. `--emit-shard K/N --out PATH --meta PATH` writes one path per line and a controlled metadata file. Selected plans of 16 files or fewer place all files in shard 1. Larger selected plans and full plans use zero-based round-robin assignment with `i % N == K - 1`.

The gate validates metadata before reading it, independently counts tracked tests for full plans, rejects empty full shards, and accepts an empty selected shard only when the plan has at least one and at most 16 tests and the shard index is at least 2. Such a shard prints `PYTEST_SHARD_EMPTY` and exits successfully. Metadata contains exactly six controlled keys, positive decimal plan and shard indexes, a non-negative decimal shard count, and a 64-character lowercase SHA-256 digest.

S7 has a read-only replay entrypoint for a fixed selector snapshot and commit cohort. Run `uv run --no-project --python 3.12 python scripts/ci/replay_pytest_selector.py` from the repository root. The cohort is stored in `scripts/ci/pytest-selector-cohort.json`, and each commit's changed paths are passed to `plan_paths`, the same final path selector used after the pull-request merge comparison. The replay extracts snapshot `22eb4003760a4af0cac43db5030c7dc8cb748921` into a temporary directory, emits commit-level reasons and selected counts plus cohort summaries, and never reads the removed `scripts/ci/pytest-durations.json` file.
