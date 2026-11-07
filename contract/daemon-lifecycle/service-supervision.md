# Daemon service supervision contract

This contract covers the per-user launchd/systemd owner used by daemon lifecycle commands.

## Command surface

- `hyprial daemon service install [--yes] [--json]`
- `hyprial daemon service uninstall [--yes] [--json]`
- `hyprial daemon service status [--json]`
- `hyprial init --service | --no-service`

Install and uninstall require an interactive confirmation unless `--yes` is present. Unsupported
platforms fail with exit 1 and JSON code `SERVICE_PLATFORM_UNSUPPORTED`. A supervisor file or
loaded label that cannot be attributed to the exact Hyprial label/unit and executable fails with
`SERVICE_OWNERSHIP_REFUSED`; it is not unloaded, replaced, or removed.

The fixed user-service label is reserved for the default home, `$HOME/.hyprial` (resolved
paths). Install, uninstall, restart, and reconcile reject any other home before supervisor
commands, daemon probes, callbacks, or writes. A home outside the operating home retains
`ownershipReason = hyprial-home-outside-operating-home`; a non-default home inside it or a
`ServiceConfig.home`/`service_home` mismatch uses `service-home-nondefault`. Both use
`ServiceOwnershipError`, mapped by the service CLI to `SERVICE_OWNERSHIP_REFUSED`.

Changing `HOME` does not change a launchd UID domain or a systemd user manager. Before any
real supervisor command (including read-only probes), both configured operating homes must
match the current OS account's home, obtained independently of environment variables.
Otherwise the same typed error uses `ownershipReason = service-account-home-mismatch`.
Doctor reports this as a failed `daemon-service` check with `metrics.reason`; it does not
query the real user's manager on behalf of a disposable HOME. Injected command runners are
unit-test fakes; they do not execute OS supervisor commands.

The default production home and account-home path are unchanged. Non-default home users
must use `hyprial init --no-service` and their own process supervisor, rather than sharing
Hyprial's fixed service label. `hyprial init` retains its existing partial-success behavior:
it reports `service = {ok: false, reason: service-install-failed, detail: ...}` and the daemon
it started stays unsupervised. `--no-service` skips installation; it is required for
containers, CI, and disposable homes. It does not make later service operations safe.

Install and uninstall also refuse a *loaded* label whose own unit file belongs to another home or
install: no local unit file means `supervisor-file-missing` (the loaded job is not unloaded,
replaced, or adopted), and a local unit file plus a foreign loaded job means
`supervisor-label-foreign` (the local file is left untouched). Restart refuses the same collision
before `launchctl kickstart` / `systemctl --user restart`, even without a local unit file,
because those commands address the machine-global label rather than our file. Reconcile
(`doctor --fix`) checks the same collision before stopping a stray process, backing up a file,
or invoking install/restart. All four manager mutation entries use one loaded-label guard. The label is machine-global while the unit file is per home, so writing
ours under it would bootstrap a second unit and then report the other job's pid as our custody.
Status reporting is separate and stays honest: a job loaded from elsewhere is reported as *not this
home's supervisor* (`loaded: false, running: false, pid: null`; `installed` still describes
the local unit file). With no local file, read-only `doctor` reports no supervisor for this home.
This read-only attribution does not grant permission to mutate a colliding label.

The status JSON object contains `ok`, `platform`, `unit`, `unitPath`, `installed`, `loaded`,
`running`, `pid`, `bootPersistent`, `changed`, `attributed`, `templateMatches`, `servingPid`,
`supervision`, `relaunchCount`, `lastExitCode`, `ownershipBusy`, `ownershipReason`, and
`backupPath`. `supervision` is one of `supervised`, `unsupervised`, or `lost`. Custody also
requires the unit file and its containing directory to be owned by the current uid and not be
group- or world-writable. `ownershipReason` identifies a failed filesystem check.

## Restart custody

