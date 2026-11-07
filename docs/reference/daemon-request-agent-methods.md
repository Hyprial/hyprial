# Agent request methods: CLI, contract and frontend field map

Status 2026-10-31: combination candidate built from the fixed M-01
`315c787e6acd816763161960d6829e1e4db9c29b` and M-02
`a608381f928330d7a5bcd3de7dc710d51704853d` declarations under the user's Task A
priority release. This is not an acceptance verdict for those cards. The
CLI/handler and reference-adapter columns are observed facts; the contract
fields are generated from the merged declarations. The authoritative
params/result/error schemas are
`contract/daemon-request/openapi.json`; do not copy this table into code.

## 1. Five-method matrix

| CLI | Method | Read/write | Retry | HTTP | Legal empty/unconfigured | Handler path |
| --- | --- | --- | --- | --- | --- | --- |
| `hyprial agent list` | `agent.list` | read | `safe` | projected | yes: `agents: []` with a reason | existing agent handler (`app._handle_agent`) |
| `hyprial agent secret ls` | `agent.secret.list` | read | `safe` | projected | yes: `grants: []` with a reason | existing agent handler |
| `hyprial agent provider default` | `agent.provider.default` | mixed read/write (`--clear`) | `never` | projected | yes: `provider: null` (unconfigured) | existing provider handler |
| `hyprial agent provider login` | `agent.provider.login` | write | `never` | projected | no: the write returns receipts or fails; must state the reason | existing provider handler |
| `hyprial agent provider logout` | `agent.provider.logout` | write | `never` | projected | yes: `removed: []`/`absent` with a reason | existing provider handler |

All five are audience `local-operator` + `bound-agent`. The target `actor` is a
business parameter; the HTTP caller identity is bound by the entry principal,
never by a request-body field. `http_exposed` is declaration/projection
eligibility, not proof that a live H server is running.

`agent secret ls` is the grant inventory of a selected agent. It is not
`agent secret list` (the local secret-store command); do not substitute one.

## 2. Frontend field map

Reference adapter:
`gui/src/features/agent-provider/api/index.ts` at the fixed
`e3029fd98ab610fc94914ad6865f2a0f606f940c` or the latest two-fix candidate
`ac893c734bbd79ea96b0fa53ee71165d01433c11`. It accepts the **business result**,
so the single temporary transport adapter must unwrap the HTTP envelope first.
The four components are all exported from
`gui/src/features/agent-provider/AgentProviderSection.tsx`; they are not four
independent files.

| Feature method | Daemon params | Business result fields used | UI view model | Notes/gaps |
| --- | --- | --- | --- | --- |
| `listAgents()` | `{}` | `agents[].actor` or `.uri`, `.name` | `AgentOption {actor,label}` | `agent.list` has no `name` field in the accepted-shape candidate; the adapter falls back to `actor`. Do not invent a display name. |
| `getDefault()` | `{}` | `provider: string \| null` | plain provider or `null` | `null` is the unconfigured state. `previous`, `credentialSource`, `defaultCredential` exist but are not used by the current view. |
| `listGrants(actor)` | `{actor}` | `grants[].grantId`, `.source`, `.fieldName`, `.environmentNames` | `AgentGrant {id,source,field,environments}` | `source` is an enum (`user-provider`, `agent-private`, `agent-shared`). Omitted `actor` is a valid all-agent query but is not used by this adapter. |
| `login(actor,provider,files)` | `{actor,provider,files}` plus optional `force`, `setupToken`, `makeDefault` | `ok`, `actor`, `provider`, `credentialSource`, `files[]`, `default` | success/failure only | `files` is a non-empty list of `{filename, contentBase64}`. The UI needs an explicit input; there is no production credential-picker or upstream OAuth flow. Never send a real credential to the mock. |
| `logout(actor,provider)` | `{actor,provider}` | `ok`, `actor`, `provider`, `removed[]`, `absent[]` | success/failure only | The adapter checks `actor`/`provider`; `removed`/`absent` may drive a later message. |

