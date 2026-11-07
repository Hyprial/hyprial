# PR pytest selection by layer

Since 2026-11-06 a pull request runs only the test directories its change can
break. Every push (dev, main, `intg/**`), manual run and scheduled run, and
every PR into `main` or `release/**`, still runs the entire `tests/` suite, so a
selection can never stand in for release validation. The unit gate, the
four-shard plan, two-worker file grouping, crash/FD guards, lint, compile and
the docs-only fast path are unchanged.

The planner is `scripts/ci/pytest_impact.py` (`plan()` + `layer_targets()`),
called by every `gate-python / pytest-shard (K)` job. Its decision is printed as
`PYTEST_IMPACT {...}` and written to the step summary.

## The rule

The product is five layers whose imports only point downward (measured over
`src/hyprial` on 2026-11-06):

```
kernel  <-  identity  <-  daemon  <-  biz  <-  shell
```

| Changed path | Tests run |
|---|---|
| `src/hyprial/<layer>/**` | `tests/<layer>/`, every layer above it, `tests/integration/` |
| `tests/<layer>/**`, `tests/integration/**`, `tests/tooling/**`, `tests/scripts/**` | that directory only |
| `.forgejo/**`, `scripts/ci/**` | `tests/tooling/` |
| other `scripts/**` | `tests/tooling/`, `tests/scripts/` |
| documentation only | the docs-only gate (no pytest) |
| anything else -- `pyproject.toml`, `uv.lock`, `tests/conftest.py`, `tests/support/**`, `tests/fixtures/**`, `src/hyprial/cli.py`, `src/hyprial/skills/**`, `scripts/ci/pytest_impact.py`, `gui/**`, `contract/**`, `sidecar/**`, ... | the full suite |

A PR's selection is the union over its changed paths; one path that maps to
"full" makes the whole PR full. Any unreadable history, a checkout that is not
the PR head, or an empty match also falls back to the full suite.

## What it replaced

The previous audited consumer map (`scripts/ci/pytest-impact-map.json`, audited
2026-09-17) covered 3 of 1185 source files and switched itself off whenever any
other backend file changed after the audit, so every PR ran the full suite
(8 of 8 sampled shard logs: `mode: full`). PRs into `intg/gui*` were also forced
to full. Both are gone.

## Measured effect

Replayed over the 210 merges into dev between 2026-10-15 and 2026-11-06: 83
would have been layered, 117 still full (mostly `gui/`, `clients/`,
`packaging/`, `contract/`), 10 docs-only; on average 22 % of test files are
skipped per non-docs PR. Extending the table (for example `gui/**` to the GUI
integration tests) is how the number grows -- each new row needs the same
"only these directories can break" argument.
