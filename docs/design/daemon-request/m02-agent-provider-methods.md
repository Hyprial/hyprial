# M-02 — `agent.provider.default` / `login` / `logout` declarations

Date: 2026-10-31. Status: implementation candidate on
`feat/request-agent-provider-specs`; not reviewed or accepted. The shared Agent
registry aggregation is intentionally not changed in this slice: M-01 owns the
partial-registration hook and M-03 combines the accepted read and provider
declarations.

## 1. Scope and non-goals

This slice migrates three existing daemon methods. It keeps the existing CLI,
the existing provider files, and the existing credential-install/revoke
semantics. It does not add an OAuth flow, a provider/model list, a second
credential store, or a second default authority.

The declaration module is
`src/hyprial/daemon/impl/request_catalog/agent_provider.py`; the runtime
handlers remain in
`src/hyprial/daemon/impl/application/actors/agents_admin/provider_credential.py`.

## 2. Method matrix

| Method | Existing CLI path | Params (declared) | Result (declared) | Domain errors |
| --- | --- | --- | --- | --- |
| `agent.provider.default` | `hyprial agent provider default [--clear]`; `login --default` without `--actor` calls `{"provider": name}` | `{}` read; `{"clear": true}` clear; `{"provider": "claude"\|"codex"\|"pi"}` legacy provider write | `{ok, provider, previous}` plus optional `credentialSource`, `defaultCredential` when a captured Claude reference exists | `INVALID_ARGUMENT`, `CALLER_NOT_AUTHORIZED`, `TARGET_NOT_AUTHORIZED`, `AGENT_NOT_FOUND`, `AGENT_ERROR`, `PROVIDER_UNSUPPORTED`, `DAEMON_ERROR` |
| `agent.provider.login` | `hyprial agent provider login <provider> --actor NAME [--source PATH] [--force] [--setup-token] [--default]` | `{actor, provider, files[{filename, contentBase64}], force?, makeDefault?}` or `{actor, provider, setupToken, force?, makeDefault?}` | `{ok, actor, provider}` plus `files[]` for native seeding or `credentialSource: "setup-token"` for the token path, and `default` when `makeDefault` | `INVALID_ARGUMENT`, `CALLER_NOT_AUTHORIZED`, `TARGET_NOT_AUTHORIZED`, `AGENT_NOT_FOUND`, `AGENT_ERROR`, `PROVIDER_UNSUPPORTED`, `PROVIDER_CREDENTIAL_EXISTS`, `PROVIDER_UNSAFE_PATH`, `UNSUPPORTED`, `STALE_AGENT_SECRET_GRANT`, `DAEMON_ERROR` |
| `agent.provider.logout` | `hyprial agent provider logout <provider> --actor NAME` | `{actor, provider}` | `{ok, actor, provider, removed[], absent[]}` | `INVALID_ARGUMENT`, `TARGET_NOT_AUTHORIZED`, `AGENT_NOT_FOUND`, `AGENT_ERROR`, `PROVIDER_UNSUPPORTED`, `PROVIDER_UNSAFE_PATH`, `UNSUPPORTED`, `DAEMON_ERROR` |

All three declarations use:

- `mode="unary"`
- `mutates=True`
- `retry="never"`
- `audience=("local-operator", "bound-agent")`
- `http_exposed=True`
- `confirmation="none"`
- an explicit `IdempotencySpec(mode="never", ...)`

`agent.provider.default` is mixed read/write in one wire method. It is not
declared read-only even for the `{}` branch, because `clear` and `provider`
write the same method. `login` records `makeDefault` and installs files; the
setup-token branch writes the owner's secret store. `logout` removes files.

With a captured Claude default reference, a truthy non-boolean `clear` (for
example `"yes"` or `1`) does not clear the legacy `provider-default.json` file
but does reach the outer captured-reference branch: the operator can clear that
reference, and a non-operator gets `CALLER_NOT_AUTHORIZED`. Falsey values leave
the reference in place. This is existing handler behavior, not a new branch.

The independent pre-review (2026-10-31) proved that the old handler accepts
`provider: null`, `clear: null`, `clear: "yes"`, unknown keys, and any value
for `force`. The declarations therefore keep the legacy value domain instead
of tightening it:

