# E2E-028: remote drop returns through a fixed slot without rebuild

## Preconditions

The runner uses two disposable daemon homes and builds both
`hyprial-tailcat` and `hyprial-tailcat-labrelay` from this checkout's
`sidecar/hyprial-tailcat` module with the repository's normal read-only,
trimmed Go build. It never resolves or executes a production sidecar binary.
The Go toolchain required by the module must be available. Missing or broken
build prerequisites are a failure because this is an isolated source gate,
not a real-environment scenario. `real_environment_required: false`.

Both real sidecars use the local labrelay DERP. Zenoh listeners, slot sockets,
and every relay leg stay inside the disposable loopback topology. Peer
discovery, gossip, and usage fetches stay disabled through the existing
Tailcat-forwarding isolation guard.

Run the checkout's Python environment explicitly:

```sh
PYTHONPATH=$PWD/src HYPRIAL_BIN=$PWD/.venv/bin/hyprial \
  uv run --locked --python 3.12 python \
  contract/e2e/run-remote-drop-no-rebuild.py
```

The runner has a 300-second outer deadline. Every build and observation has a
smaller bound. Teardown owns only the two disposable daemons, their sidecars,
and the labrelay.

## Steps

1. Build the real sidecar and labrelay from this tree into the scenario's
   disposable root, then start the loopback labrelay.
2. Mint separate device keys for two isolated daemons, start both with real
   sidecars, create `group-remote-drop`, invite the second owner, and complete
   the join through the public CLI.
3. Wait for both OrgFS directory views to contain both devices and for the
   one-dialer rule to expose an active peer-to-fixed-slot assignment. Send and
   acknowledge one baseline message.
4. Record the peer's exact directory address, the fixed forwarding endpoint
   set, and baseline counts for forwarding-change and transport-rebuild events.
5. Terminate only the dialling daemon's owned sidecar. This drops the slot path
   carrying its remote peer while both daemons and the OrgFS directory remain
   in place. Wait for the supervisor to launch a new sidecar and rebind the
   same peer to the same fixed endpoint set.
6. Require the peer's directory address to remain byte-identical. Send and
   acknowledge a new message after the restored slot is active.
7. Compare the daemon log with the baseline and clean up all owned processes.

## Assertions

- Both executables were built from `sidecar/hyprial-tailcat` in the checkout;
  no ambient or production sidecar binary is used.
- Before the drop, a real sidecar slot carries an acknowledged message between
  two nodes whose OrgFS directories have converged.
- The sidecar PID changes, but the peer's OrgFS directory address and the
  dialling daemon's fixed forwarding endpoint set do not.
- After return, `zenoh.forwarding.redialed == []` and
  `zenoh.forwarding.changed == []` relative to the pre-drop baseline.
- The transport reconfigure path is not entered: there is no
  `zenoh.forwarding.redial_failed`, no `zenoh.session.rebuilt`, and no
  `zenoh.session.closed` with `reason=reconfigure` after the baseline.
- A message sent after return reaches the peer and is acknowledged.
- The scenario emits one terminal PASS or FAIL and leaves no owned daemon,
  sidecar, or labrelay process behind.
