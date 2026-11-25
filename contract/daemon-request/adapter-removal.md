# Local platform integration removal

`management.adapter.remove.preview` observes an existing Lark integration and
returns an expiring `operationId`, affected route names, binding and whether the
integration-owned credential will be deleted. It makes no deletion or platform
stop request. Closing the confirmation is cancellation with no effects.

`management.adapter.remove` accepts only that `operationId`. The daemon binds it
to the authenticated operator/session/epoch and the exact configuration,
credential digest, Agent incarnation, registration, pin and runtime version.
Direct `{name}` execution is no longer accepted. Parameter schemas are checked
before operator admission; no state is read or changed until both succeed. Both methods are published via
MethodSpec and HTTP for the local operator. Browser-supplied caller claims are
never authority. No credential bytes, filesystem paths or digest leave the API.

An active or starting integration must be explicitly stopped and previewed
again. Confirmation reserves its registry, pin and lifecycle against concurrent
start/rebind. A changed or expired preview cannot delete anything. Deletion
removes the local gateway (including routes), registration and pin; it preserves
the bound Agent and the remote provider app. Only `lark-<name>` credentials not
referenced by another gateway are removed. External/shared credentials remain.

A bound integration is not refused: the preview's `boundAgent` names the Agent
that receives on it, else the owner's user agent when only the user agent cell
names it (I7a, 2026-11-24). Removing such an integration goes through; the
owner's copies that used it then end `TARGET_UNRESOLVED` (`adapter-unconfigured`)
until it is added back. A user agent cell that cannot be read names nobody.

The existing shared lifecycle database stores a removal intent before effects,
including configuration and registry preimages, but only a credential digest.
A durable commit separates reversible configuration/registration changes from
credential cleanup. Before commit, errors compensate; after commit, recovery
only completes cleanup. Startup settles pending intents before serving requests
or restoring platform workers. Recovery never repeats stop, never forwards a
prepared deletion, and never overwrites a changed preimage or replacement
credential. Such an inconsistent recovery remains a startup error with its
receipt retained for investigation. Pending operations block another removal;
the offline CLI also refuses to bypass a pending receipt.

The same operation ID replays its retained result within its original preview
session/epoch lifetime. A transport timeout/disconnect is not proof of failure;
clients must not resubmit or fall back to offline deletion. The shared GUI reads
`adapter.list` after success and displays success only once the target is absent.
An uncertain result disables that confirmation; close, refresh and inspect before
requesting a new preview. Restart invalidates old preview tokens, not journal
recovery responsibility.

`hyprial adapter remove` keeps its existing flags, uses preview followed by
confirmation while connected, and returns the safe removal result plus
`daemonReload`. Its connected JSON result has `name`, `provider`, `removed: true`,
`unboundAgent` and `credentialRemoved`, rather than the offline file-level
`removed` object. It never returns `channelsPath`, `secretPath` or `secretKept`.
The daemon operation already removes its runtime entry; the subsequent reload
is an observation of remaining changes and does not repeat that removal.
`--force` retains its explicit stop-before-preview behavior.
When preview cannot reach any daemon it may use the existing ownership-fenced
offline removal. Failure after execute submission never takes that fallback.

A rejected stop-effect submission is settled as a failed stop, never as a
successful stop. The operation attempts to abort its exact runtime reservation
even if begin did not return; a genuinely unsettled effect retains custody.
After commit, credential cleanup and runtime settlement must both complete
before the journal becomes COMPLETED. A failed runtime settlement retains the
COMMITTED intent, blocks a new deletion, and is recovered before serving on
restart. No deletion request is automatically replayed.
