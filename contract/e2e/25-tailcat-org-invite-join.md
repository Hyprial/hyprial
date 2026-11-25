# E2E-025: tailcat org invite → join → orgfs communication

## Preconditions

Two real daemons use disposable homes and state directories, each with its
own device key (minted through the sidecar's `genkey`, the login flow's
job performed here directly) and its own real `hyprial-tailcat` sidecar.
Both sidecars meet through the local labrelay DERP fixture, so every leg —
Zenoh listeners, Tailcat tunnel, DERP — is loopback.  Peer discovery,
gossip, and usage fetches are off; there is no host tailscale in the
product at all.  `real_environment_required: false`.

The daemon forbids forwarding under `HYPRIAL_NETWORK_ISOLATED=1`, so this
scenario is the one sanctioned user of `IsolatedDaemon`'s
`tailcat_forwarding=True` opt-in: the blanket switch is replaced by the
narrower guarantees `require_tailcat_forwarding_isolation()` asserts —
loopback-only Zenoh endpoints from the daemon's own `ps`,
`HYPRIAL_PEER_DISCOVERY=0`, `HYPRIAL_ZENOH_GOSSIP=0`,
`HYPRIAL_USAGE_DISABLE=1` — plus the runner's own half,
`HYPRIAL_TAILCAT_DERP` pinning both sidecars to the local labrelay.

Required executables (missing ⇒ `RESULT BLOCKED`, never a skip):

* `HYPRIAL_TAILCAT_BINARY` — the pinned sidecar build (forwarding protocol
  v5, tailcat `b4dc28e8`)
* `HYPRIAL_TAILCAT_LABRELAY` — the local DERP relay; it prints one
  `{"v":3,"derp":{...}}` line on stdout and exits when its stdin closes

Run with an explicit executable from the checkout under test:

```sh
PYTHONPATH=$PWD/src HYPRIAL_BIN=$PWD/.venv/bin/hyprial \
  HYPRIAL_TAILCAT_BINARY=/path/to/hyprial-tailcat \
  HYPRIAL_TAILCAT_LABRELAY=/path/to/hyprial-tailcat-labrelay \
  .venv/bin/python contract/e2e/run-tailcat-org-invite-join.py
```

The runner has a 300-second overall deadline.  Every wait is bounded, and
teardown stops exactly the two daemons it started plus the labrelay
(stdin EOF, then SIGTERM/SIGKILL backstop).

## Steps

1. Start the labrelay and read its `derp` config; pass it to both daemons
   as `HYPRIAL_TAILCAT_DERP`.
2. In alice's and bob's disposable homes, ensure the device key and record
   (`hyprial.daemon.ensure_device_key`, owner `alice`/`bob`, device id = the
   node id `a`/`b`).
3. Start both daemons with `HYPRIAL_FORWARDING=on`, the explicit loopback
   Zenoh listener as the forwarding inbound target, and the pinned sidecar
   binary.  Wait for both Tailcat address files (bounded).
4. Alice: `hyprial org create group-demo --json` (device published), then
   `hyprial org invite group-demo --user bob --json`; take the https link.
5. Bob: `hyprial org join - --json` with the link on stdin; require
   `ok` and `org=demo` with every bootstrap step reporting `ok`.
6. Verify, all with bounded waits:
   * both nodes' `hyprial org network demo --json` list devices `a` and `b`;
   * alice `fs write`s a document into the org space and bob `fs read`s the
     same content; then bob writes and alice reads;
   * path proof on bob's `hyprial daemon status --json`: `forwarding.endpoints` are
     sidecar-mapped `tcp/127.0.0.1:<port>` entries, all present in bob's
     Zenoh connect set, and alice's real Zenoh listener is absent from
     that set — business traffic rides Tailcat, never a direct dial;
   * seed alice's accepted org-context, then bob calls `org.fetch` with the
     GUI's `requestMode=neighbors` over real IPC: exactly one answer, one
     staged candidate from `a` (publisher `alice`, version 2), no rejection;
     after alice stops, the same call returns zero answers/candidates and
     never reports a fabricated success.
7. Stop both daemons and the labrelay; write the evidence JSON to the
   artifacts directory.

## Assertions

- `org create` publishes alice's device; `org invite` returns the https
  link form; `org join` decodes it and runs the full bootstrap
  (connect-inviter → join-org → register-device → connect-members).
- The directory on BOTH nodes converges to exactly devices `a` and `b` —
  bob's row reaches alice and alice's row reaches bob through OrgFS sync
  over the Tailcat path.
- OrgFS documents replicate in both directions within bounded time.
- Bob's forwarding endpoints are sidecar-mapped loopback ports and are
  what his Zenoh session dials; alice's listener never appears in bob's
  connect set.
- `org.fetch` with `requestMode=neighbors` stages alice's seeded context on
  bob as a candidate and preserves the requested/responded/received/rejected
  counts; with alice stopped it returns an empty answer rather than success.
- The scenario emits exactly one terminal PASS, FAIL, or BLOCKED result
  and leaves no daemon, sidecar, or labrelay process behind.
