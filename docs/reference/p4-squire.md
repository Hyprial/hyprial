# P4 Squire migration

P4 migrates Squire v3 into the Python runtime as independently reviewable
vertical slices. The first slice owns the local Squire identity and setup
state; delivery, the five-function pack, and scheduling build on these public
seams in the next slice.

## P4-1: setup and profile core

`hyprial squire setup` is a seven-step, re-entrant state machine:

1. register a managed, headless Pi provider named `squire` in daemon desired
   state, with `deepseek-flash` as the default model;
2. expose `AppOnboardingPort` for dedicated-app creation and authorization;
   P4 deliberately returns `APP_CREATION_DEFERRED` because Allen excluded the
   platform workflow body from this phase;
3. recommend an app visibility range without enforcing it;
4. capture an app-scoped owner open_id behind a ten-minute misbinding code;
5. record that trusted-intranet access has no owner-only or cross-user ACL;
6. detect Claude Code and recommend the hyprial plugin when applicable;
7. point to the separate daemon service workflow.

Every step can be rerun with `--step <id>`. A single-step app check does not
create local Squire state. Repeating a completed operation with identical
inputs produces an empty `changed` array and byte-identical JSON state.

Squire is the user's ordinary agent named `squire`. Its registry-owned home is
`$HYPRIAL_HOME/agents/squire`, with work files in `workspace/` and its skill in
`config/shared/skills/squire/`. The agent uses an explicit config and the same
home provisioner as other agents; routines bind to its normal agent URI. Setup
requires its state directory to resolve inside `HYPRIAL_HOME`. On first setup,
the three legacy files (`~/squire/profile.md`, `backlog.md`, and
`skills/squire/SKILL.md`) are copied into the new layout when present. Old files
are retained; conflicting destination content stops the first import before
copying. Later setup runs preserve edits in the new home.
Known legacy paths inside the copied skill are rewritten to the new home;
the original skill keeps its bytes.
`--home` and its `--cwd` alias select a legacy source to read, never a new
agent home outside `HYPRIAL_HOME`.

Example local setup (the four host-side lookup flags are optional overrides
since 2026-09-18 — omitted, `machine` is derived from `HYPRIAL_NODE_ID`
(else the hostname), `owner-key`/`machine-key` from the owner/machine
slugs, and `login-name` from the platform (OS) login):

```sh
hyprial squire setup \
  --channel channel:gregpaws:mac-studio:squire \
  --json
```

Explicit overrides still work one for one, e.g. on a node whose id differs
from the hostname pass `--machine <node-id>`:

The first run returns `steps[owner-open-id].bindingCode`. After the dedicated
app captures its owner open_id, rerun with `--owner-open-id` and
`--binding-code`. The code prevents accidental channel mismatch; it is not an
authentication or authorization boundary.

The sender open_id is captured from the daemon log: once the adapter forwards
the owner's DM, the `send.received` entry in `state/logs/daemon.jsonl` carries
it as `senderOpenId`.

Setup fails fast on the two misconfigurations that otherwise produce
permanently undeliverable `user:` traffic behind a healthy-looking result:

- `--machine` must equal the daemon node id. When `HYPRIAL_NODE_ID` is exported a
  mismatch is an error; without it a hostname mismatch is a warning, because
  the daemon then defaults its node id to the hostname. (A derived `--machine`
  follows `HYPRIAL_NODE_ID`/hostname by construction, so only an explicit
  override can trip this — and it still fails loudly.)
- When `channels.json` exists, the last `--channel` segment must name a
  configured adapter; otherwise setup lists the configured names and stops
  before writing state. A missing `channels.json` downgrades this to a
  warning so setup can still run before any adapter exists.

## Outbound bootstrap from zero state

A fresh install gains `user:<owner>` delivery through exactly these steps:

1. Configure the Lark adapter in `channels.json` plus its
   `secrets/<credentialRef>.json` (migration or manual), then start the
   daemon and the adapter (`hyprial adapter start <name>`).
