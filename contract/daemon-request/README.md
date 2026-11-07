# Daemon request contract projections (A-03 skeleton)

`openapi.json` is generated from the daemon's pure `MethodSpec` declarations.
It is a **contract projection**, not a running HTTP gateway: A-03 starts no
server and the document contains no clock, home path, socket or live identity.

Regenerate and verify:

```sh
uv run python scripts/verify/dump_daemon_openapi.py --write
uv run python scripts/verify/dump_daemon_openapi.py --check
```

`x-hyprial-methods` is the complete declared catalog (including methods that
are not HTTP-exposed). `paths` contains only `http_exposed=true` methods, one
`POST /daemon/request/<method>` projection each; mutations `link.add` and
`link.drop` are catalogued but not exposed. `daemon.catalog` is the read-only
authority the HTTP entry consumes at runtime; it is catalogued with
`httpExposed=false` and has no path.

`httpBinding` (catalog entry and `x-hyprial-http-binding` path extension) is a
**transport binding**, not an authorization claim. An explicit binding names the
HTTP identity mode/selector parameter and the readiness probe key. A declaration
with `http_exposed=true` but no explicit binding is still eligible for the
projection and receives the default no-selector binding (`identity.mode=none`);
that never infers a caller from a business parameter. Runtime routing additionally
requires the audience/unary/safe profile in the HTTP entry, and the actual
session/domain authorization stays in the daemon. Error responses are a typed
`oneOf` over the method's declared domain codes and their `data` schemas.

The snapshot is pinned by
`tests/tooling/contracts/test_daemon_openapi_contract.py`; that test also
fails on empty/boolean schemas, duplicate or unbound handlers, an unregistered
method, and an execute method that accepts replacement params.

## Same-source examples and Contract Mock (M-03)

`openapi.json` also carries the validated development examples when a method has
an example set:

- `x-hyprial-examples` on the operation: labelled success, empty/unconfigured,
  a true schema-invalid `invalid-params` case, and schema-valid
  `semantic-param-error`/`domain-error` cases.  The kind declares the validation
  boundary; the error code does not.  Legacy handlers keep authority over
  durations, coercions, requiredness and combined inputs, so a schema-valid
  value may still be an `INVALID_ARGUMENT` after dispatch.  An `invalid-params`
  case may carry any raw JSON value, including a non-object; for an
  object-universal schema such as `agent.provider.default/login`, that is the
  only true structural negative and the declaration is not tightened to create
  an object negative;
- `x-hyprial-example-gaps`: explicit reasons when a method has no legal
  empty/unconfigured state or no non-parameter domain error;
- JSON Schema `examples` inside the projected request/result schemas.

The cases are declared in
`src/hyprial/daemon/impl/request_catalog/examples.py` and validated against the
owning `MethodSpec` schemas by `export_openapi()`. The current combination
candidate contains the five Agent methods and 23 cases; the gate passes:

```sh
uv run python scripts/verify/check_agent_contract_examples.py
```

Run the Contract Mock (loopback only, no daemon, no real credentials):

```sh
PYTHONPATH=contract/daemon-request uv run python -m contract_mock \
  --scenario success --host 127.0.0.1 --port 8770
```

`--scenario` is required: `success`, `empty`, `unconfigured` or `error`.
The `error` scenario serves either a `domain-error` or a schema-valid
`semantic-param-error`; use `--case METHOD=CASE` to choose among them.
Per-method overrides use `--method-scenario METHOD=SCENARIO` and
`--case METHOD=CASE`. The startup line prints the synthetic bearer token and
the contract digest. `GET /healthz` is unauthenticated; `GET /mock/contract`
reports the selected scenario, gaps and skipped un-mockable methods. A method
with no selected scenario returns 409 `MOCK_SCENARIO_MISSING` rather than a
fabricated result.

`http-envelope.schema.json` pins the transport envelope/status map read from
H-01 candidate `02b9001fae32bd998a75654c0ea259d94276c7c5`. It is a temporary
M-side seam for frontend development, **not same-source with H-03**. The final
mock must consume the accepted H-03 projection and HM-01 must verify parity;
until then the mock does not prove real HTTP, browser, permission or
credential-write behavior.

Breaking changes are reviewed with:

```sh
uv run python scripts/ci/check_daemon_contract_diff.py \
  --base <old-openapi.json> --head contract/daemon-request/openapi.json \
  [--pr-body-file pr-body.txt]
```

A `BREAKING` finding stays red unless the PR body declares
`Contract-Impact: <reviewed reason>`. See
`docs/guides/daemon-request-contract-mock.md` for startup, scenario, field-map
and sensitivity details.
