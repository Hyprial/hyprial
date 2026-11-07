# Refactor toolchain (S1-S6)

Staged, manifest-driven migration tooling for the deep-module reconstruction
(`kernel ← identity ← daemon ← biz ← shell`). Spec:
`docs/design/refactor-c0-and-tooling-2026-10-01.md`. Evidence baseline:
`ad4ce68b6ccd08bd64dd9fdce448dd91a8329f43`
(`docs/evidence/refactor-c0-ad4ce68b-2026-10-01/`).

**Status: toolchain implementation complete through the round-3 review fixes
(PY-01..PY-08); no real migration rule is approved.** Apply refuses anything
that is not explicitly approved; entry points live in `PENDING-DECISIONS.md`,
`preview/` and `../docs/evidence/refactor-python-r3-2026-10-01/launchspec-pilot-draft.json`.

## Layout

| File | Role |
|---|---|
| `run.py` | CLI: `plan` / `apply` / `verify` / `status`; staged simulation, apply gating, idempotency, state |
| `manifest.schema.json` | JSON Schema mirroring the validator (draft 2020-12) |
| `validate_manifest.py` | Authoritative strict validator (structure, per-kind requirements, cross-rule conflicts) |
| `transforms/imports.py` | Binding-aware import rewrites (`import_module`, `import_symbol`) via LibCST scopes |
| `transforms/symbols.py` | `symbol_extract`, `class_member_extract` with explicit dependency manifests |
| `transforms/facades.py` | `facade_export`: tool-managed lazy facades, eager append, conflict refusal |
| `transforms/references.py` | `reference_rule`: module strings, JSON/TOML fields, text spans, `parents[N]` shifts, authority entry migration |
| `transforms/layout.py` | `layout_move`: file/dir moves, consumer sync, WORM hash pinning |
| `transforms/parsing.py`, `transforms/common.py` | LibCST parse cache, module index, virtual workspace, hashing/diffs |
| `verify.py` | Staged verification dispatch (direction/face gates, authority, resources, gated commands) |
| `pyproject.toml`, `uv.lock` | Isolated tool environment, `libcst==1.9.0` pinned, Python 3.12 |
| `preview/` | Real-repo read-only demonstration manifest + plan + diff (all rules draft) |
| `PENDING-DECISIONS.md` | What the main model must approve before any apply |

## Environment

```sh
uv sync --project scripts/refactor --python 3.12   # tool env (libcst 1.9.0)
export PATH=/opt/homebrew/bin:$PATH                # fixtures shell out to git
```

Tests (from the repository root; 230 toolchain tests, including the 13-test
C0 scanner suite):

```sh
export PATH=/opt/homebrew/bin:$PATH   # fixtures shell out to homebrew git
uv run --project scripts/refactor --python 3.12 \
  python -m unittest discover -s tests/tooling/refactor -t . -p "test_*.py" -v
uv run --project scripts/refactor --python 3.12 \
  ruff check scripts/refactor tests/tooling/refactor --exclude scripts/refactor/.venv
```

The whole repository suite also collects these tests. Supply the same pinned
tool dependency as an ephemeral overlay; do not add it to the product lock or
exclude the tests to hide import errors:

```sh
uv run --with libcst==1.9.0 pytest tests/
```

## Manifest model

Rules carry `rule_id`, `kind`, `stage` (S1..S6), `status` (`draft` |
`approved`), `expected_matches`, `reason`, and kind-specific fields
(`old_module`/`new_module`/`old_symbol`/`new_symbol`, `source_path` +
`source_sha256` pins, `dependencies`, `moves`, `operations`, `wiring`,
`sync_imports`, `manifest_updates`, `verification`). Match-count semantics:

- import rules: rewritten/split/removed import statements **plus** rewritten
  module-object chain usage sites (a chain site is counted once).
- `reference_rule`: sum of the per-operation counts.
- `symbol_extract` / `class_member_extract`: number of extracted symbols.
- `layout_move`: number of moved files (directories expand).

A rule applies only when matches equal `expected_matches`; zero matches with
the migrated form present and a recorded completion counts as `already_done`
(explicit zero-modification state). Zero matches without history is a
failure — a vanished source symbol is never treated as done.

## Commands

```sh
# read-only cumulative preview (S1..S6), writes only to --output
python -m scripts.refactor.run plan --manifest M --root R --all --output DIR

# execute in an isolated checkout; --confirm is the apply_token from plan
python -m scripts.refactor.run apply --manifest M --root R --stage S1 --confirm TOKEN

# staged verification; command checks only run with --run-commands
python -m scripts.refactor.run verify --manifest M --root R --stage S1

# recorded stage state (.refactor-state/m-<hash>/stages.json)
python -m scripts.refactor.run status --root R
```

Exit codes: 0 ok; 2 manifest/tool error; 3 unapproved rules; 4 baseline
mismatch; 5 input drift/dirty; 6 confirm mismatch; 7 rule refusal (state
preserved, `last-failure.json` written); 8 write failure; 9 verification
failure; 10 corrupt state.

Apply preconditions: manifest validates; every rule in the requested stage is
`approved`; HEAD equals `baseline_commit`; no manifest-touched path is dirty
in git (paths this manifest already wrote are exempt via recorded output
hashes); manifest hash pins hold; `--confirm` matches the token. Writes are
materialized moves-then-contents with per-file atomic replace; stage state,
rule logs and input/output hashes are recorded under `.refactor-state/`.

## Refusal semantics (fail closed)

- shadowed or ambiguous bindings, dangling import roots, star imports of a
  moved symbol, dynamic `getattr` on a renamed symbol, emptied imports that
  would drop standalone comments (imports).
- free variables not covered by the explicit dependency list; zero-arg
  `super()` in extracted methods; unclassified `self.X`; missing/invalid
  wiring (symbols).
- export conflicts, non-tool-managed facade files (facades).
- match-count drift on every reference operation (references).
- target conflicts, Windows case collisions, symlink sources/target parents,
  path escapes, WORM content-hash pins (layout).

## Known limitations

- `docs/notes/` and other WORM content is never rewritten by design; `.py`
  files there are not auto-migrated either (scope is a pending decision).
- Import rewrites leave a now-unused plain `import old` in place when only a
  chain usage moved to the new module; the reviewer removes it or pairs an
  `import_module` rule.
- The plan "uncovered references" filter is substring-based over C0 evidence
  and is deliberately over-inclusive (a review aid, not a rewrite list).
- Facade lazy mode is tool-managed only; converting an arbitrary existing
  `__init__.py` requires an explicit main-model decision (refused today).
- `symbol_extract` string-annotation free names are collected conservatively;
  complex stringified annotations may require explicit dependency entries.
