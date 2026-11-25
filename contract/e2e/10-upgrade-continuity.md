# E2E-010: upgrade a running deployment without losing continuity

Status: executable; fully automated in a disposable local environment.

This scenario closes the acceptance dimension that E2E-005 and E2E-002/003 do
not: **upgrading a deployment that is actually running.** E2E-005 exercises the
`uv` install/upgrade/uninstall mechanism and version resolution but never boots
a daemon; E2E-002/003 restart the *same* version. Neither crosses a version
boundary while a daemon, its connectors, a registered interactive session, and
in-flight messages are live. This runner does, and asserts the deployment comes
back on a *different* version with no lost work.

The version boundary is genuine. The **old** version is `v0.5.1`, the oldest
supported version containing the upgrade-safe channel logging fix `acc00a82d`.
It is installed whole and unmodified except for its synthetic version stamp.
The **new** version is the candidate under test. `HYPRIAL_E2E_UPGRADE_FROM`
remains an explicit diagnostic override. The runner probes the old executable's
help to select `daemon upgrade` or legacy top-level `upgrade`; its synthetic
version stamp must not determine the command surface. This also permits a
`HYPRIAL_E2E_UPGRADE_FROM=v0.5.5` diagnostic without changing the v0.5.1 path.
An already-loaded pre-v0.5.1 channel
cannot be retroactively repaired by installing a newer package, so keeping the
old `nightly` baseline made this continuity assertion permanently red.

The main case is explicitly **upgrade-continuity-same-interpreter**. It sets
`UV_PYTHON` to the initial interpreter and checks that the installed interpreter
minor version remains unchanged. This isolates package continuity from a Python
migration; it does not claim that an unpinned old CLI's default upgrade is safe.

The unsupported historical path is retained separately:
`python contract/e2e/run-upgrade-pre051-limitation.py` pins the pre-fix tree
`bbe80d4402394916a87f54ca2a605d0bdc71e0e1` and requires the exact `rich.table`
import failure after a successful old-channel startup. Its named
`KNOWN-LIMITATION` result is not a supported-upgrade gate pass. A different
failure or unexpected success fails that limitation check and requires review.

`python contract/e2e/run-upgrade-cross-minor-limitation.py` separately starts
v0.5.2 on Python 3.12, asks uv to use 3.14, and requires the old CLI's
`identity_transaction` import failure (product card `3066f189`). Neither known
limitation is a skip or supported-upgrade pass.

Lifecycle trigger: once the interpreter-pin fix ships (target v0.5.4), add a
forward case whose baseline contains the fix, whose `UV_PYTHON` is unset, and
whose uv default is 3.14; it must succeed while retaining the initial interpreter.
When the oldest supported baseline contains the fix, retire the cross-minor
known limitation and promote that forward case to the main continuity gate.

