# Cell attachment observation (0.5.5.3, slice B)

This additive internal Python surface is exported as `hyprial.daemon.cell_observation`:
`CellObservationPort` and immutable `CellSnapshot`. It does not add CLI/IPC
commands or change existing status fields. Run-state publication is a later slice.

## Identity and source association

- `AgentSessionDomains.cells` hosts the logical attachment view. The existing
  directory facade's `cell(name-or-local-uri)` resolves to that view. The view
  itself accepts only a canonical local agent URI; unknown/foreign names return
  `None`, never a stopped-process claim.
- A cell snapshot contains `actor`, `diagnostic_incarnation`, `binding_revision`,
  `target`, `custody`, and `lifecycle`. The target is
  `(local host, harness.lifecycle, canonical actor / diagnostic incarnation)`.
- The diagnostic incarnation is a domain-separated SHA-256 digest of the
  canonical actor and the identity owner's high-entropy entity token. It is not
  an authority credential and is never accepted as one. It stays stable across
  process restarts, mount eviction, and view reconstruction. Agent destruction
  and recreation produce a different ID even with the same name and URI.
- Entity tokens, native session refs, leases, PID/birth identity, prompts, and
  grants do not occur in cell values. Identity and session owners retain their
  existing authority; the diagnostic digest does not replace their fences.
- Managed source association captures entity token and binding revision at the
  existing harness start-admission boundary. A later binding cannot adopt an
  old running source just because its short harness/name key matches. Failure
  to obtain the diagnostic association never fails or changes a start.
- Interactive attachment requires the existing current Agent binding and
  Session projection to agree on the session reference. Its custody is
  `external`; contact is not proof of OS execution or exit. No PID probe,
  transfer, stop, session registration, or heartbeat is triggered by mounting.
- Registration without a source remains `unknown`, unavailable, custody `none`.
  A known binding with no matching source remains unavailable with its binding
  custody label. Source loss is not process death. A verified managed exit can
  be `stopped` while the cell survives.

## Observation and cleanup

`snapshot(actor)`, `observe(actor, after_revision, epoch, limit, wait_ms,
observer_id)`, `open(actor)`, and `cancel(observer_id)` delegate to the read-only
lifecycle monitor contract. Namespace is always lifecycle; zero-wait only.
Missing, closed, or repeatedly changing identity/binding cuts return `None`.
`None` means no stable cell snapshot was obtained, not authoritative absence or
non-execution. Snapshot/observe users must handle this unavailable result.

The view revalidates entity and binding around mounting. Source binding and
publication share the source owner's lock, so an exit during installation is
current or retained. No callback, polling thread, mailbox, ActorSpec, durable
cell store, or supervision policy is created.

Mount capacity defaults to 4096. Eviction and explicit view shutdown unbind
only diagnostic resources and close their observers. Rebinding/eviction/view
reconstruction require cursor resync, while the diagnostic cell identity stays
stable for the same agent. A binding-revision change invalidates the diagnostic
association even if its owner, source key and session reference are unchanged.
The next snapshot/observe/open closes old subscribers and gives stateless cursors
a new epoch requiring resync; no intermediate observation of the released binding
is required. Repeated reads of the unchanged binding retain the epoch.
Mid-mount failure unbinds the attempted target before
propagating the error; retry cannot leave an orphan observer.

Detaching the observation never stops a managed child or external terminal.
It does not mutate routes, grants, homes, sender identity, durable inbox rows,
fetch/ACK/settlement state, PAC flags, or lifecycle recovery policy.

## Acceptance vectors

`tests/daemon/cells/observability/mount/` covers production composition with
fake runtime start/stop/resume, unknown registration, same-name recreation,
stale-binding refusal, verified child stop, rollback/reconstruction, observer
cancellation/eviction, stable diagnostic IDs, and real external sleeping-process
survival. Business-custody sentinels cover pending inbox IDs/sender, grants,
routes and home entries. AST checks reject execution/business commands in the
new cell modules. These are isolated unit/composition tests, not native carrier
or ractor acceptance.