- `default` accepts the legacy `provider`/`clear` values and unknown keys. A
  present non-null `provider` is still validated by the handler. Exact boolean
  `true` clears the legacy provider-name file. A truthy non-boolean `clear`
  leaves that file but still enters the outer captured-Claude-default branch:
  the operator can clear the captured reference, and a non-operator is refused.
  Falsey `clear` values read and leave the captured reference exposed.
- `login` names `actor`, `provider`, `files`, `force`, `setupToken` and
  `makeDefault` but leaves their value domain to the handler. In particular,
  the entry's truthiness check on `makeDefault` runs before the handler's
  boolean check, and `force` keeps its `bool(...)` coercion. This preserves the
  old error order: a worker with a truthy non-boolean `makeDefault` gets
  `CALLER_NOT_AUTHORIZED` before a missing actor is reported.
- `logout` keeps `actor` structural and leaves `provider` requiredness to the
  handler. This preserves the legacy actor-first error: for `{"actor": 5}` or
  `{"actor": null}` with no provider, the old handler reports the invalid actor,
  not the missing provider. Unknown keys remain ignored.
- The handler remains the validation authority. The declared domain errors
  cover its refusals; the schema does not replace them. The second independent
  review (fd59) found the logout root-required ordering; the logout declaration
  now keeps only `actor` structural and leaves `provider` to the handler, and
  `test_logout_combined_invalid_fields_keep_the_old_error_order` covers that
  review's combined-invalid table.

None of the existing provider handlers attach structured `error.data`; the
declared `MethodError` entries therefore have no `data_schema`. The optional
`defaultCredential` result object is the existing registry reference
(`ownerActor`, `ownerEntityToken`, `entryId`, `fieldName`, `kind`), not a
provider credential. Setup-token and uploaded file bytes are never returned by
the handler.

## 3. Permission and target/caller boundary

- For `login`, `actor` is the recipient, not the caller. The entry excludes
  `actor` when it decides whether the caller is the host operator.
  `makeDefault` requires the operator; a worker origin is refused with
  `CALLER_NOT_AUTHORIZED`.
- `default` reads are allowed to a bound agent/worker; writes (`clear` or
  `provider`) require the host operator. The caller cannot self-report the
  operator identity in params.
- `logout` and `login` act only on an agent owned by the daemon's logged-in
  user. A hosted visitor or another owner's agent is refused with
  `TARGET_NOT_AUTHORIZED`; an unknown name is refused with `AGENT_NOT_FOUND`.
- The provider entry now translates the same `AgentError`, `DomainCommandError`
  and `AgentHomeError` boundary the legacy agent branch applies, so a binding
  that calls the entry directly keeps the old wire codes instead of leaking raw
  registry exceptions.
- This slice does not add the `agent` family to the migrated-family set and
  does not edit the shared registry assembly. Other Agent methods still take
  the legacy route until M-03 combines the declarations.

Internal registry-home failures surface as `AGENT_ERROR`; the setup-token grant
fence surfaces as `STALE_AGENT_SECRET_GRANT`.  The latter is an existing wire
literal that is not yet in the shared `ipc_errors` registry; the declaration
mirrors it rather than renaming it.  Other generic domain-command settlement
or admission failures are infrastructure, not provider business errors; the
generic registry normalizes an undeclared code to `DAEMON_ERROR`.  CLI-local
source failures (`PROVIDER_SOURCE_MISSING`, `PROVIDER_SOURCE_AMBIGUOUS`,
`PROVIDER_UNSAFE_PATH` from the local source resolver) happen before IPC and
are not method errors.

## 4. Existing login input to GUI adaptation

