# Daemon request examples and Contract Mock

Status 2026-10-31: **combination candidate, not card acceptance.** The fixed
M-01 `315c787e` and M-02 `a608381f` declarations are merged into the M-03
source under the user's Task A priority release. The five Agent example sets,
single-source OpenAPI snapshot and Contract Mock scenarios are implemented;
the gate below reports `PASS` on this combination. M-01/M-02 card acceptance,
the full regression, real H-03 HTTP/TS parity and browser behavior remain
separate and are not claimed here.

## 1. What this is

```text
MethodSpec declarations
  └─ daemon_openapi()                       contract/daemon-request/openapi.json
       └─ x-hyprial-examples (same schema)  examples validated by each method's own schemas
       └─ Contract Mock                     POST /daemon/request/<method> + HTTP envelope
       └─ generated client (H-02, later)    MethodSpec → OpenAPI → TS
```

- The Contract Mock is a local, loopback-only development server. It needs no
  daemon, home, socket, identity, credential store, Node or real provider.
- It is not H, not a browser-integration proof and not a permission or
  credential-write test. H-02/HM-01 own the transport/client parity check.
- The mock reads only the committed business projection; it does not import
  `hyprial`, so it cannot quietly start a daemon or take a second schema copy.

## 2. Current five-method gate

```sh
uv run python scripts/verify/check_agent_contract_examples.py
```

Exit codes are three-state:

| Exit | Meaning |
| --- | --- |
| 0 | all five methods are present with success, legal empty/unconfigured or a reason, a true schema-invalid params case, and a schema-valid handler/domain error or a reason |
| 1 | the projection has a real defect (missing scenario, published secret, ...) |
| 2 | the required declarations are absent from the projection — **not a pass** |

The combination snapshot contains five methods with 23 example cases and
exits 0. The cases live in
`src/hyprial/daemon/impl/request_catalog/examples.py`; regenerate after changing
a declaration or example:

```sh
uv run python scripts/verify/dump_daemon_openapi.py --write
uv run python scripts/verify/check_agent_contract_examples.py
```

Do not hand-edit `contract/daemon-request/openapi.json`.

## 3. Run the mock

```sh
PYTHONPATH=contract/daemon-request uv run python -m contract_mock \
  --scenario success \
  --host 127.0.0.1 --port 8770
```

The startup line prints the endpoint, the scenario, a **per-process synthetic**
`Bearer` token and the contract digest. Copy the token; there is no default
browser-visible token and no request-body identity claim.

```sh
curl -sS http://127.0.0.1:8770/healthz
curl -sS -H "Authorization: Bearer <printed-token>" \
     -H "Content-Type: application/json" \
     -d '{}' \
     http://127.0.0.1:8770/daemon/request/agent.list
```

### Explicit scenario selection

`--scenario` is required and accepts exactly:

| Scenario | Served case kind | Notes |
| --- | --- | --- |
| `success` | success | normal business result |
| `empty` | empty | legal empty result; the case carries its reason |
| `unconfigured` | unconfigured | legal absent/default state, e.g. no provider configured |
| `error` | domain-error or semantic-param-error | declared business rejection, dispatched |

A method with no case for the selected scenario returns HTTP 409
`MOCK_SCENARIO_MISSING` and the recorded gap reason; it does not fabricate a
success. Override one method:

```sh
PYTHONPATH=contract/daemon-request uv run python -m contract_mock \
  --scenario success \
  --method-scenario agent.provider.default=unconfigured \
  --case agent.provider.login=installed
```

A `--method-scenario` override that the method cannot satisfy fails at startup.
An explicit `--case` wins over the quality selection.

### Validation boundary

The mock never infers the error stage from the error code.

