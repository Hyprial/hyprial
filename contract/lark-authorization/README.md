# Configured Lark App authorization service

The public Python entry is
`hyprial.daemon.create_lark_authorization_service(home, state_dir)`.
The trusted host owns this service and calls `close()` on shutdown. This is
a reusable domain facade, not a new HTTP or CLI protocol. The GUI consumes it
through the unified backend owner; it never shells out to a CLI.

Methods:

- `status(adapter)` independently inventories the configured App's tenant
  grants, with an eight-second default process deadline plus bounded cleanup.
- `start(adapter)` starts one authorization attempt or returns the existing
  active/unknown attempt for that adapter and its currently configured App.
  A changed App first cancels and cleans up prior-App work. If that cleanup
  remains uncertain, the old attempt is returned with `unknown` and
  `WORKER_CLEANUP_UNCONFIRMED`; no new worker starts. A later `start` retries
  that cleanup before admitting work for the new App. Confirmed cleanup
  settles an active or unknown prior-App attempt as `cancelled`, so restoring
  that App does not reuse the retired attempt. It never creates an App.
- `operation(operation_id)` reads a safe snapshot; an unknown ID raises
  `KeyError`. `current_operation(adapter)` returns the latest host-local
  attempt, including terminal results, or `None`.
- `cancel(operation_id)` stops owned work and reports cancellation separately
  from grants. An active or unknown attempt becomes `cancelled` only after
  cleanup is confirmed; failed cleanup stays `unknown`. This preserves the
  grant snapshot and never starts another attempt. `close()` stops all owned
  work and closes the effect executor.

`status` returns `adapter`, `appId` (string or null), `authorization`,
`capabilities` (granted capability IDs as strings), and `checkedAt` (numeric
Unix seconds). A successful inventory additionally includes
`missingCapabilities` for required capabilities without grants. A diagnostic
`reason` is a sanitized code, never a raw SDK exception.

Authorization states are `authorized`, `unauthorized`, `unconfigured`, and
`unconfirmed`. Only valid scope inventory with every required capability
granted establishes `authorized`. Valid missing grants establish
`unauthorized`; malformed inventory, read failure, or unreadable configuration
remain `unconfirmed`. A successfully loaded configuration without the named
adapter is `unconfigured`.
Missing or null `scope_type` means tenant; contradictory duplicate grants for
the same normalized scope/type are malformed and remain `unconfirmed`.

Operation snapshots contain `operationId`, `adapter`, `appId`, `status`, and
an `authorization` snapshot. Status is `starting`, `awaiting_user`, `checking`,
`succeeded`, `denied`, `expired`, `cancelled`, `failed`, or `unknown`. Active
verification may include `verificationUrl` and `expiresAt` (Unix seconds).
Terminal snapshots remove both. Optional `errorCode` is sanitized.

SDK success is followed by independent scope inventory; the operation only
succeeds if that read confirms required tenant grants for the same App.
Denied, expired, and cancelled describe the attempt, not a revocation of
previously observed grants. No code infers revoked grants from transport
failures. Unknown attempts are not implicitly restarted: a fresh successful
`status` query can reconcile them from authoritative grants only after their
worker cleanup is proven. While custody remains uncertain, the query updates
the separate grant snapshot but keeps the attempt `unknown`.

Device authorization requests only required tenant scopes, targets the
configured App, and never creates/renames an App or rewrites its secret.
User OAuth (`lark-cli user-auth`) is a separate credential and is unused.
Only official HTTPS verification hosts are exposed; secrets, returned
`client_secret`, `device_code`, and ambient credentials never enter responses.

Every SDK/inventory effect has its own managed process group. Registration
startup defaults to ten seconds, total authorization to 900 seconds, and
post-approval inventory to eight seconds. Polling never waits on SDK I/O.
The `startup_timeout` option bounds the SDK's initial `init`/`begin` exchange
until its browser verification response; it does not wait for a worker-ready
message or restart a failed child. The public kernel `ActorRuntime` has no
request-deadline contract: `ActorSpec` describes actor admission and
`SupervisionPolicy` describes restarts. `EffectLane` owns effect capacity and
completion, while `GenerationScheduler` schedules callbacks; neither defines
the SDK verification-response/expiry protocol. This exact request-bound site
is recorded with source and test evidence in `tests/supervision_exemptions.json`.
The manifest's shrinking-only allowance for legacy supervision is unchanged:
this entry records a scanner false positive for a request-response budget,
under the existing classification of non-supervision matches. The six managed
capabilities remain those in the original [supervision standard](../../docs/notes/supervision-consolidation-standard-2026-08-29.md).
The timeout test also proves that an unknown operation reuses its ID without
launching a second authorization effect, and that its child is closed before
the service itself closes.
Cancellation and close terminate/reap the child through kernel-owned process
custody. The worker calls the public kernel `bind_current_process_to_parent`
for parent PID/birth-identity loss. Its bounded cleanup callback removes
private scratch; the kernel exits the child even if that callback raises or
blocks. The service remains the sole authority for authorization time budgets;
the worker job carries no duplicate `maximumSeconds` deadline. The real
host-crash test proves termination on Linux and proves that disabling the
binding leaves the orphan alive until test cleanup. Native Windows/macOS
orphan behavior remains unverified here.
At most four effects are admitted; excess starts fail explicitly. Failed
cleanup retains the exact handle and its capacity reservation, for both
authorization and inventory workers. `cancel` retries an operation's retained
handle. `close` retries every retained handle, including after a previous
`close` failed, and raises a sanitized error while any cleanup is unconfirmed.

Operation IDs are memory-only and bounded to 128 retained attempts. A browser
refresh against the same host recovers through `current_operation`. A host
restart loses that journal: the browser must retain its own submission/unknown
marker, reread grants, and never turn a missing old ID into an automatic new
authorization. This journal is transient authorization workflow state, not
a second durable message/session authority.

For isolated tests, `registrar(job)` may inject an owned effect with
nonblocking `snapshot()` and bounded, idempotent `close()`; `effects` may
inject an executor (owned by its caller), and `clock` supplies expiry Unix
seconds. Deadline overrides are for trusted composition/testing only.

Verification: `uv run pytest tests/daemon/adapters/lark/identity/authorization`.
The real SDK cases use only a loopback accounts/OpenAPI stand-in and disposable
configuration. Real tenant approval is not claimed by this isolated suite.
