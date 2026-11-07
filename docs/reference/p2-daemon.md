# P2 daemon lifecycle boundary

The daemon composes the P1 protocols without importing their concrete
implementations: `TransportSession`, `PresenceView`, `DeliveryTransport`, and
`InboxPort`. Provider processes use the independent
`ProviderLauncher` / `ManagedProviderProcess` protocols so the concurrent
provider work can plug in without a daemon-to-provider implementation import.

## Desired state and recovery

`DesiredStateStore` writes schema v1 at `desired-state.json.v1`. The v1 object
contains managed providers, the `asMailbox` role, and the F4 expected
interactive-session registry (`actor`, `cwd`, `command`, and `source`). A
schema-v1 state also owns the deprecated shared-channel inventory and a
`channelPins` map that is legacy staging only: adapter pins live in the agents
database (`state/agents.sqlite3`, `pins` table), and entries left in
`channelPins` are pins the startup migration could not resolve yet (reported
at error level, never silently dropped). The TS-era `legacyConversationPins`
field is retired and no longer written. A
legacy provider array is backed up once as `desired-state.json.pre-v1` and
migrated atomically. The old path is then replaced with an invalid-provider
rollback guard. An old daemon therefore fails loudly instead of silently
restoring no providers.

Every `DaemonRuntime.start()` performs the same desired-state restore. The run
marker records whether the previous run was unclean, but never decides whether
restoration happens. Expected interactive sessions are reported as missing
when they are absent from the injected `PresenceView`; the daemon records and
guides these sessions but never starts an interactive TUI.

`run_until()` continuously reconciles crashed managed providers and due durable
inbox work. The live transport is the in-process `ZenohTransport`; durable
message state is held by the local SQLite store, so there is no second
`zenohd` supervision axis to reconcile.

## Home ownership and initialization

`hyprial init` owns HYPRIAL home creation. A first `hyprial login` with no home
runs that same initialization sequence before login and finishes by starting the
daemon; it does not carry a second home-creation implementation. Other commands
still fail loudly (`HYPRIAL_HOME_NOT_INITIALIZED`) when the configured home does
not exist, naming the resolved path and whether it came from the `HYPRIAL_HOME`
environment variable or the `~/.hyprial` default — a shell pointing at a deleted
test home must read as a configuration error, never as an empty-but-healthy bus.
`hyprial version` is the read-only exception and neither requires nor creates a
home.

A running daemon owns its home exclusively through
`$HYPRIAL_HOME/.active_daemon`: a JSON record with the owner pid, its
`darwin-starttime:`/`proc-starttime:` process identity, a random claim
generation, the UTC heartbeat time, and a monotonic timestamp, refreshed every
`keepalive_duration` (default 5 s, `HYPRIAL_DAEMON_KEEPALIVE_DURATION`). A second
daemon claiming the same home exits with `HYPRIAL_HOME_IN_USE` naming the owner
pid while the heartbeat is fresher than 2× the keepalive and the pid still
matches the recorded process identity. Stale, dead, or pid-reused records are
taken over; a sibling flock serializes claim/refresh transactions and the
generation check stops a superseded daemon from clobbering the new owner. The
marker is written via `O_EXCL` temp file + fsync + atomic rename, so a reader
never observes a half-written record. Staleness math uses the shared kernel
monotonic clock, never wall time. This fences double-daemon collisions only:
a husk that stopped heartbeating but never exited (card #158) is correctly
treated as absent and is not reaped by this mechanism.

## Doctor and OS services

`hyprial doctor` is the CLI projection of daemon `ps`: it verifies IPC liveness and
reports the live Zenoh, adapter, Channel, and historical-recipient health
fields. It does not instantiate a second diagnostic service or probe a retired
MCP HTTP port.

Packaged launchd and systemd templates preserve `HYPRIAL_HOME` and
`HARNESS_STATE_DIR`, restart after crashes, start at login/boot, use process
group shutdown, and keep logs in daemon state. `ServiceManager` provides
idempotent install/status/uninstall operations with an injected standalone
daemon handoff and command runner for CLI integration and isolated testing.

## Gates

```sh
UV_CACHE_DIR=/tmp/hyprial-p2-uv-cache uv run --extra test pytest -q
PYTHONPATH=src python contract/daemon-lifecycle/run.py
./contract/run-all.sh --isolated-only
```