| Case kind | Params | Stage | Example |
| --- | --- | --- | --- |
| `invalid-params` | may be any raw JSON value and must fail the projected params schema | rejected before dispatch, `dispatch: "not-dispatched"` | wrong JSON type; a non-object body when the schema accepts every object |
| `semantic-param-error` | must pass the schema | handler/business rejection after registry validation, `dispatch: "dispatched"` | legacy `inactiveSince=0`, malformed duration, combined provider input |
| `domain-error` | must pass the schema | declared business rejection, `dispatch: "dispatched"` | target not authorized |

`domain-error` may use any declared code, including `INVALID_ARGUMENT`; the
kind states where the rejection happens, not the code. The true schema negative
is still required for every method, so a handler-level rejection never hides a
missing pre-dispatch control. Send an invalid body and the mock returns
`INVALID_ARGUMENT` with `dispatch: "not-dispatched"` without selecting a case.

### Other endpoints

- `GET /healthz` — `{"ok": true}`, no auth.
- `GET /mock/contract` — bearer-protected metadata: contract version/digest,
  selected scenario, available scenarios, gaps, skipped un-mockable methods and
  the envelope source. Not an H endpoint.
- CORS defaults to the local Vite origins `localhost`/`127.0.0.1` on ports
  5173 and 4173. Add another with repeatable `--allow-origin`; there is no
  wildcard. Loopback binding only.

### Check mode

```sh
PYTHONPATH=contract/daemon-request uv run python -m contract_mock \
  --scenario success --check
```

It loads and validates the projection, prints the available scenarios, and
reports `BLOCKED` for a projection with no examples.

## 4. Envelope and contract version

The mock mirrors the H-01 candidate envelope shape:

```json
{"schemaVersion":1,"ok":true,"method":"agent.list","requestId":"mock-...",
 "dispatch":"dispatched","result":{"ok":true,"agents":[]}}
{"schemaVersion":1,"ok":false,"requestId":"mock-...","code":"INVALID_ARGUMENT",
 "message":"...","dispatch":"not-dispatched","method":"agent.list"}
```

`contract/daemon-request/http-envelope.schema.json` pins the shape, field set
and status map read from H-01 candidate
`02b9001fae32bd998a75654c0ea259d94276c7c5`. It is a **temporary M-side seam,
not same-source with H-03** and not a second transport authority. The final mock
must consume the accepted H-03 projection and HM-01 must verify envelope/status
parity; until then this is a development seam only. Do not treat mock status
codes as proof of H behavior.

The business projection is `openapi.json` `info.version` (`1` today); the mock
reports it as `contractVersion` and the canonical document hash as
`contractDigest`. The TS client is generated from the same projection by H-02;
until H-02 lands, `MethodSpec → TS` drift is **BLOCKED**, not verified.

## 5. Same-source examples

Examples live in `REQUEST_EXAMPLES` in
`src/hyprial/daemon/impl/request_catalog/examples.py`. Each set names an
`http_exposed` method and carries cases with:

- `success`, `empty` or `unconfigured` — params and result validated by the
  method's own schemas; empty/unconfigured require a `reason`;
- `invalid-params` — the params may be any raw JSON value (including a
  non-object) and must actually fail the params schema, with a declared
  parameter error code.  This matters for legacy declarations such as
  `agent.provider.default/login` whose object schema accepts every object: the
  only true structural negative is a non-object body, and the declaration must
  not be tightened to create an object negative;
- `semantic-param-error` — params pass the schema, but the existing handler
  rejects the value (legacy duration/coercion/requiredness or combined input);
  the code and data must be declared, and the response is dispatched;
- `domain-error` — valid params, any declared error code (including
  `INVALID_ARGUMENT`), and data matching that error's declared data schema;
- `empty_not_applicable` / `domain_errors_not_applicable` — explicit reasons
  when a method has no such legal state. The mock reports these as gaps.

`export_openapi()` validates every case, adds `x-hyprial-examples` and
`x-hyprial-example-gaps` to the operation, and adds JSON Schema `examples` to
the request/result schemas. Regeneration is the only edit path.

## 6. Drift and breaking-change rules