All daemon restart callers share one decision. If an attributable installed supervisor currently
owns the serving daemon, restart uses `launchctl kickstart -k gui/<uid>/<label>` or
`systemctl --user restart <unit>`. It waits for daemon readiness and requires a fresh manager job
pid: the post-restart job pid differs from the prior job and serving pids, and the daemon serving
the configured socket has exactly that pid. A mismatch fails with `SERVICE_SUPERVISION_LOST`;
error data sets `restart.supervision` to `lost` with a typed `restart.reason`, while upgrade restart
records expose that value as `restart.supervisionReason`. A status probe reports “not loaded” only
for the manager's definitive absence response: `launchctl print` exits 113 and includes the exact
`Could not find service "<label>" in domain for user gui: <uid>` line, or `systemctl --user show`
exits 4 with stderr exactly `Unit <unit> could not be found.`. Every other manager non-zero exit,
timeout, or missing executable uses the lost-supervision result. A verified supervisor restart
reports `supervised`. A restart with no attributable owner uses the existing strict stop/start path
and reports `unsupervised`. A loaded manager label with no unit file is configured but inconsistent;
it fails instead of falling back to the unmanaged restart.

## Daemon stdout/stderr stamps

`hyprial daemon run` stamps its Python stdout/stderr lifecycle and error records,
including ownership refusals before runtime construction:

- `t`: UTC ISO-8601 timestamp with milliseconds and `Z` suffix, evaluated when
  the record is emitted (for example `2026-10-30T17:43:01.123Z`).
- `pid`: the writing process ID, always present.
- `epoch`: the runtime epoch after construction; omitted before it exists.

JSON objects gain these top-level fields, without a text prefix. Existing
`ok`, `code`, `error`, and other non-stamp fields retain their meanings. Reserved
stamp keys are replaced rather than duplicated. Fragmented and indented JSON
objects are buffered until complete and emitted as compact JSON; a flush between
fragments does not turn them into text. An unfinished object is emitted as stamped
diagnostic text when the command exits. Text records use
`t=<timestamp> pid=<pid> [epoch=<epoch>] ` prefixes. Other CLI commands do not gain
these fields. This is not a change to the structured `daemon.jsonl` schema.

Consumers must accept the additive fields. The service ownership-busy detector
searches for the `DAEMON_OWNERSHIP_BUSY` token; it does not require an exact JSON
shape or an unprefixed line. Native-library writes and subprocess writes through
inherited OS descriptors bypass Python streams and are outside this stamp surface.

## Doctor reconciliation

When a service file exists, `doctor` detects:

1. a serving daemon outside supervisor custody;
2. repeated supervisor launches with `DAEMON_OWNERSHIP_BUSY` in the service stderr log;
3. a service file that differs from the current rendered template;
4. an unsafe owner or mode on the service file or its directory;
5. a loaded manager label whose service file is missing.

`doctor --fix` uses the same service manager. It gracefully stops a verified stray socket owner,
lets the supervisor take custody, and verifies the resulting pid. It backs up an attributable stale
unit before re-rendering and re-bootstrap. A foreign file or loaded label is never modified.

## Legacy autoupdate timer removal

`autoupdate-timer-v1` cleanup manages separate autoupdate labels, not the daemon label.
With no local legacy files it performs no supervisor commands. Before removing any existing
legacy file it compares the loaded job's reported path with the exact local path. Systemd
checks both `hyprial-autoupdate.service` and `hyprial-autoupdate.timer` before disable, unlink,
or daemon-reload. Launchd checks the autoupdate plist before bootout or unlink. A foreign or
unknown loaded path, unknown systemd load state, or failed probe refuses cleanup and preserves
all local files. The upgrade step reports its existing failed-step result; no new error code
is introduced.

## Failed starts under a supervisor (0.5.5.1.1)

launchd (`KeepAlive` + `ThrottleInterval` 5 s) and systemd (`Restart=`) relaunch a daemon that
exits during startup without knowing why. A deterministic startup refusal therefore becomes a hot
loop (2026-10-28: 271 failed starts in 1 h 49 min). The daemon now owns the throttle:

- Every `daemon.start.failed` event carries `code` (the daemon/sidecar error code token, or `null`),
  `attempt` (consecutive failed starts, 1-based) and `backoffMs` (`5000 * 2^(attempt-1)`, capped
  at `60000`). The stderr mirror carries `code` only when one exists.
- The counter is persisted at `<state>/daemon-start-failures.json` (atomic, mode 0600) as
  `{count, firstAt, lastAt, lastCode, lastPhase}` and is deleted when `daemon.ready` is logged.
  `hyprial doctor` reports it as the `daemon-start-failures` check (`warn` below 3, `fail` from 3)
  with a remediation action; the row is absent when no failure is recorded.
