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
  adapter onto the named operations below. Caller-supplied identity fields are
  rejected with `CORE_IDENTITY_REJECTED`, including identity query parameters
  and identity fields on the injected bridge object.

## Injected connection port

```js
const port = {
  sessions: {
    list(),                                   // -> [{ sessionId, title?, targetUri?, updatedAt?, state? }]
    history(sessionId, { cursor?, limit? }),  // -> { events, nextCursor }
    events(sessionId, since),                 // -> { entries, terminal }
    send(sessionId, { text, idempotencyKey }),// -> ack { status: 'accepted', commandId, acceptedAt?, deduplicated? }
    create(targetUri),                        // -> { sessionId, targetUri, status, created }
  },
  targets: { list() },                        // -> { nodes, targets, counts, truncated }
  capabilities: { get() },                    // -> capability flags
};
```

`targets.list` is one bounded read-only directory read: `nodes` are the
daemon's own `hosts` rows and `targets` are its `agent` targets. Rows are
shape-checked, capped at 200 and report `counts`/`truncated`; status text is the
daemon's own string, and no role, model or online claim is invented. An agent
click sends only its `targetUri` to `sessions.create`; the node validates the
target against the same live directory and derives a stable session, then
`sessions.list` must list it before Core selects it. The browser never names a
sender, conversation, session id or extra target.

There is no generic request/command channel, no `cancel`, no URL, no shell and
no spawn. Any other operation on the port is rejected with
`CORE_CONNECTION_INVALID`, and Core passes only the request arguments above —
never an identity field — to the port.

`transport` is required metadata describing how the injected implementation
reaches the node: `{ kind: 'local-ipc' | 'tailnet' | 'mock', label? }`.
`local-ipc` and `tailnet` are **not implemented here**: Core contains no
networking, no address and no credential, and the real tailnet binding, the node
identity check (N-A2) and the node-side app adapter are separate, unfinished
slices. `mock` is accepted only by the explicit `createDemoConnection()` test
fixture below, and address/credential fields on the transport metadata are
rejected.

An optional `powerPack: { available: boolean }` capability may be injected by
the composition root. It is separate from the transport, absent by default, and
this slice reports availability only — no process management, no commands.

## Usage

```js
import { createChatCore, createDemoConnection, mountChat, startBrowserChat } from '@hyprial/gui/core';

// Native/server composition (identity bound outside Core):
const core = createChatCore({ port, transport: { kind: 'tailnet', label: 'node A' } });
await core.connect();

// Static browser entry: only an injected bridge produces a connected page.
startBrowserChat({ document, location });            // reads __hyprialAppCore
startBrowserChat({ document, location });            // no bridge -> visibly disconnected
```

The v11 shell adds a rail with 聊天 and 通讯录. 通讯录 shows the authoritative
node/agent directory from `targets.list`, the sessions `sessions.list`
returned, plus the optional native-owned local-agent discovery; a click on a
listed agent asks the node to create or reuse its session. Search, the
current-session chip, the directory refresh and the local pane switch make no
extra port calls. Fields the port does not carry (role, model) are shown as
unavailable; "创建 Agent" remains disabled and points at the local management
window. The 任务 entry is disabled with its reason stated.

At phone widths (360/390/430) the chat becomes a single pane: session list,
thread or info, with explicit back controls and `Escape` returning one level.
The layout does not impose a desktop minimum width.

The phone shell also owns the mobile viewport contract:

- the page opts into `viewport-fit=cover`; the app frame pads itself with the
  top/right/left safe-area insets and the composer keeps the bottom inset;
- `views/viewport.mjs` mirrors `visualViewport.height` into
  `--core-viewport-height`, so the frame shrinks with the on-screen keyboard
  (`100dvh` remains the no-script fallback);
- the message log is its own scroll frame (`overscroll-behavior: contain`);
  opening a conversation jumps to its latest message, and new messages follow
  only while the reader is already near the bottom;
- the composer uses a 16px font at phone width so iOS WebView does not zoom on
  focus.

These are presentation fixes only. Message-history continuity across restarts,
the outbox and Android hardware back are backend/shell work; this slice does not
simulate them.

## History semantics

- Loading, empty and failed history are distinct states. Only a settled,
  successful first page may render the "no readable messages" label; a failed
  request never claims the conversation is empty and keeps any already loaded
  messages on screen.
