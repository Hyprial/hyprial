# App Core (browser chat slice)

`core/index.mjs` is the first browser-only Core slice: a static ESM module that
projects sessions, history, progress and terminal state from one injected
connection port, plus a chat entry page that can be built with
`npm run build:core` (`--out <dir>`, default `dist/core`).

## What Core is and is not

- It is a **projection**. Sessions, replies and completion come only from what
  the injected port returns; an acknowledgement is shown as accepted, never as
  completed, and no reply is synthesised.
- It is **browser scoped**. The runtime closure of `core/index.mjs` may not
  import Node builtins, external packages or another GUI module, may not use
  network or storage globals, and holds no local persistent business authority.
  `scripts/check-architecture.mjs` and `scripts/build-core.mjs` enforce both
  properties; `npm run build` (the product SSR/server build) is untouched.
- It binds **no identity**. The browser never supplies a principal, session
  scope, credential or trusted context. A native/server composition root
  authenticates the user, resolves the trusted context and projects its session
  adapter onto the five named operations below. Caller-supplied identity fields
  are rejected with `CORE_IDENTITY_REJECTED`, including identity query
  parameters and identity fields on the injected bridge object.

## Injected connection port

```js
const port = {
  sessions: {
    list(),                                   // -> [{ sessionId, title?, updatedAt?, state? }]
    history(sessionId, { cursor?, limit? }),  // -> { events, nextCursor }
    events(sessionId, since),                 // -> { entries, terminal }
    send(sessionId, { text, idempotencyKey }),// -> ack { status: 'accepted', commandId, acceptedAt?, deduplicated? }
  },
  capabilities: { get() },                    // -> capability flags
};
```

There is no generic request/command channel, no `cancel`, no URL, no shell and
no spawn. Any other operation on the port is rejected with
`CORE_CONNECTION_INVALID`, and Core passes only the request arguments above —
never an identity field — to the port.

`transport` is required metadata describing how the injected implementation
reaches the node: `{ kind: 'local-ipc' | 'tailnet' | 'mock', label? }`.
`local-ipc` and `tailnet` are **not implemented here**: Core contains no
networking, no address and no credential, and the real tailnet binding, the node
identity check (N-A2) and the node-side app adapter are separate, unfinished
slices. `mock` is accepted only alongside the explicit demo selection below, and
address/credential fields on the transport metadata are rejected.

An optional `powerPack: { available: boolean }` capability may be injected by
the composition root. It is separate from the transport, absent by default, and
this slice reports availability only — no process management, no commands.

## Usage

```js
import { createChatCore, createDemoConnection, mountChat, startBrowserChat } from '@hyprial/gui/core';

// Native/server composition (identity bound outside Core):
const core = createChatCore({ port, transport: { kind: 'tailnet', label: 'node A' } });
await core.connect();

// Static browser entry: no bridge -> visibly disconnected, composer disabled.
startBrowserChat({ document, location });            // reads __hyprialAppCore when injected
startBrowserChat({ document, location });            // ?demo=1 -> explicit simulation
```

## Demo mode

`?demo=1` (or `createDemoConnection()`) builds an isolated in-memory simulation:
no network, no daemon, no durable store, and a visible `Demo` badge. Every row it
returns carries `simulation: true` and its terminal state is `simulated`, so the
UI cannot present it as a finished real turn. Nothing else selects a mock; there
is no implicit fallback in the browser, and the demo cannot be combined with
identity parameters.

## Send semantics

- A new message gets a **new** idempotency key; the same request retried after an
  unknown outcome reuses the **identical** key and payload (`retrySend`), and
  Core never re-sends on its own.
- A definitive rejection (invalid argument, unknown session, conflict,
  unauthenticated) is reported as failed and is not retryable; anything else is
  reported as an unknown outcome (`uncertain`), not as a failure or a success.
- Session selection is epoch-guarded: a late response for a previously selected
  session cannot overwrite the current one.

This is an internal frontend port proposal, not a claim that these daemon API
routes exist. Bridge validation is not authentication: the node/native boundary
must verify every operation. An injected bridge always takes precedence over a
demo query; invalid bridges fail closed.

Disconnect retains an unresolved request in memory for same-instance retry and
invalidates late responses. Reloading the page or restarting the app loses that
retry handle. Durable resumption and multi-device cursors remain unfinished;
server deduplication alone cannot recover an idempotency key the client lost.

The build has four root files and a `runtime/` module directory, uses only
relative imports and external scripts/styles, and runs under a self-only CSP.

## Optional local discovery

A native-created Power Pack window may supply `__hyprialPowerPack` separately
from the chat bridge: `{capabilities:{available:true,operations:['status']},
status()}`. This read-only port displays the already owned daemon and its Agent
projection even when chat remains disconnected. Default Core has no such port.
The native command checks the exact local window/URL, keeps the owned child
handle, verifies `ps.daemon.pid` and running state, and checks for child exit
before exposing a whitelist of `agents[].actor/status` (maximum 200 rows).
Unknown ownership, missing shape, timeout or exit clears discovery instead of
claiming readiness. No raw ps, paths, environment, credential state or PID is
sent to the page. `ps` is the existing daemon query, which may perform its own
normal stale-route housekeeping; this feature adds no mutation command.

Opening/refreshing this view never starts a daemon or invokes a CLI, and offers
no lifecycle actions. Native status is not proof the App chat adapter is ready.
This is an additive App-internal port/command; it does not change the daemon
protocol or payload/phase contract. Real installed-runtime discovery needs a
separate authorized acceptance run; fixture/unit success is not that evidence.
