# Lark App onboarding: programmatic App creation and authorization

`hyprial adapter add <name>` creates (or adopts) the Lark App backing a
gateway through the **official device-authorization flow**. Creating an App is
not a manual console procedure: the only human step is one approval in a
browser or via QR code, which the official flow structurally requires. Every
behavior stated here is implemented in the Lark credential modules and the
`adapter add` / `adapter scopes authorize` CLI commands.

## Credential resolution: three ordered outcomes

Onboarding answers one question — which App credential should this gateway
use? — with three ordered outcomes:

1. **Environment credentials, used verbatim.** If `HYPRIAL_LARK_APP_ID` and
   `HYPRIAL_LARK_APP_SECRET` are both set and the caller did not pass `--new`,
   they are used directly. No network call happens and no browser step is
   needed. `--new` demands a brand-new App and deliberately bypasses this
   short-circuit; otherwise "make me a new App" would silently return the
   ambient one.
2. **Non-interactive caller: fail with instructions.** With `--json` or a
   non-TTY stdin, the command cannot prompt a human, so it fails with
   `USER_ACTION_REQUIRED` carrying a structured action: the exact command to
   rerun (`hyprial adapter add <name>`) and the exact environment variables to
   set (`HYPRIAL_LARK_APP_ID`, `HYPRIAL_LARK_APP_SECRET`). The device-authorization
   flow requires a person, so failing with instructions is the only honest
   answer for an unattended caller.
3. **Device-authorization flow.** Otherwise the official SDK flow
   (`lark_oapi.register_app`) runs: it mints a verification URL/QR code that a
   human must open, and returns the created App's `client_id`/`client_secret`.

Two inputs select distinct paths, and `--app-id` and `--new` are mutually
exclusive:

- `--app-id <id>` selects registration-only mode for an existing App. Supply
  its secret through `--secret-file`, stdin, or the hidden prompt; no platform
  App is created or changed, and at least one `--route` is required.
- `--new` always creates a new App, ignoring the environment credentials.

Before any human approval is requested, the command validates the adapter name
and rejects a name clash (`ADAPTER_EXISTS` unless `--force`). The approval
that creates a real App on a real tenant cannot be taken back, so a name typo
must cost nothing rather than strand an App nothing will ever reference.

## What a created App is declared with

The registration request declares the minimal tenant scope set the gateway
needs plus the one event the inbound worker subscribes to:

- Tenant scopes: `im:message`, `im:message.group_at_msg:readonly`
- Event: `im.message.receive_v1`

The scope set is defined as `LARK_ONBOARDING_REQUIRED_SCOPES` in
`onboarding.py`, and `tests/test_lark_onboarding.py` asserts that it satisfies
every `requirement == "required"` capability in
`LARK_GATEWAY_CAPABILITIES` (`message-send`, `receive-p2p`,
`receive-group-mention`) rather than trusting two lists to stay in sync.

Consequence: **a freshly created App can receive and reply to messages without
a second visit to the developer console.** Optional capabilities (for example
`group-history`, i.e. `im:message.group_msg`) are deliberately not part of the
creation declaration; see the re-authorization section below.

The flow also sends `app_preset` values (name and description) that pre-fill
the web App-creation page; they are conveniences, never authoritative.

## The one human step, and why it exists

The device-authorization handoff is a verification URL printed to **stderr**
(with its expiry, rounded to minutes):

- stderr rather than stdout because `--json` owns stdout as exactly one JSON
  value; this also keeps the URL visible when stdout is piped.
- It is a direct write, not a log record: the flow blocks until a human opens
  the URL (or scans it as a QR code) with the Feishu/Lark app of the tenant
  that should own the App, so the handoff must not scroll past. Ctrl-C aborts.

This approval is the **only** manual step in App creation. It cannot be
removed or automated: it is a structural requirement of the official
device-authorization flow, which exists precisely so that a tenant human
consents to the App's creation and scope declaration.

## Credential storage: written at 0600, never printed

The App secret is never logged, printed, or included in command output. It
flows from the SDK result straight into `add_lark_gateway`, which persists it
as `secrets/lark-<name>.json` (JSON key `appSecret`) via an atomic write at
mode **0600**, with the parent directory created at 0700 if missing. The
`--json` summary deliberately omits the secret; it reports only
`onboarding.source` (`environment` or `device_authorization`),
`onboarding.createdApp`, and — for the device flow — the verification URL and
its expiry.

Routes are optional on the onboarding path (`--route name=native_chat_id`,
repeatable): a brand-new App has not been messaged yet, so its chat ids are
typically unknown. `hyprial adapter add` asks a running daemon to hot-reload
the new adapter after registration and reports the result as `daemonReload`.
When no daemon answers, nothing is lost because it reads the same config at
its next start. Then run `hyprial adapter scopes authorize <name>` to request
tenant-admin approval for the declared scopes, pin an agent, and start the
adapter.

## Re-authorizing an existing App: declare + authorize in one confirmation

`hyprial adapter scopes authorize <name> --interactive [--capability <id> ...]` runs the
same official device-authorization flow against the adapter's **existing** App
(`reauthorize_lark_app` in `onboarding.py`). Because the scope/event
declaration rides along with the authorization request as the `addons`
parameter, **a single human confirmation both declares the listed scopes on
the App and authorizes them for the tenant** — no developer console visit.

This is the only path that reaches a scope the App never declared: the v6
`scopes/apply` endpoint used by plain `hyprial adapter scopes authorize` can only request
scopes that are already declared (see
[lark-app-permissions.md](../domains/lark-app-permissions.md)).

Properties of the re-authorization path, by construction:

- `HYPRIAL_LARK_APP_ID`/`HYPRIAL_LARK_APP_SECRET` are never consulted. The caller named
  a specific App; an ambient credential cannot stand in for it, and
  short-circuiting would silently skip the authorization that was asked for.
- It cannot create an App: `create_only` is never sent and `app_id` is
  mandatory. No `app_preset` is sent either — pre-fill values have no meaning
  for an App that exists, and withholding them removes any chance of a name or
  description reaching a live App.
- The returned `client_secret` is neither read nor returned: authorizing an
  App we already hold a credential for must not rotate or re-persist that
  credential.
- The platform-confirmed `client_id` must equal the adapter's `app_id`;
  a mismatch raises `LarkOnboardingError` and nothing is changed locally.
- The scope list is derived from gateway capabilities
  (`capability_scopes`): required capabilities are always included; optional
  capabilities are never requested implicitly and must be named with
  repeatable `--capability` (see `hyprial adapter scopes check <name>` for the ids).
  Privacy-widening capabilities are called out by name in the prompt —
  `group-history` grants `im:message.group_msg`, which lets the App read every
  message in its groups rather than only @-mentions.
- The event declaration is unchanged (`im.message.receive_v1`): the inbound
  worker subscribes to exactly one event whether the App is new or old.

## Endpoint override

`HYPRIAL_LARK_ACCOUNTS_DOMAIN` points the flow at a self-hosted or test accounts
endpoint. Both the Feishu and the Lark domain are overridden together, so the
SDK's `tenant_brand` switch can never fall back to the public endpoint
mid-flow.

## What remains manual

App creation and scope declaration are covered above. Still manual (detailed
in [lark-app-permissions.md](../domains/lark-app-permissions.md)): App version
publication, tenant-admin approval of `scopes/apply` requests, availability
scope, enabling bot capability, and adding the bot to a chat.