- When `HYPRIAL_SERVICE_MANAGED=1` (set by the installed launchd/systemd unit) and `attempt > 1`,
  the daemon logs `daemon.start.backoff {attempt, backoffMs}` after teardown and waits `backoffMs`
  before exiting. The wait ends immediately on SIGTERM/SIGINT (`hyprial daemon stop`,
  `launchctl`), so an operator is never blocked by it. A first failure and any unmanaged
  `hyprial daemon run` never wait.
- The supervisor's own throttle still applies on top (launchd: 5 s; systemd: `RestartSec`).
  Relaunch cadence under a persistent refusal is therefore `backoffMs` + supervisor throttle,
  i.e. at most one attempt per ~65 s instead of one per ~5 s.

## Degraded start with a pending invite redemption (0.5.5.3.4)

A previous `org join` can leave its invite redemption pending in the migration receipt; every
later daemon start resumes it against the invite authority. When the authority is unreachable
that resume used to abort the whole start (`daemon.start.failed AUTHORITY_UNREACHABLE`), leaving
the device unusable until local state was cleaned by hand. The resume of a *pending redemption*
receipt now degrades instead:

- The daemon keeps starting and serving. Only the redemption resume is affected; every other
  startup phase, and the resume of complete receipts and directory imports, stays fatal as
  before. An explicit `org join` that fails still reports its error to the operator.
- The failed attempt is recorded at `<home>/tailnet/redemption-degraded.json` (atomic, mode 0600)
  as `{tailnetId, inviteId, code, error, attempts, firstAt, lastAt, retryable, nextAttemptAtMs}`; `error`
  carries the exact sidecar reason (e.g. `AUTHORITY_UNREACHABLE: Tailnet membership command
  failed.`). The record is deleted once the redemption completes.
- A bounded-backoff retry runs on the forwarding reconcile tick: delay is `30000 * 2^(attempts-1)`
  ms capped at `600000`, so at most one attempt per 10 minutes (no retry storm). Once the
  authority is reachable the redemption completes without a restart and the record is cleared.
- A permanent invite refusal (`INVITE_EXPIRED`, `INVITE_INVALID`, `INVITE_CONSUMED`,
  `INVITE_VERSION_UNSUPPORTED`) stops the replay: the record carries `retryable: false` and
  `nextAttemptAtMs: null`, the receipt keeps `lastError`, and neither later starts nor the retry
  send that invite again. Only an explicit `org join` sends an invite; a different invite
  replaces the stale request and clears that tailnet's record, as does any completed redemption
  (including one completed by the startup resume). A non-retryable record makes the reconcile
  tick return without reading any receipt. A retryable record whose receipt has since become stale
  (an explicit join of the same invite refused permanently) is rewritten as stale by the next tick
  or start; a record written before `retryable` existed derives it from its `code` when read. A
  stale receipt found without a record (e.g. after an upgrade) is recorded with stage-tagged text
  (`... (stage=invite-redemption) ...`) rebuilt from its `lastError.code`; for `INVITE_EXPIRED`
  it is identical to what the first failed resume writes.
- The record is one file shared by all tailnets. A stale (`retryable: false`) record never
  replaces another tailnet's record; a retryable record replaces another tailnet's stale one;
  `attempts`/`firstAt` are only carried over from the same tailnet's record. Stale receipts are
  held in `tailnetId` order after every resume, so repeated starts leave the same record
  regardless of receipt enumeration order, and a stale tailnet becomes visible again once the
  retryable one completes. Known limit: with two transiently failing tailnets only one holds the
  record and is retried by the tick; the other is retried at the next start.
- Events: `membership.redemption.degraded` (startup resume failed),
  `membership.redemption.retry-failed` (a retry failed), `membership.redemption.completed`
  (a retry succeeded) — all in the `zenoh` domain with `tailnetId` and `code`.
- `ps` (and therefore `hyprial daemon status`) carries `daemon.degradedRedemption`: the record,
  or `null` when the daemon is not degraded.
- `hyprial doctor` reports the `membership-redemption` check (`warn`, never `fail`: the daemon
  serves) with the exact reason, attempt count, and a `hyprial org migration-status` action;
  the row is absent when no degradation is recorded.