`agent.provider.default --clear` is a contract/HTTP write branch. The current
frontend has no clear button; verify it at the API/mock layer and do not add a
UI control just to satisfy the matrix.

## 3. Scenario coverage to be attached

| Method | success | empty/unconfigured | schema-invalid negative | schema-valid semantic rejection | domain error |
| --- | --- | --- | --- | --- | --- |
| `agent.list` | one agent row | empty registry | `inactiveSince` boolean/container | `inactiveSince=0` or bad duration (handler `INVALID_ARGUMENT`) | agent registry read failure |
| `agent.secret.list` | one grant row | no grants | `actor` wrong JSON type | handler-level actor validation | unknown agent |
| `agent.provider.default` | configured provider | unconfigured (`provider: null`) | non-object body (the object schema accepts every object) | legacy provider value/combination rejected by handler | provider unsupported / target unauthorized |
| `agent.provider.login` | file receipts | not applicable (reason required) | non-object body (the object schema accepts every object) | missing/invalid file, actor or `makeDefault` (handler) | credential exists / unsafe path / target unauthorized |
| `agent.provider.logout` | removed files | nothing removed (`absent`) | `actor`/`provider` wrong JSON type | actor-first requiredness rejected by handler | provider unsupported / unsafe path / target unauthorized |

The validation boundary is declared, not inferred from `INVALID_ARGUMENT`:
`invalid-params` must fail the projected schema (pre-dispatch) and may use any
raw JSON params, including a non-object; a `semantic-param-error` passes the
schema but the existing handler rejects it after registry validation;
`domain-error` passes the schema and carries any declared business code.  M-02's
`agent.provider.default/login` schemas intentionally accept every JSON object,
so their true structural negative is a non-object body (for example `[]`), not
an object with a wrong field.  M-01/M-02 intentionally keep the handler
authoritative for `inactiveSince`, durations, provider requiredness/coercion and
combined inputs, so the schema must not be tightened to reject today's accepted
input.

The actual case names, values and error codes are generated from the accepted
declarations. `check_agent_contract_examples.py` is the gate; the Contract Mock
reports missing scenarios as `MOCK_SCENARIO_MISSING`.

## 4. Known UI gaps (do not fake)

- No agent display name; `agent.list` does not provide one in the current
  result shape.
- No provider catalog, model catalog, provider→model relation, tier probe or
  provider login status field. Those are not part of these five methods.
- No native credential file picker, OAuth page or setup-token entry component.
  The frontend must make the accepted input explicit; the mock accepts only the
  synthetic sentinel.
- No UI for `--clear` on the provider default.
- No cross-agent grant administration or Org/role field; Org is deferred.
- A transport timeout or `dispatch: "unknown"` is not a definite rejection.
  Show an unknown outcome; do not retry the write or claim rollback.
- Login success does not prove that a secret grant was created. Refresh and
  assert the old handler's real result, not an assumed one.

## 5. Verification seam

| Layer | What proves it | Status |
| --- | --- | --- |
| CLI ↔ handler equivalence | M-01/M-02 focused tests + existing CLI cases | owned by those cards; not re-claimed here |
| declaration ↔ OpenAPI | `uv run python scripts/verify/dump_daemon_openapi.py --check` | PASS |
| examples ↔ declaration schemas | `check_agent_contract_examples.py` and export-time validation | PASS on the combination candidate (23 cases) |
| mock ↔ envelope | mock startup validates every response against `http-envelope.schema.json` | generic PASS; five methods BLOCKED |
| mock ↔ real H-03 envelope | final mock consumes the H-03 projection; HM-01 verifies parity | UNVERIFIED; current schema mirrors H-01 candidate only |
| real HTTP + generated TS + browser | H-02/H-03 + HM-01 combined tree | BLOCKED |
| M-01/M-02 card acceptance | their own review/accept gates | separate; not claimed |