It reuses E2E-005's isolation: a temporary
`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`/`UV_CACHE_DIR`/`HOME`, a test-owned
`GIT_CONFIG_GLOBAL`, `UV_PYTHON_DOWNLOADS=never`, `HYPRIAL_PEER_DISCOVERY=0` (the
macos-host runner is a production host; the throwaway daemon must not join its
mesh), and no inherited `PYTHONPATH` (it would let the installed old daemon
import the candidate's source). The current canonical `harness-bridge.git`
source is redirected to a disposable bare remote. The real Forgejo repository
is **read once** — a `git fetch` of the old tag, with the host's own
credentials — and never written; ambient uv tools, caches, Python installs, and
home/Harness state are neither read nor changed. Every `HYPRIAL_HOME`,
`HARNESS_STATE_DIR`, socket, and worker process is disposable and test-owned;
the runner terminates only daemons and workers it spawned.

Connectors are exercised through the same fake-worker seam as E2E-007 (a worker
that signals readiness on the inherited `HYPRIAL_LARK_READY_FD` and idles), so no
Lark credential or network is used and the scenario is not gated on external
accounts.

## py upgrade semantics

py's `hyprial upgrade` resolves the highest PEP 440 version tag and runs
`uv tool install --force --compile-bytecode git+<url>@<tag>`. When the resolved tag changes and a
daemon is running, the command then gracefully stops that PID and launches the
upgraded daemon through the same file-backed, new-session `daemon run` path as
`hyprial init`. The result reports the PID and package version before and after the
restart. `--no-restart` is the explicit escape hatch; with no running daemon the
restart is skipped and explained. A restart failure exits nonzero while
retaining `upgradeCompleted: true` plus the resolved tag and commit.

## Preconditions

- `uv` and `git` are available and dependency downloads are allowed when the
  disposable uv cache is empty.
- The checkout is installable via the production-form Git SSH requirement and
  carries an `hyprial` console script; its current version may be development or
  stable because the disposable clone constructs the test version boundary.
- No production credential, socket, daemon, or `~/.hyprial` state is needed; the
  runner owns every daemon PID and worker it starts.
- Every explicit `init` probes the installed CLI and uses `--no-service` when
  supported. Legacy releases without that option retain their direct-launch
  interface; a failed help probe stops the scenario rather than assuming it is safe.

## Steps

1. Clone the current checkout, rewrite only the disposable clone to an old
   development version, and publish its version tag into a disposable bare Git remote.
2. Install the old package through the current canonical source and verify
   that `hyprial version` reports the expected old package version.
3. `hyprial init` to boot the running deployment; record the daemon PID.
4. Start the configured Lark adapters and confirm they report online, are
   recorded in desired state, and record their worker PIDs.
5. Start a real old-package stdio Channel child through an MCP client, and
   confirm `hyprial daemon status` reports its build/protocol and owner-fence telemetry.
6. Register an additional interactive session over daemon IPC and confirm it
   via `identity.whoami` and `hyprial daemon status`.
7. Send two in-flight messages and capture their stable `messageId`s plus the
   first message's pre-upgrade `deliveryId`. Capture the public `Agent.agent_id`
   of the Channel, interactive session, and driver from the installed old
   package's registry reader. The reader opens only a disposable SQLite backup
   obtained through a read-only source connection; its normal schema opening
   never writes the running fixture registry or exposes `entityToken`.
8. Publish version tag `v0.4.0` to the disposable remote and run `hyprial upgrade`
   under the live daemon; confirm the result reports the exact tag and commit,
   old/new PID and package version, and a completed automatic restart. The same
   held-open MCP client must survive, retain its honest start-time build
   version, and complete a fresh `harness_whoami` tool call against the new
   daemon; durable desired state alone cannot fake that call.
   Read those same logical records through the installed new public registry,
   require their original agent ids to remain unchanged, and pin their current
   canonical entity URIs. Session telemetry is compared with this independent
   mapping, retaining the original session/PID/birth, build/protocol, lease,
   current epoch, and held-client tool-call proofs. A recreated incarnation,
   foreign session, or different entity address remains a failure.
   The registry's static `covers` paths select impact checks; they do not
   establish a passing execution of this scenario.
9. Start a new-package Channel and drive the upgraded `hyprial agent start claude` path
    with an isolated fake Claude client. Assert its generated config contains a
    logical actor selector and PID/birth owner fence without recording the identity.
10. Restart once more; the same new held-open Channel client must complete a
    fresh tool call without MCP reconnect. Drain both messages in FIFO order
    and restart again to prove no replay. After restart, full-state reads wait
    at most 30 seconds on light `ping.restorePending`; a restore timeout or
    IPC failure remains a failure, never an invented empty inbox.

## Assertions

- The isolated `uv` install boots a live daemon on the old package version from
  the canonical source.
- Starting the configured adapters brings them online and records them in
  desired state.
- The interactive session registers and is visible to `identity.whoami` and
  `hyprial daemon status`.
- `hyprial upgrade` moves the running deployment to the `v0.4.0` tagged commit,
  reports `resolvedTag` + `resolvedCommit`, and records exact before/after
  `{pid, epoch, version}` facts. Both PID and epoch change, the after-version
  equals the installed version, and `restartRequired` is false. A legacy old
  CLI can omit both epoch fields: in that case old init/ping and an independent
  serving replacement ping must prove the epoch change and match the reported
  PID/version. Partial, null or wrong reported epochs fail; CLI success alone
  is never generation proof.
- On the upgraded deployment, an explicit `updateTrack: stable` resolves the
  movable `stable` tag as a no-op WITHOUT restarting the daemon (epoch
  unchanged), and a missing track tag (`updateTrack: nightly`) fails
  `UPGRADE_CHECK_FAILED` without falling back to the highest version tag.
- All connectors recover online with fresh worker PIDs after the upgrade
  restart.
- The interactive session re-registers as a fresh generation on the upgraded
  daemon.
- An old loaded Channel reaches the new daemon through the same held-open MCP
  client while retaining its honest old build version; replacing the package
  on disk does not rewrite it.
- A newly launched Channel reports the upgraded build/protocol and owner fence,
  survives a daemon restart without MCP client reconnect, and the generated
  launcher config uses its logical actor selector plus PID/birth fencing; the
  daemon resolves that selector to its official entity address.
- Pre-upgrade role names remain historical inputs. Current post-upgrade Channel,
  session, and driver reads/sends use the independently pinned entity URIs;
  supported bare CLI ACK aliases still exercise their normal public resolver.
  Neither legacy send-address acceptance nor a weaker lease is introduced.
- `hyprial doctor`/`ps` generation data is additive and does not claim that durable
  registration is a live process-health signal.
- Both pre-upgrade in-flight messages survive the automatic restart with stable
  `messageId`s and fresh `deliveryId`s, preserve FIFO order, and are not
  replayed after acknowledgement.
- The rows the old version wrote stay readable on the new version (C48):
  legacy `summary` stays `null`; if the old CLI supports `--summary`, its exact
  supplied summary survives. `bodyBytes` equals the exact body byte count,
  `kind` is `message`, and `message.read_body` returns the original body.
  The old send-help probe chooses the applicable summary form; it does not
  infer schema behavior from the fixture's synthetic version stamp.
  The same probe selects grouped `message send` or the pre-grouping top-level
  `send` command. Only an explicit missing `message` group permits that legacy
  probe; an import error or other failed help command remains a failed gate.
- The source checkout, real Forgejo `dev`, ambient uv directories, and
  production Harness state remain unchanged.
