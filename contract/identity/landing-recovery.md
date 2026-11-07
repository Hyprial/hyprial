# Identity landing recovery ports (P2 B3)

Internal public identity/daemon-composition ports; no new IPC or CLI.
`AgentActor.verify_landing_payload(actor, *, expected_entity_token, expected_files)`
and the corresponding AgentDirectoryFacade method return `present`, `absent`,
or `partial`. The identity owner captures the ready durable HomeReceipt under
an exact entity-token fence, the existing home effect lane reads the filesystem,
and the owner rechecks current home authority before publishing the result.
The completion must match its correlation/generation, receipt and manifest.
No second durable identity store or new worker/timer is introduced.

Every verification is a fresh read, with a new correlation ID: a retained past
success cannot authorize recovery. Existing bounded home-call custody and wait
semantics apply to a slow filesystem effect. The result is an observation, not
an exclusive lease on bytes after return.

- `present`: all declared payload files match path, size, mode and SHA256 under
  a valid target receipt. Extra target files are permitted, matching the existing
  materialisation contract; this is not a whole-directory equality guarantee.
- `absent`: the validated target home contains no regular payload files besides
  its custody receipt. Missing or invalid homes/receipts are **not** absence.
- `partial`: missing/mismatched payload, unsafe/link paths, malformed mirror,
  extra-only residue or a home replaced during inspection. No mutation occurs.
- Missing/revoked/malformed durable authority or a changed incarnation rejects
  the call, rather than minting an absence result. P2 must reconcile authority.
- The source `state/home-receipt.json` entry is excluded from payload equality;
  the target's own receipt must validate. Empty/invalid manifests are rejected.

`AgentDirectoryFacade.settle_landing_destroy(actor, *, expected_entity_token,
operation_id)` returns `destroyed`, `already-cleaned`, or `stale-incarnation`.
It reuses SettleAgentDestroyCommand and the existing blocking settlement edge:
only the first two dispositions can mean gone; a stale incarnation is untouched.
Missing durable cleanup evidence is an error, not already-cleaned. Admission
rejection, lost event owner or failed cleanup propagates an error. While cleanup
is pending, there is no result. No caller deadline or automatic retry is added.
The P2 effect lane (not its mailbox) calls this port; P2 owns deadlines/intents.
An operation ID must be nonblank. It is correlation, not a perpetual idempotency
ledger: repeated calls reconcile the existing durable incarnation/cleanup facts.

These ports do not enable the P2 migration entry point, grant secrets, or prove
cross-machine/real-model acceptance. TC012 remains BLOCKED.
