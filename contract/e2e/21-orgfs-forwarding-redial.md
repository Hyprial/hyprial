# E2E-021: OrgFS remains live across a slot-pool-growth redial

## Preconditions

Two real daemons use disposable homes and state directories. Their Zenoh
listeners and connections are explicit private Unix endpoints, system peer discovery,
gossip, usage fetches, and automatic forwarding are disabled, and no Tailnet or
Headscale service is used. `real_environment_required: false`.

The daemon intentionally forbids its forwarding backend when
`HYPRIAL_NETWORK_ISOLATED=1`, so this runner uses the narrower loopback-only
topology and verifies the effective endpoints through `hyprial daemon status` before it
continues. Both nodes use a protocol-v7 sidecar fixture with leased roster
exchanges and owned Unix stream proxies, without Tailcat or DERP. It accepts
and reports the daemon's fixed slots and records exact
`map-peer(peer, address, slot)` requests. The daemon starts with a one-slot
pool. A leased peer binds the existing slot without a redial; a second leased
peer exhausts the pool, grows it, and therefore exercises the sole legitimate
forwarding-driven rebuild path. Directory-only publication is a negative
control and must not change admission or produce a redial.

Run with an explicit executable from the checkout under test:

```sh
PYTHONPATH=$PWD/src HYPRIAL_BIN=$PWD/.venv/bin/hyprial \
  uv run --no-sync python contract/e2e/run-orgfs-forwarding-redial.py
```

The runner has a 240-second overall deadline. Every individual wait is bounded,
and teardown addresses only PIDs read from its disposable daemon metadata and
sidecar PID files. SIGINT, SIGTERM, timeout and assertion failures all unwind
through cleanup. An exited zombie is distinguished from a running process;
unexpected liveness probes never count as successful cleanup.
The outer deadline and interrupt exception bypasses transient probe retries;
it cannot be swallowed as an ordinary assertion while polling.
An interruption during graceful teardown still reaches both owned PID stops;
an interrupted TERM grace still escalates to KILL and confirms process exit.

## Steps

1. Start daemon A with a protocol-v7 forwarding fixture and one fixed slot, then create
   an orgfs space plus the `group-redialnet` directory space. Invite B's user
   before B starts, so A's orgfs mesh and
   liveliness observer already exist when B appears. Register a bounded set of
   64 local interactive fixture actors through public `session.register` IPC so session replay
   exercises callback-channel pressure while remaining below the fixed
   transport's 4096-entry handoff capacity. Preserve every returned official
   `a.<id>` URI and verify all 64 identities against the public agent list.
2. Start B with its own private Unix listener and publish an explicit leased
   roster connecting the two nodes through daemon-selected slots.
   Register a real interactive actor on B and require A's public `targets`
   output to report it online. Join B to A's orgfs space and require
   product-visible membership plus an A-authored orgfs update reaching B.
3. Require the first leased peer to bind slot 0 without a redial. Write an
   owner-authored fixture-device row through the public fs CLI and require
   no admission change. Publish its matching leased roster entry; it exhausts
   the one-slot pool, grows slot 1, and calls
   `ZenohTransport.reconfigure_connect`; no production diagnostic switch or
   direct internal method call is used.
4. Require `zenoh.forwarding.redialed` and `maintenance.stalled == false` in
   `hyprial daemon status`. A healthy pump need not emit stale-fence `yielded` events.
5. Require B to observe the exact 64 official online identities within the
   original 20-second replay window, and require another OrgFS update to
   replicate after redial. Explicitly pin A as the fixture's authority using
   the public `org pin-authority` command before B restarts, so its legacy
   directory import follows the ordinary migration path. Stop B, queue A to B
   using B's official agent URI, restart B, require a fresh authority exchange,
   delivery and a new
   `inbox.retry_pump.completed` record with `inboxResults >= 1`, and no
   `inbox.retry_pump.failed` during that window. Use B's public reply path
   back to A after the redial, then acknowledge the reply.
6. Reject `daemon.maintenance.stalled` in structured logs and
   `zenoh.lock.wait_exceeded` in either daemon launch log; stop both daemons and
   both sidecars by their owned PIDs on every exit path.

## Assertions

- A reports B's registered actor online before redial. The two users are
  members of one genuinely joined orgfs space, and an A-authored update reaches
  B before forwarding changes. Because A created the space before B connected,
  B's successful join also proves A's liveliness observer re-announced the space
  when B appeared.
- Binding inside the initial pool produces no redial. Exhausting that pool
  adds exactly one fixed slot, preserves slot 0, and produces
  `zenoh.forwarding.redialed` with both slots.
- The maintenance watchdog remains unstalled and the retry pump continues to
  deliver queued work after the redial.
- All 64 registered official actor identities are online after replay; an
  alias prefix or unrelated actor cannot satisfy this assertion.
- A request A to B and its correlated reply B to A both deliver after redial.
- Neither daemon logs `zenoh.lock.wait_exceeded` or
  `daemon.maintenance.stalled`.
- The scenario emits exactly one terminal PASS or FAIL result and leaves no
  live daemon or sidecar process behind.
