# Daemon request public baseline (A-05)

This is the consumable A baseline for the B (`agent move`) and C (remaining
daemon-request migration) workstreams. It records the public Python surface,
the declared method snapshot, the installed console/resource proof, and the
reproduction gate. It is **not** a release PASS: the known full-suite and
remote-CI obligations recorded in
[`docs/evidence/cli-command-surface/a05-installed-verification.md`](../evidence/cli-command-surface/a05-installed-verification.md)
remain open until the coordinator's independent final run.

## Bound source, artifact and contract

| Item | Value |
| --- | --- |
| Source commit | `069a10e41` (A-05 final build tree based on dev `d7175833a`; includes the effort/identity/doctor/gate rework) |
| Validator | `scripts/verify/verify_installed_baseline.py` SHA-256 `41b645218626c09ec9d4bc5473f6186315364661c272502e387b55a1c677f71e` |
| Merged dev commit | `d7175833a` (final dev tip: 98b104257 R2 User-Agent + #2029 CI skip; the 42 import rows were measured against the earlier bulk dev `da2c9337d`; `5f6e4d0ed` was the pre-0.5.5.1 tip and `d8a7fa88c` its integration merge) |
| Integration tree | `069a10e41` (final A05 source; dev `d7175833a` plus the reviewed boundary rework) |
| Accepted dependency heads | A-03 `20269c960c98ba5def5abe65d3948a60ea8f8550`; A-04 `45a7d613577d022c5cb04412940caf9cb6549b11` |
| Wheel | `hyprial-0.5.5.1+g069a10e-py3-none-macosx_13_0_arm64.whl` |
| Wheel SHA-256 | `f18a9a47e5398d32796631e1b12b2d53f2d559af022967970b3576dc84257090` |
| Wheel member count | 1164 |
| Sidecar resource | `hyprial/_bin/hyprial-tailcat`, 28,357,602 bytes, mode `0755` |
| Console script | `hyprial = hyprial.cli:main` |
| Contract file | `contract/daemon-request/openapi.json` SHA-256 `8b8a83ba3e1a5365f102e242589f79bfef4489ad3d9c1540913cebed0d174a81` |
| Installed OpenAPI canonical SHA-256 | `2aa3208b775e7febd62a3993fb3da552a1b0454f791fb7efd11aebb7e0f93894` |
| Installed catalogue canonical SHA-256 | `c63e2fb823fc7ecb4843633231326bd1b8df6a69e0dbe9846fb17a9a25302cd6` |
| Verified runtime | CPython 3.12.14, macOS 26.5.1 arm64 |

The artifact was built from the clean source commit with
`scripts/build/build-wheel.sh` (PEP 440 local segment `+g<short7>`; the real
release base is now the four-segment `0.5.5.1`), installed non-editably outside
every checkout, and exercised with `python -I` so neither the current
directory nor `PYTHONPATH` can load `src/hyprial`.

## Public import surface

B/C consume these public names; they do not import another domain's `impl/`,
keep a second IPC client, or add old aliases/shim modules.

```python
from hyprial.kernel import (
    DaemonRequestError,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    PreviewSpec,
    RequestPortError,
    daemon_request,
    daemon_request_once,
    daemon_socket_path,
    export_catalog,
    export_openapi,
    ipc_errors,
    validate_method_specs,
)
from hyprial.daemon import daemon_catalog, daemon_openapi
```

Client contracts:

- `daemon_request(method, params=None, *, socket_path, timeout=None,
  restore_wait=None, clock=None, uuid4=None)` — the only shared IPC transport;
  retries only the pre-side-effect `DAEMON_RESTORING` refusal.
- `daemon_request_once(...)` — exactly one connect/frame/response; a write
  failure is reported, never blindly replayed.
- `daemon_socket_path(state_dir, *, env)` — endpoint selection without reading
  the state root when an authoritative `HARNESS_SOCKET_PATH` is set.
- `MethodSpec` / `MethodError` / `PreviewSpec` / `IdempotencySpec` — pure
  declaration data; `export_catalog` / `export_openapi` project the same
  declarations deterministically without a daemon, home, or credential.
- `daemon_catalog()` / `daemon_openapi()` — the daemon-owned frozen registry
  view. They are pure data, not a running HTTP gateway.

## Declared method snapshot

Catalogue `schemaVersion: 1`; OpenAPI `3.1.0`. All four methods are owned by
`daemon.links`, audience `local-operator` + `bound-agent`, confirmation `none`,
mode `unary`.

| Method | Mutates | Retry | HTTP exposed | Required params | Declared errors |
| --- | --- | --- | --- | --- | --- |
| `link.add` | yes | never | no | `from`, `peer` | `INVALID_ARGUMENT`, `DAEMON_NOT_READY`, `CALLER_NOT_AUTHORIZED`, `SENDER_UNRESOLVED`, `SENDER_NOT_AUTHENTICATED`, `STALE_SESSION`, `SESSION_SUPERSEDED`, `INVALID_SESSION_SOURCE` |
| `link.drop` | yes | never | no | `from`, `peer` | same set as `link.add` |
| `link.list` | no | safe | yes | `from` | `INVALID_ARGUMENT`, `DAEMON_NOT_READY` |
| `link.stats` | no | safe | yes | `from` | `INVALID_ARGUMENT`, `DAEMON_NOT_READY` |

`link.add` params also accept `onBehalfOf`, `sessionRef`; `link.drop` the same.
The mutating pair carries no idempotency key (mode `never`); the read pair is
`read-only-query`. `link.list` / `link.stats` are the two `POST
/daemon/request/<method>` OpenAPI paths. `link.add` / `link.drop` stay
catalogued but not HTTP-exposed.

This is the A-05 snapshot, not the complete future catalogue. B adds `move.*`
declarations and handlers in its own PR; C adds the remaining command groups.
Neither may register a placeholder `{}` schema or a fake handler.

## Installed behaviour snapshot

- `hyprial help --json` exits 0 and reports `ok:true`; stdout SHA-256
  `a88e3c507479610b79e340710336d355205258f50b0cf12b4b4cbead224c4d51`.
- `hyprial version` reports `packageVersion: 0.5.5.1+g069a10e` and the wheel
  install origin.
- A fresh isolated daemon (`hyprial daemon run`, short AF_UNIX path, isolated
  `HOME`/`HYPRIAL_HOME`/`HARNESS_STATE_DIR`, Zenoh isolated, peer discovery
  off, `HYPRIAL_LINKS_BYPASS=0`) served the real link commands.
- The eight accepted link text/JSON cases match
  `tests/integration/cli/link/golden.json` byte-for-byte (stdout, stderr,
  exit). The error cases keep `INVALID_ARGUMENT`, `CALLER_NOT_AUTHORIZED`,
  `SENDER_UNRESOLVED`, exit 1 and the pinned empty-peer text/JSON bytes.

## Reproduce the installed acceptance

```sh
# 1. Build from the exact clean source commit and record the hash.
scripts/build/build-wheel.sh /tmp/a05-dist
shasum -a 256 /tmp/a05-dist/hyprial-*.whl

# 2. Install locked runtime dependencies and the wheel into a venv outside
#    every checkout.  `--no-deps` prevents the installer from substituting a
#    source tree or another dependency set.
uv export --locked --no-emit-project --format requirements-txt \
  --output-file /tmp/a05-locked-req.txt
uv venv --python 3.12 /tmp/a05-installed-venv
uv pip sync --python /tmp/a05-installed-venv/bin/python --require-hashes \
  --index-url https://pypi.org/simple /tmp/a05-locked-req.txt
uv pip install --python /tmp/a05-installed-venv/bin/python --no-deps \
  /tmp/a05-dist/hyprial-*.whl

# 3. Verify the artifact, installed public surface, console script, OpenAPI
#    export, package resource and a real isolated daemon.
uv run --no-project --python 3.12 \
  python scripts/verify/verify_installed_baseline.py \
  --wheel /tmp/a05-dist/hyprial-*.whl \
  --expected-commit 069a10e41 \
  --python /tmp/a05-installed-venv/bin/python \
  --hyprial /tmp/a05-installed-venv/bin/hyprial \
  --contract contract/daemon-request/openapi.json \
  --golden tests/integration/cli/link/golden.json \
  --artifacts-dir /tmp/a05-artifacts --json-out /tmp/a05-artifacts/result.json
```

Exit 0 with `"ok": true` is the acceptance result. The standard wheel above
has `0.5.5.1+g069a10e`, 1,159 installed product members byte-matched to the
wheel, and 13 real isolated daemon/CLI cases. A thin/worker wheel (no sidecar,
`py3-none-any`), a wrong installed artifact and a tampered contract all exit 1;
none may be used as proof of the published platform wheel. The standard published wheel
always carries the Go sidecar; `HYPRIAL_TAILCAT_ALLOW_MISSING` is not part of
this acceptance.

## B/C consumption rules

For B (`agent move`):

1. Declare each method with the public `MethodSpec` and complete
   params/result/error schemas; bind it to a real handler. Do not add a
   compatibility router fallback.
2. Use `hyprial.kernel.daemon_request` / `daemon_request_once` for transport;
   a business write is never blindly retried. Keep move state ownership in B's
   domain, not in the client or CLI.
3. Add the thin CLI wrapper through the shared CLI registration/DI and the
   single `CliResult`/`CliStream` output layer. `--json` stdout is one `ok`
   object; progress/warnings/confirmation go to stderr.
4. Update `contract/daemon-request/` and both ledgers in the same PR; run the
   request-contract gate with the real PR base. Unimplemented move methods
   must not appear as available commands.

For C (remaining migration):

1. Migrate one command group per PR onto the same `MethodSpec` + handler
   binding + CLI output patterns. The declared set and reachable dispatch set
   must converge; no empty schema or fake handler may fill a gap.
2. Keep the import ratchet monotonic: exact old references only decrease, and
   the backend never imports the CLI. Add negative controls when extending a
   gate.
3. Use the installed verifier at the exact final integration head as the
   package/console/resource proof, in addition to focused and contract tests.

## Known limits (not PASS)

- Only `link` is migrated and declared; all other dispatch families remain on
  the legacy router until C.
- `link.add` / `link.drop` are catalogued but not HTTP-exposed. This baseline
  starts no HTTP gateway.
- The generic preview protocol exists as a declaration/binding surface but is
  not wired to a business write; durable preview replay is C's scope.
- B move business, ACL/host acceptance and identity/runtime authority are not
  implemented here.
- The full test suite is not green at this baseline. Known failures and the
  coordinator's final full run remain open; this document does not turn them
  into PASS. See the evidence file for the exact retained list.

## Change policy

Any change to a JSON key, diagnostic field, event name, error code or declared
method must update `contract/` (or the PR must state a reviewed
`Contract-Impact` exemption), regenerate the snapshot, and update this
baseline in the same PR. Skill-Impact is `not-needed` for this baseline unless
an agent-facing command or workflow changes.
