# Adapter desired-state model split

Base: `ee383f80c1505805ea1bb128157ed09896604eca`. Target: 0.5.5.1.

## Types and ownership

Adapters are delivery infrastructure, not executable agent harnesses. Introduce
`AdapterSpec(provider, name, status, legacy_launch)` in the adapter package and
`DesiredState.adapters`. `DesiredState.harnesses` contains declared worker kinds
only. Adapter specs have no `harness` property. Retain the duplicate-name admission
guard for CLI clarity; recovery must still accept old agent/adapter collisions.
Adapter activation, stop, restore and registration rollback use the adapter
collection. Worker selection and agent ownership never read that collection.

## Storage and migration

Desired-state schema 3 has separate `providers` (workers) and `adapters` arrays.
SQLite remains the sole authority: add an adapters table and migrate existing
SQLite documents as well as legacy `.v1` imports. Validate the complete migrated
document before committing. Move legacy adapter rows atomically with the schema
version update; preserve unused launch metadata as inert `legacyLaunch` data so
migration cannot silently discard persisted intent. Reject unknown kinds,
duplicate/conflicting adapter records, malformed arrays and future versions.
The adapter package owns legacy-kind recognition; general consumers do not test
for the string `lark`. Repeated load/save is idempotent. Pins remain untouched in
the agents database (and unresolved staged pins retain their original names).

## Compatibility

Schema 3 is a forward-only boundary, including homes with executionRuntime.
Older daemons reject schema 3 with their existing unsupported-schema error;
there is no compatibility worker row that could resurrect the collision.
Before switching schemas, the same SQLite transaction stores the first legacy
document, its schema version and SHA256 in `adapter_model_backup`. Repeated
migration never overwrites it. The `.v1` import uses the same backup boundary.
There is no automatic binary rollback in the base implementation (#1913 changes
readiness reporting only).

Provide `hyprial adapter downgrade-state --json` as an offline operation, guarded
by the daemon ownership lock. It verifies the backup digest and restores the
legacy document as schema 2. It refuses if current desired intent differs from
the migrated backup: silently rewinding later user changes is not rollback.
Pins in agents.sqlite3 are not read or written. Operators stop the daemon, run
the new command with the new binary, then install/start the old binary. If
migration or restoration fails, keep the new binary and all state/backup files;
repair with an operator rather than editing schemaVersion or deleting state.
Runtime-only downgrade refuses schema 3 with `RUNTIME_DOWNGRADE_REFUSED` and
this two-step remedy: restore adapter backup with the new binary, install the
schema-2 base, then use that base's runtime downgrade to reach schema 1. Do not
add a broader current-intent down-converter. The schema-2 runtime safety tests
retain all behavioral assertions using the actual base codecs/store/runtime.
A full two-step test proves adapters and both staged/authoritative pins survive.
Default tests compare committed golden outputs generated with the real base;
no git history or CI workflow change is required. The separate actual-base
execution group is opt-in with `HYPRIAL_SCHEMA2_EVIDENCE=1` and its result is
recorded independently, never inferred from a default skip. Regenerate with
`scripts/verify/regenerate_adapter_model_goldens.py`; neither path fetches git
objects or uses the network.

## Surfaces and consumers

Adapter status/list and daemon status report `adapter: "lark"` or `provider`,
never `harness: "lark"`. Agent list/top contain workers only. Update the contract
and packaged operations skill. Audit the hotfix's complete consumer inventory,
including lifecycle stop, registry binding, transfer, restore, snapshots,
projections, startup sweeps and registration rollback.

## Validation

RED tests first: legacy JSON and SQLite migration with same-named agent and
adapter, round trip/idempotence, corruption/future versions, rollback refusal,
separate adapter activation/removal and surface shape. Run the hotfix collision
corpus (including isolated delivery and pins), keeping behavioral assertions
unchanged; fixture/representation updates are individually recorded in evidence. Then targeted storage,
adapter, restore, lifecycle and CLI suites; ruff and both structural gates.
Full suite and real provider/Lark traffic are UNKNOWN unless actually run.
All runtime tests use isolated homes; no production daemon or credentials.
