# Pi connection launch boundary

This stage implements the Pi runtime seam from [design PR #3134](https://code.hyprial.com/HyprialOS/hyprial/pulls/3134).
Only a trusted daemon-built `PiModelBinding` can enter it. Public `agent.start`
does not accept a connection reference. Stage 4a resolves internally persisted
bindings and existing grants at the common launch seam; public configuration waits
for stage 4b. Existing native auth and owner-key methods
are unchanged. No new daemon-request method or HTTP endpoint is introduced.

## Exact request

Custom connections support RPC/headless on verified Pi SDK 0.85.1, 1.2.0 and
1.3.0. Other versions and custom TUI launches refuse admission. The existing
Pi maximum-version guard still applies. The template uses `openai-completions`,
one opaque manually supplied model ID, text input, no reasoning, a 32768-token
local context budget and a 4096-token output budget. These are local limits,
not assertions about the upstream model. Explicit limits require output < context.

Every request uses the selected endpoint plus `/chat/completions`, selected Key
and exact model ID. No model/credential fallback, redirect, provider retry,
store flag, developer role or reasoning option is enabled. Registration uses
zero cost placeholders because the SDK requires cost fields; reported cost is
`null` with `costStatus=unknown`, never a claim that the model is free.

The Key enters only the private process environment and request authorization;
it is absent from launch arguments, receipts and model-store files. Each agent
uses its own native-root `models-store.json`.

## Session ownership and resume

Bound sessions require a positively identified local volume at the identity-owned
Pi session root (`<agentHome>/state/pi`), with no symlink/reparse aliases below
that home. Other home subdirectories do not qualify. Unknown, shared and
FUSE volumes refuse admission. A non-recursive session-specific mkdir owns the
writer; an empty or corrupt claim also blocks reuse. No PID/age-based stealing.

All launches claim ownership while opening/checking the session and writing its
canonical receipt. Legacy launches release after that initial receipt; bound
launches retain ownership until awaited abort/dispose completes. EOF, normal
daemon stop and early safe failure release it, including SIGTERM during runtime
construction before the SDK registers its RPC signal handlers. Crash, failed
disposal or changed owner token leave it blocked. Recovery uses a new session;
this stage provides no automatic or manual unlock command. Legacy model/auth
selection remains unchanged, but a competing initialization or residual claim
can now refuse legacy startup during that short initialization interval.

The trusted launch projection may omit provider/model or repeat the exact bound
pair. Conflicting or partial overrides refuse admission before Key injection;
the later binding composition stage must not silently overwrite a selection.

The receipt is `.hyprial-loader-receipts/<sha256(sessionId)>.json`, with top-level
schema `hyprial-pi-loader-v1` and a version-1 `modelBinding` snapshot. Resume must
match binding reference, connection/config revision, exact model and template
limits. Authorization/binding revision alone does not rewrite the original
snapshot. Missing provenance or incompatible schema refuses before a new receipt
is written. Legacy sessions without a binding remain legacy; a bound receipt
cannot downgrade to legacy. Resume never substitutes a fresh session.

## Diagnostics and verification

`agent.start` declares `MODEL_SESSION_BUSY` (admission/session-reserved),
`MODEL_RESUME_MISMATCH` (resume/binding-changed, model-changed or provenance-missing),
`MODEL_COMBINATION_UNSUPPORTED` (admission/mode, sdk-version, runtime-option or
session-storage), and `MODEL_AUTH_UNAVAILABLE` (authorize/missing). Their HTTP
statuses are 409, 409, 400 and 403 respectively. Only closed adapter diagnostics
become structured phase/reason data; arbitrary stderr is not copied into it.

Turn failures use `MODEL_UPSTREAM_ERROR: request/<reason>` with no upstream body
or credential. `model-not-found` requires the upstream's explicit
`model_not_found` code; generic 404 is `request-rejected`. HTTP 200 proves neither
model capability nor that an external gateway avoided internal substitution.

Acceptance lives in `tests/daemon/harnesses/pi/connections/`,
`tests/daemon/ipc/contracts/agent/inventory/test_pi_connection_start.py` and
`tests/kernel/manysys/test_fs_locality.py`. It uses actual Pi SDKs, synthetic Keys,
isolated homes and loopback upstreams. It does not establish real vendor,
production binding, browser, custom TUI or cross-device acceptance.