2. `hyprial squire setup ... --channel channel:<owner>:<machine>:<adapter>`.
3. The owner DMs the dedicated app once; read `senderOpenId` from the
   daemon `send.received` log entry.
4. Rerun setup with `--owner-open-id` and the `--binding-code` from step 2.

No daemon restart is required after step 4: on the first delivery that finds
the adapter unregistered, the receiver refreshes its registry from the
current `channels.json` and `users.json` once before failing.

## What `message.send` cannot deliver

`channel:<owner>:<machine>:<adapter>[:<route>]` targets are rejected with
`UNSUPPORTED_TARGET`; the error names the two deliverable alternatives.
The two gaps this section originally recorded have since closed:

- Proactive Lark route/group send now exists as `route:<adapter>:<route>`
  (fanout expansion included) — see `docs/reference/route-outbound.md`.
- Replies to pending messages that arrived from a Lark adapter are now
  bridged through the supervised adapter worker's native reply API — see
  the "Correlated Lark reply bridge" section of `docs/reference/p3-mcp.md`.
  `MESSAGE_REPLY_UNAVAILABLE` remains only for a malformed channel actor or
  an adapter name absent from persistent configuration.

The invariant both paths preserve is unchanged: an undeliverable send never
reports `ok: true`.

## Persistent state

- `state/desired-state.json.v1`: managed Pi Squire launch specification.
- `state/users.json`: version-1 daemon-owned profiles, including associated
  agents, Squire channel, app-scoped owner open_id, preferred receiver, and
  notification rules.
- `state/squire-setup.json`: pending ten-minute binding codes only.

All files use serialized atomic replacement and mode `0600`. Changing the
Squire channel clears a previous open_id binding. Profile binding intentionally
does not enforce owner identity, machine ownership, or cross-user ACLs.

## Public seams for P4-2

- `UserProfileStore` is the owner and receiver lookup boundary for `user:`.
- `AppOnboardingPort` accepts a future Lark app-creation implementation without
  importing it into CLI or setup policy.
- `ProviderLaunchSpec` remains the only dependency from Squire into daemon;
  Pi process details remain provider-owned.

P4-2 will add receiver-Squire delivery, feature-pack services, and the daemon
timer/inject primitive. It must not reinterpret the misbinding code as auth or
send an app-scoped open_id through another app.

## P4-2: receiver-owned `user:` addressing

`harness_send` and `hyprial message send --to user:<owner>` resolve a user target through
the receiving daemon. The receiving daemon selects the profile's preferred
machine, obtains the app-scoped owner open_id locally, and asks that user's own
Squire adapter to send the DM. The sender's adapter never sees or reuses the
receiver's open_id.

The delivered text begins with `来自我的侍从，转述自 <sender>` so the platform
message is visibly a relay. If the receiving profile has no Squire adapter or
owner binding, the operation fails with `TARGET_SQUIRE_UNCONFIGURED` and
guidance that names `hyprial squire setup` as the remedy
(`UNCONFIGURED_SQUIRE_MESSAGE`); a configured but unregistered adapter fails
with `TARGET_SQUIRE_ADAPTER_UNAVAILABLE` after one registry refresh. No
introduction gate, impersonation check, ACL, or owner-only policy is applied.

Zenoh carries a receiver-owned delivery frame and a result receipt. Each
network attempt has its own receipt token, while reconnects retain the MCP
mutation id as the business idempotency key. The receiving daemon persists the
result in `state/user-deliveries.json`. At startup it resolves the profile's
adapter name against validated `channels.json`, loads that receiving app's
credential locally, and registers an outbound Lark adapter. The sender never
loads or receives those credentials.

The Lark adapter derives a stable 36-character provider `uuid` from the
unbounded business key. This satisfies the platform's 50-character limit and
suppresses retry duplicates inside Lark's documented idempotency window. The
persistent ledger handles completed operations across restart; the system does
not claim unlimited exactly-once delivery after an ambiguous provider call.

The profile's persisted v1 key remains `squireChannel` for TypeScript migration
compatibility. Python routing exposes it as `squire_adapter`; `channel` in new
code and documentation is reserved for IRC channels.
