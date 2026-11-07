# N2 migration evidence

Scope: node `migrate` only. No real agent home was used; all behavior below
was exercised against a throwaway directory under `/private/tmp`.

## Commands and readings

Initial throwaway home:

```text
/private/tmp/agent-home-migrate.tRdS3m/
├── e2e/run.json
├── records/note.md
└── state/home/state.txt
```

Dry run command (default mode):

```sh
./.venv/bin/python scripts/agent-home-migrate.py /private/tmp/agent-home-migrate.tRdS3m
```

Reading: JSON reported `dry_run=true`, `changed=true`, three planned moves,
and creates for `persona/` and `persona/skills/`. The home contents were
unchanged.

Apply command:

```sh
./.venv/bin/python scripts/agent-home-migrate.py /private/tmp/agent-home-migrate.tRdS3m --yes
```

Reading: JSON reported `dry_run=false`, `changed=true`; the three legacy
directories moved to `sandbox/home`, `workspace/notes`, and `workspace/e2e`.

Idempotency command:

```sh
./.venv/bin/python scripts/agent-home-migrate.py /private/tmp/agent-home-migrate.tRdS3m --yes
```

Reading: JSON reported `changed=false`, with no planned moves or creates.

Resulting paths included:

```text
persona/
persona/skills/
sandbox/home/state.txt
workspace/notes/note.md
workspace/e2e/run.json
```

Focused tests and checks:

```sh
./.venv/bin/python -m pytest -q tests/scripts/test_agent_home_migrate.py tests/scripts/test_agent_home_inventory.py
./.venv/bin/python -m compileall -q scripts/agent-home-migrate.py scripts/agent-home-inventory.py
./.venv/bin/ruff check scripts/agent-home-migrate.py scripts/agent-home-inventory.py tests/scripts/test_agent_home_migrate.py tests/scripts/test_agent_home_inventory.py
```

Reading: `8 passed`; compileall passed; Ruff reported `All checks passed!`.
The tests cover dry-run non-mutation, apply, second-run `changed=false`, and
preflight conflict rejection plus injected mid-apply failure without partial
mutation.

# N3 graveyard destruction evidence

Scope: node `destroy` only. Production agent homes and credentials were not
modified. The live exercise used a copy of the current workflow worker home
under `/private/tmp`; `secrets/` was excluded from the copy and the copy was
given synthetic secret files.

## Behavioral RED -> GREEN

Initial RED, before `scripts/agent-home-destroy.py` existed:

```sh
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run pytest -q tests/scripts/test_agent_home_destroy.py
```

Reading: collection failed with `FileNotFoundError` for
`scripts/agent-home-destroy.py` (exit 2). An earlier invocation using the user
uv cache was rejected by the filesystem sandbox and was not counted as RED.

Security regression RED: a synthetic secret file named `secrets/.git` was
incorrectly counted as a worktree (`worktrees=2`, expected `1`; one failed).
After excluding secret bytes from external-reference parsing while still
naming them in the receipt, the focused result was:

```text
7 passed, 2 warnings in 0.05s
```

The warnings are dependency deprecations in `lark_oapi`; no test was skipped.

## Real-home-copy exercise

Source structure copied from (secret content excluded):

```text
/Users/bobadmin/.hyprial/agents/wf-ab8ac52b3f644d1fa05b14220937401c-6f61ec5c7a0a
```

Isolated copy:

```text
/private/tmp/agent-home-destroy-real.ut3QRK/agents/worker
```

The copy retained the real `config/` and `state/home-receipt.json`; it was
augmented with synthetic `state/cache/transient.json`,
`secrets/provider/token`, and `secrets/session.env`.

Dry run (no `--yes`) reported the named plan without creating `destroyed/`:

```sh
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run python scripts/agent-home-destroy.py \
  /private/tmp/agent-home-destroy-real.ut3QRK/agents/worker \
  --who agent:workflow-destroy --why workflow-N3-real-home-copy
```

Apply:

```sh
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run python scripts/agent-home-destroy.py \
  /private/tmp/agent-home-destroy-real.ut3QRK/agents/worker \
  --who agent:workflow-destroy --why workflow-N3-real-home-copy --yes
```

Final target:

```text
/private/tmp/agent-home-destroy-real.ut3QRK/agents/destroyed/worker@2026-10-23T07:18:30Z
```

## Field-by-field receipt check

`jq -e` checked the final `DESTROYED.json` as one predicate and returned
`true` (exit 0):

- `who`: `agent:workflow-destroy`
- `when`: `2026-10-23T07:18:30Z`
- `why`: `workflow-N3-real-home-copy`
- `what.source`: the isolated `.../agents/worker` copy
- `what.target`: the timestamped `.../agents/destroyed/worker@...` directory
- `what.subdirs_moved`: `["config"]`
- `what.files_moved`: `[]`
- `what.state_files_moved`: `["state/home-receipt.json"]`
- `what.state_discarded`: `["state/cache", "state/cache/transient.json"]`
- `what.secrets_deleted`: `["secrets/provider", "secrets/provider/token", "secrets/session.env"]`
- `external_refs`: `{"symlinks":0,"total":0,"venv":0,"worktrees":0}`

The source home no longer existed. The target contained only `config/`,
`state/home-receipt.json`, `DESTROYED.json`, and `COMPLETE`. `COMPLETE`
contained the same UTC timestamp. An `rg` scan for both synthetic secret
values returned no matches (exit 1), while their names remained in the
receipt.

## Focused and structural gates

```sh
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run pytest -q tests/scripts/test_agent_home_inventory.py \
  tests/scripts/test_agent_home_migrate.py tests/scripts/test_agent_home_destroy.py
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run ruff check scripts/agent-home-destroy.py \
  tests/scripts/test_agent_home_destroy.py
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
```

Readings: all three script suites `15 passed`; Ruff `All checks passed!`;
both structural gates `status=PASS` with zero violations. No contract field or
agent-operation skill changed (`Contract-Impact: none`; `Skill-Impact: none`).

## Broad-suite status

The required broad command was started exactly as follows and collected
15,137 tests:

```sh
UV_CACHE_DIR=/private/tmp/agent-home-destroy-uv-cache TMPDIR=/private/tmp \
  uv run --with libcst==1.9.0 pytest tests/
```

Status: **FAIL (partial run); remainder NOT_RUN**. Before 31%, failures and
errors were already reported in existing Lark fixtures, daemon transport/MCP
lifecycle, Codex native-home, Pi extension, home lease/identity resolver, IPC
cost, lifecycle wait, links wiring, log migration, and maintenance tests.
The run then entered repeated roughly 50-second network/orgfs cases and was
interrupted to preserve the fixed workflow deadline; final exit was 130, so no
complete pass/fail count is claimed. None of the reported failing modules
imports or exercises the standalone N3 script.

After the interruption, the three script suites were rerun: `15 passed, 2
warnings in 0.09s`; `uv run ruff check .` again reported `All checks passed!`.
A read-only process-list check was unavailable under the filesystem sandbox
(`ps: operation not permitted`); no cleanup command was attempted.
