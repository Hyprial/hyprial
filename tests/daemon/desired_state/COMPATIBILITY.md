# Adapter-model compatibility fixtures

`adapter-model-goldens.json` records actual outputs from base
`ee383f80c1505805ea1bb128157ed09896604eca`: canonical schema-1/2 documents,
restored schema-2 document, schema-3 rejection text, runtime-bearing schema-2
input, post-runtime-downgrade schema-1 output, and serialized authoritative pin
rows. Its own metadata repeats the base SHA and regeneration/evidence commands.
Default tests read this committed file. They do not read git history or fetch
anything. Live pin databases are also compared byte-for-byte across restoration.

Regenerate using the actual base (requires its git object and test dependencies):

```sh
uv run python scripts/verify/regenerate_adapter_model_goldens.py
```

Explicit actual-base evidence run (not part of default CI acceptance):

```sh
HYPRIAL_SCHEMA2_EVIDENCE=1 uv run pytest \
  tests/daemon/desired_state/test_adapter_rollback.py \
  tests/integration/smolvm/runtime/test_smolvm_runtime_state.py -q
```

Without this opt-in, tests that execute historical code report a labelled skip.
That skip is NOT evidence of rollback or runtime-safety success. Record the
explicit run independently. The helper never fetches git objects. It loads the
base launch codec, desired-state codec, SQLite reader/writer, store, import path,
and runtime downgrade implementation in a distinct temporary module namespace.
Unchanged supporting modules are shared with the checkout. All homes/databases
are temporary. The old runtime safety assertions are unchanged; only their
schema-2 implementation fixture is selected explicitly.
