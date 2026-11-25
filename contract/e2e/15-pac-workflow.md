# E2E-015: PAC workflow dispatch

## Preconditions

An installed checkout CLI, Python 3.12, and local process creation are required.
No provider credentials or external network are used.

## Steps

Run `uv run --python 3.12 python contract/e2e/run-pac-workflow.py`.
The runner uses an isolated home/state/socket and the checkout's installed CLI.
It starts real daemon and worker processes using a deterministic Codex protocol
fixture. This is CLI/daemon/protocol evidence, not real model/provider evidence.

## Assertions

The gate covers graph plan/run/inspect, dependency-triggered workers, authenticated
explicit completion, graph-close reclamation, operation-key replay, daemon
restart, the legacy write barrier, and SQLite integrity. Routine removal first
proves that removing the sole coordinator binding is refused with
`ROUTINE_LAST_BINDING`, then binds a replacement routine and proves that removing
the owning routine reclaims its coordinator process. Negative and policy
cells additionally live in tests/daemon/pac/workflows/runtime/graph_service/ and
 tests/daemon/pac/workflows/cutover/test_pac_workflow_cutover.py. The runner retains its isolated evidence
path and stops its owned daemon on every exit path.

Fixture authors read each created agent's official `a.<id>` URI from the agent
registry rather than constructing a URI from its display name. Passive channel
fixtures use the current channel protocol and lease heartbeats; a rejected or
stale binding remains a failed assertion.
The routine status retains its declarative `produces` spelling. A replacement
that borrows its coordinator uses the issued address from the public registry.

Managed graph owners retain their logical roster spelling. The native-failure
assertion instead selects the official actor from that worker's environment
receipt, ties its worker log to the failed graph, and still requires exactly
one native protocol attempt. Missing, foreign or malformed receipts fail.

The expansion-v1 acceptance extension is isolated and credential-free. It
uses a daemon-trusted `pac-expansion.json`, a parent planner plus system-owned
placeholder, and a disposable local Git repository. It must prove invalid
completion leaves the planner requested, valid completion atomically binds one
child, managed execute nodes receive distinct pinned worktrees, exact replay
returns the same child, child terminal state projects to the placeholder, and
clean cleanup removes only the worktree while preserving its branch and
commits. Fake harness and local Git evidence are not real-provider or OS
sandbox evidence.

Old reply-prefix/ACK matching, retry/backoff and automatic summary behavior are
retired. Their test scenarios are not run against the new execution semantics.

## Cross-node extension

E2E-015 runs both local and cross-node scenarios. To run only the cross-node
part, use `uv run python contract/e2e/run-pac-cross-node.py` for two real
isolated daemons connected only by an ephemeral localhost Zenoh endpoint. Their
owners differ. A deterministic native worker proves cross-node CLI completion,
native failure return, borrowed-process survival, session forgery rejection,
cancellation fencing, durable offline completion across both daemon restarts,
and a real scheduled routine occurrence using a borrowed remote actor. Exact
owned PIDs and daemon locks must clear before the runner prints PASS. This is
cross-node protocol evidence, not evidence from separate physical hosts or a
production tailnet/provider account.

The cross-node worker's isolated Codex config explicitly declares the same
`fixture` model as managed launches. This local protocol fixture needs no
provider credentials. The normal resume compatibility guard remains enabled
across the intentional daemon restart.

See `tests/daemon/pac/workflows/delivery/test_pac_remote_workflow.py` for modified-grant rejection, duplicate
and conflicting outcomes, lost acknowledgements, transitive rework withdrawal,
fixed-deadline guards, no capability in prompts, and reset replay isolation.
Remote expansion cells additionally cover feature refusal, full-frame bounds,
invalid-to-correct attempt CAS, accepted conflict, and origin-scoped preview.
