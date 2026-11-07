# Workflow graph observation

Workflow execution is a PAC graph. Use format 2 YAML and inspect the expanded
plan before dispatch. Format 1 execution inputs are refused; their stored runs
are available only through workflow history.

- `hyprial workflow plan FILE --json` validates nodes, forward/back edges,
  worker sharing, explicit borrowed owners, launch selection and failure policy.
- `hyprial workflow run FILE --operation-key KEY --yes --json` publishes a graph.
  Repeat the same key only for the same request; it returns the same graph.
- `hyprial workflow inspect GRAPH --json` reads node flags, current request IDs,
  fixed deadlines and evidence references in one SQLite snapshot. A terminal
  managed graph also reports `workerCleanup`: `pending` until every
  receipt-owned worker is confirmed absent with a durable `down/done`
  activation, `complete` after that confirmation, or `attention` when identity
  or lifecycle observations make reclaim unsafe to claim.
- `hyprial workflow inspect GRAPH --node NODE --json` reads one node; add
  `--context` for that node's immutable workflow context.
- `hyprial workflow complete GRAPH NODE --request-id REQUEST --reason-ref REF`
  sets the owner's flag only for the current request and current input facts.
  Expansion planners additionally supply `--expansion FILE`; success returns
  `childGraphId` and `expansionDigest`. An invalid document leaves the same
  request current. An accepted exact replay returns the stored child even
  after parent close or policy tightening.
- `hyprial workflow fail GRAPH NODE --request-id REQUEST --reason-ref REF`
  records explicit failure. Replies and native completed turns are not flags.
- `hyprial workflow cancel GRAPH` closes the graph and reclaims its owned workers.
  Explicitly borrowed actors are not adopted or stopped.
- `hyprial workflow history list/status` reads the sealed legacy archive.

Each requested node includes `deliveryState`: `planned` while its notification
is retryable, `accepted` after a real message ID is recorded, `withdrawn` when
the request is no longer current, or `failed` after a definitively permanent
delivery rejection. A failed delivery also includes `deliveryFailureCode`; it
is terminal and is not retried by later workflow cadences or notification
resend commands.

The CLI uses the local user identity, or a managed worker's daemon-issued
actor/session binding. `--from` is a checked claim, never an impersonation option.
GUI graph writes use the linked session binding; human completion controls use
the local human identity. A node ID is not an actor alias.

An upstream reset invalidates outstanding downstream requests, including through
intermediate approvals. Old completion flags remain historical facts; their
owners must reset and review changed inputs. A stale request cannot complete a
new round. Explicit back edges notify the target owner to perform that rework.
A graph with back edges requires an explicit end node.

A relative timeout starts when the node is first requested, so dependency
waiting does not consume that budget. An explicit deadline_ms remains an
absolute deadline. Neither native progress, replies, replay nor a re-request
extends it. Budget a human node's timeout for its prescreen, up to the declared
number of asks, and one upstream rework. Failure policy is terminate (default),
continue (independent branches only), or hold (owner action). There is no
implicit report: declare a report node with its task and recipient.

Graph state is distinct from process liveness. A completed flag remains valid
when a finished worker is reclaimed. Use PAC context/events and hyprial daemon status for
runtime evidence; an accepted inbox delivery does not prove business completion.
`hold` is not terminal and creates no cleanup intent. Explicitly borrowed actors
have no ownership receipt, so even a borrowed actor whose name starts with
`wf-` is excluded from terminal cleanup.

Expansion placeholders project `kind: expansion`, `systemOwned: true`, child
identity/state/digest/deadline, bounded child-node state, and optional
worktree cleanup debt. Child graph status/list includes `parentGraphId` and
`parentNodeId`; PAC journal event/snapshot kind remains the physical `task`.

For imported cross-machine requests, the execution daemon's bound owner may use
`workflow inspect GRAPH --node NODE`. This returns the origin's node snapshot
and the local durable return state. If the origin cannot be reached, `current`
is null, `authorityAvailable` is false, and the local return state is still
readable. This never implies that the task is current or complete. Graph-wide
status remains on the origin. No request capability is part of this projection.