- `retryHistory` repeats the request that failed: a failed first page (including
  a failed refresh) is reloaded from the start, while a failed next page resumes
  from its recorded cursor. Load-more also requires both `hasMore` and a
  non-empty cursor, so a bare flag cannot issue an unusable request.
- A successful retry clears only the failure object it repeated. A newer,
  unrelated failure (for example a failed events read) stays visible instead of
  being hidden by the history success.

## Demo fixture

`createDemoConnection()` builds an isolated in-memory simulation for tests: no
network, no daemon, no durable store, and a visible `Demo` badge. Every row it
returns carries `simulation: true` and its terminal state is `simulated`, so the
UI cannot present it as a finished real turn. **The production page never
selects it**: `?demo=1` is treated as a disabled request and the page stays
disconnected. There is no implicit fallback in the browser, and the demo cannot
be combined with identity parameters.

## Send semantics

- A new message gets a **new** idempotency key; the same request retried after an
  unknown outcome reuses the **identical** key and payload (`retrySend`), and
  Core never re-sends on its own.
- A definitive rejection (invalid argument, unknown session, conflict,
  unauthenticated) is reported as failed and is not retryable; anything else is
  reported as an unknown outcome (`uncertain`), not as a failure or a success.
- Session selection is epoch-guarded: a late response for a previously selected
  session cannot overwrite the current one.

The desktop adapter host maps these operations onto the daemon's existing
`hosts`, `targets`, `message.send`, `message.history` and `progress.list`
surfaces. The remote facade is fail-closed for `targets.list` and
`sessions.create`: those operations have no authorization-matrix row, so a
remote device is denied until explicit per-session sharing lands. Bridge
validation is not authentication: the node/native boundary must verify every
operation. An injected bridge always takes precedence over a demo query;
invalid bridges fail closed.

Disconnect retains an unresolved request in memory for same-instance retry and
invalidates late responses. Reloading the page or restarting the app loses that
retry handle. Durable resumption and multi-device cursors remain unfinished;
server deduplication alone cannot recover an idempotency key the client lost.

The build has six root modules plus the `views/` view directory and a
`runtime/` copy, uses only relative imports and external scripts/styles, and
runs under a self-only CSP.

## Optional local discovery and management

A native-created Power Pack window may supply `__hyprialPowerPack` separately
from the chat bridge. Two exact shapes are accepted, and the declared
operations must match the members one for one:

```js
// read-only
{ capabilities: { available: true, operations: ['status'] }, status() }
// read-only + bounded management
{ capabilities: { available: true, operations: ['status','create','start'] },
  status(), create(name), start(name, harness) }
```

The read-only port displays the already owned daemon and its Agent projection
even when chat remains disconnected. Default Core has no such port at all. The
native command checks the exact local window/URL, keeps the owned child handle,
verifies `ps.daemon.pid` and running state, and checks for child exit before
exposing a whitelist of `agents[].actor/status` (maximum 200 rows). Unknown
ownership, missing shape, timeout or exit clears discovery instead of claiming
readiness. No raw ps, paths, environment, credential state or PID is sent to
the page. `ps` is the existing daemon query, which may perform its own normal
stale-route housekeeping.

With the management shape, the list rows carry a `启动` action (harness chosen
from claude/codex/pi/dsh) and a bounded `创建` field. Both run through the
shell's own CLI pipeline — the same bundled argv and environment the runtime
was started with — as `hyprial agent create --name …` and `hyprial start
<harness> --name … --headless`. `--headless` is the only start this window can
own: an interactive harness needs a terminal the page does not have, and
`start` creates the Agent first when it does not exist yet, so an explicit
`create` is optional. Names are checked against the daemon's own rule before
any process is spawned, harnesses are a fixed allowlist, and the shell reduces
every CLI envelope to one bounded outcome: success with the action's own
wording, or the CLI's failure code and message verbatim — never a raw Agent
object, path or environment, and never a success the shell did not receive.

Opening/refreshing this view never starts a daemon; create/start invoke the CLI
only through the runtime this window already owns. Native status is not proof
the App chat adapter is ready. This is an additive App-internal port/command;
it does not change the daemon protocol or payload/phase contract. Real
installed-runtime discovery and a real Agent start need a separate authorized
acceptance run with model credentials; fixture/unit success is not that
evidence.