| Existing input | GUI adaptation | Constraint |
| --- | --- | --- |
| CLI reads `~/.claude/.credentials.json`, `~/.codex/auth.json`, or pi's `auth.json` + `models-store.json` | Browser file picker reads the selected files and base64-encodes their bytes into `files[]` | Browser cannot read arbitrary paths. The selected names must exactly match the provider's allowlist; pi requires both files; an empty selection is refused. The CLI source resolver additionally refuses symlinks and non-regular host paths. |
| `--source PATH` | Not available in the GUI | CLI-only convenience for a single-file provider. The GUI has only the file picker. |
| `PI_CODING_AGENT_DIR` relocation for pi | Not available in the GUI | CLI-only environment override; the GUI must not guess a server path. |
| `--setup-token` reads one non-TTY stdin token | One controlled sensitive input (password-style), submitted once with `setupToken` | Never prefill, log, persist in local storage, or echo the value. The daemon accepts only a non-empty token with no whitespace/NUL and at most 16384 characters. |
| `login --default --actor OWNER` | Operator-only `makeDefault` action; for Claude the owner actor is the credential owner | A non-operator principal must not set the machine default. The GUI must not present this action as available to an agent session. |
| `agent provider default --clear` | Backend/contract coverage; a front-end button is optional | Same mixed method, `mutates=True`; do not call it from a read-only view without an explicit operator action. |
| `--actor NAME` target selection | Agent selector in the section; the selected agent is the target | The selected target is not the caller identity; server-side principal binding remains authoritative. |

The GUI must not claim more than the handler provides:

- `login` is a one-shot seed, not validation. The result says the files/token
  were installed; it does not prove the provider accepts them.
- Native seeding does not necessarily add an `agent.secret` grant. Do not
  assume a grant exists after `login`; re-read the existing grant surface.
- `logout` removes the agent's provider files only. It does not invalidate the
  copied upstream token; the vendor CLI or provider account remains the
  authority for that.
- Missing capabilities: no provider OAuth web flow, no provider/model discovery
  list, no credential validation/refresh loop, no browser-side vendor login,
  no server-side reading of the operator's home paths, and no server-side
  "logged in" state beyond the installed files. The GUI cannot revoke the
  upstream token.

## 5. Verification evidence

Focused suite (real `DaemonApplication`, provider fixture, real CLI runner):

```bash
env -u CLAUDE_CODE_MAX_CONTEXT_TOKENS -u CLAUDE_CODE_AUTO_COMPACT_WINDOW \
  uv run --python 3.12 pytest \
  tests/daemon/ipc/contracts/test_agent_provider_specs.py -q
```

Observed: `25 passed` (exit 0).

Provider and contract neighbors:

```bash
env -u CLAUDE_CODE_MAX_CONTEXT_TOKENS -u CLAUDE_CODE_AUTO_COMPACT_WINDOW \
  uv run --python 3.12 pytest \
  tests/daemon/harnesses/providers tests/daemon/ipc/contracts \
  tests/shell/cli/agents/provider -q
```

Observed: `173 passed` (exit 0). The two ambient Claude context variables are
unset because this agent shell exports values that make the existing
`test_claude_context_window` tests read the operator's environment rather than
their fixture; with them unset the suite is green. That is a test-environment
isolation note, not a product change in this slice.

Structural and boundary gates:

```bash
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
```

Observed: both exit 0, no structural violations and no boundary violations.

## 6. Known limits

- Actual router reachability is not switched in M-02. `METHOD_REGISTRY` still
  exposes only `link.*`; `agent.provider.*` requests still take the legacy
  agent branch. The equivalence tests assemble a `MethodRegistry` from
  `AGENT_PROVIDER_BINDINGS` and compare it with the old `app.handle` route; they
  do not prove that a live daemon already serves the declarations. M-03 owns
  the single registration/aggregation commit. Until that lands, the honest
  statement is "direct binding equivalence", not "live IPC integration".
- HTTP write admission is owned by H-03. These declarations are
  `http_exposed=True` with `mutates=True`/`retry="never"`, but a real
  authenticated HTTP write round trip is not proven in M-02 and must not be
  reported as a joint PASS until H-03 and the contract mock are integrated.
- No real provider account, browser, or real Lark/Feishu path is exercised.
  Credentials in the tests are synthetic and isolated.
- The shared registry does not yet include these bindings; M-03 owns the single
  aggregation commit. Until then, the focused tests assemble a
  `MethodRegistry` from `AGENT_PROVIDER_BINDINGS` directly.