| Check | Command | Current state |
| --- | --- | --- |
| declaration → OpenAPI snapshot | `uv run python scripts/verify/dump_daemon_openapi.py --check` | PASS |
| five-method example coverage | `uv run python scripts/verify/check_agent_contract_examples.py` | BLOCKED (M-01/M-02) |
| mock ↔ projection | mock loads `openapi.json`; startup validates every example/envelope | PASS for the generic fixture; BLOCKED for five methods |
| breaking diff base → head | `uv run python scripts/ci/check_daemon_contract_diff.py --base <old.json> --head contract/daemon-request/openapi.json [--pr-body-file pr-body.txt]` | script ready |
| MethodSpec → TS client | H-02 generated client + its `--check`; HM-01 runs both on the combined tree | BLOCKED (H-02) |

The breaking-diff rule: removing a method/path/error code/example case, adding a
required field or required property, removing an enum value, narrowing a type or
rejecting previously accepted extra properties is **BREAKING**. Adding an
optional field, an error code, an example case or a path is compatible. A
breaking change is green only with a PR-body line:

```text
Contract-Impact: <reviewed reason; which consumers were updated>
```

The repository's `Contract-Impact` gate remains the declared-release mechanism;
this script adds the schema/example-specific detection it does not cover.

## 7. Sensitive-input rules

- No real token, credential file, PEM, API key, `HYPRIAL_HOME` path or
  `/Users/...` path is allowed in an example, mock response, OpenAPI, log or
  screenshot.
- Provider login examples use `setupToken: "example-not-a-credential"` and
  `contentBase64` that decodes to the same sentinel. The scanner allows only
  that sentinel for sensitive keys.
- Findings name the JSON path and never echo the candidate value.
- Tests use isolated temporary homes/sockets/ports only; the mock never reads a
  production daemon, Lark App or user configuration.

## 8. Frontend handoff

The fixed reference frontend is
`feat/gui-examples-agent-provider@e3029fd98ab610fc94914ad6865f2a0f606f940c`;
the latest two-fix candidate is
`ac893c734bbd79ea96b0fa53ee71165d01433c11`. Both keep the adapter at
`gui/src/features/agent-provider/api/index.ts`. All four components
(`AgentSelector`, `ProviderDefaultView`, `AgentGrantList`,
`AgentProviderSection`) are exported from
`gui/src/features/agent-provider/AgentProviderSection.tsx`, not four separate
files. Its injected `request(method, params)` expects the **business result**
(for example `agents`, `provider`, `grants`, `actor`, `removed`), not the HTTP
envelope.

Two deliberate seams remain until H-02:

1. A single temporary transport adapter unwraps
   `{ok, result}`/`{ok, code, message, dispatch}` and maps a definite pre-dispatch
   rejection to `ProviderMutationRejected`. Transport timeouts and
   `dispatch: "unknown"` are not rejections, are not retried and never claim a
   rollback.
2. That adapter is the only place to replace with the generated grouped client.

Field mapping and known UI gaps are in
[`docs/reference/daemon-request-agent-methods.md`](../reference/daemon-request-agent-methods.md).
Do not invent `name`, provider/model lists or grant fields the handler does not
return.

## 9. Known BLOCKED / UNKNOWN

- M-01/M-02 card acceptance and their own final review verdicts: separate from
  this combination candidate.
- Served HTTP OpenAPI, grouped TS client, real Uvicorn + isolated daemon,
  browser CORS/auth and real write side effects: BLOCKED on H-02/H-03 and HM-01.
- Envelope/status parity with the real H-03 projection: UNVERIFIED; the current
  schema is a temporary H-01 candidate mirror, not same-source.
- Mock status mapping for domain codes not present in the H-01 candidate:
  UNKNOWN; the mock defaults to 500 exactly as that candidate does.
- The full `tests/` suite is not run in this preparation build; it needs the
  coordinator's fixed-SHA heavy-suite slot.
