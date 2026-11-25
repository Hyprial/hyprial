# E2E-011: Lark App onboarding and one-click scope authorization

Status: every step up to and including presenting the verification URL is
automated. Creating a *real* App is a device-authorization flow that requires a
person to open that URL in Feishu/Lark and approve; that approval, the resulting
production App, and tenant-admin scope authorization are `[REAL ENV]` and are
not simulated.

Two commands share that flow. `hyprial adapter add` creates or adopts an App.
`hyprial adapter scopes authorize <name> --interactive` re-runs the same flow against the
App an adapter *already* has: because the scope/event declaration travels with
the link as the `addons` parameter, one confirmation both declares and
authorizes the scopes. That is the only path that reaches a scope the App never
declared -- the `scopes/apply` endpoint can request authorization only for
already-declared scopes and answers `nothing_to_request` otherwise.

The automated run drives the real commands against a loopback stand-in for the
accounts endpoint, `hyprial adapter add` through a real pseudo-terminal. Both
`domain` and `lark_domain` are pinned to that stub, so the official SDK's
Feishu-to-Lark tenant-brand switch cannot redirect the flow to a public host.
No production App (`hq-adjutant-feishu`, `hyprial-kanban`, `mac-studio-feishu`)
and no production credential is read or written.

## Preconditions

- The editable or installed `hyprial` executable contains the Python CLI and daemon.
- The runner owns disposable `HYPRIAL_HOME` and `HARNESS_STATE_DIR` roots under a
  short writable platform temp root (`/private/tmp` on macOS or `/tmp` on Linux).
- A pseudo-terminal can be allocated: the interactive branch is gated on
  `stdin.isatty()`, so a pipe would silently take the non-interactive path.
- Any ambient `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` is removed for the child
  process; a shell proxy would intercept the SDK's loopback requests.
- `[REAL ENV]` A dedicated Feishu test tenant and a human who can scan a QR code
  or open a verification URL, without reusing any production App.
- `[REAL ENV]` A tenant administrator who can approve the requested scopes.

## Steps

1. Run `hyprial adapter add <name> --json` with `HYPRIAL_LARK_APP_ID` and
   `HYPRIAL_LARK_APP_SECRET` set and assert the adapter registers with no request
   reaching the accounts endpoint.
2. Run `hyprial adapter add <name> --json` with no credentials in the
   environment and assert a structured `USER_ACTION_REQUIRED` refusal that names
   the rerun command and both environment variables, writing no state.
3. Run `hyprial adapter add <name>` behind a pseudo-terminal and assert the
   official SDK performs `init`, `begin`, and polling (including one
   `authorization_pending` round) against the registration endpoint only.
4. Assert the verification URL is written to stderr for the user and is the
   SDK-transformed form (`from=sdk`, `tp=sdk`, `source=python-sdk/hyprial`), not the
   raw URI the platform returned.
5. Decode the URL's `addons` parameter (urlsafe-base64 of gzipped JSON) and
   assert it declares exactly `im:message`,
   `im:message.group_at_msg:readonly`, and the `im.message.receive_v1` event.
6. Assert the app preset pre-fills `Harness Bridge <name>` and that a plain run
   sends neither `createOnly` nor `clientID`.
7. Assert the returned credential is persisted: the app id in `channels.json`
   with an empty route set, the secret at mode 0600.
8. Run with `--new` while environment credentials are set and assert
   `createOnly=true`; run with `--app-id <id> --route ... --secret-file ...`
   and assert the supplied App is registered without an accounts-endpoint call.
9. Re-run `hyprial adapter add <name> --new` against an already-registered name
   and assert it fails with zero further requests reaching the registration
   endpoint.
10. Run `hyprial adapter scopes authorize <name> --interactive --json` against an onboarded
    adapter and assert the link carries `clientID=<the adapter's app id>` with
    no `createOnly` and no App-creation preset (`name`/`desc`), that stdout is
    exactly one JSON value, and that the link is on stderr.
11. Decode that link's `addons` and assert it declares only the required
    scopes; re-run with `--capability group-history` and assert it adds exactly
    `im:message.group_msg` and names that scope in the stderr prompt.
12. Run `hyprial adapter scopes authorize <name> --capability group-history` without
    `--interactive` and assert `INVALID_ARGUMENT`.
13. Start the daemon on the resulting home and assert `hyprial adapter list` loads
    the onboarded route-less gateways.
14. `[REAL ENV]` Open the presented URL in a dedicated test tenant, approve the
    App, and assert the returned `client_id`/`client_secret` register a working
    adapter.
15. `[REAL ENV]` Run `hyprial adapter scopes authorize <name> --interactive`, have the App
    owner open the link and confirm, then confirm the grants with
    `hyprial adapter scopes check <name>`.

## Assertions

- Environment credentials short-circuit onboarding entirely: the adapter is
  registered and the accounts endpoint receives zero requests.
- Without a human and without environment credentials, onboarding refuses with
  a structured `USER_ACTION_REQUIRED` action carrying the exact rerun command
  and both environment variable names, and writes no gateway entry.
- `--new` ignores present environment credentials and requests a brand-new App;
  `--app-id` selects registration-only mode for an existing credential and does
  not create, authorize, or otherwise mutate a platform App.
- The verification URL is presented to the user on stderr, never only in a log,
  and is the SDK-transformed URL rather than the raw platform URI.
- The App is declared with exactly the scopes satisfying every required gateway
  capability plus the single inbound event the worker subscribes to, verified by
  decoding the parameter the platform will actually receive.
- A registration that is already doomed -- a taken or malformed adapter name --
  fails before the device-authorization flow starts. Human approval creates a
  real App on a real tenant and cannot be undone, so a preventable error must
  never cost one.
- `adapter scopes authorize --interactive` re-authorizes the adapter's *existing* App:
  it sends that app id as `clientID`, never `createOnly`, and never the
  App-creation preset, so it cannot create an App or rename a live one. That
  name-clash guard is deliberately absent here -- it protects the creation path,
  and the adapter this command targets is required to exist already.
- The one-click link is a handoff on stderr, so `--json` stdout remains exactly
  one JSON value even while the link is being shown.
- Privacy-widening scopes are opt-in and audible: a plain interactive authorize
  declares only the required scopes, `--capability group-history` adds exactly
  `im:message.group_msg` and names it in the human-facing prompt, and
  `--capability` without `--interactive` is refused rather than silently
  dropped by an endpoint that cannot honour it.
- A newly onboarded gateway has no named outbound route (the App has never been
  messaged, so no chat or user id exists yet) and the daemon still loads it.
- No `client_secret` byte appears in any stdout, stderr, or evidence output, and
  no production App name is referenced.
- `[REAL ENV]` App creation itself cannot be automated: the platform requires a
  human to complete the device-authorization approval. A run without that human
  is `BLOCKED`, never `PASS`.

Run:

```sh
uv run python contract/e2e/run-lark-app-onboarding.py
```
