# E2E-024: peer re-learn after a slot-pool-growth rebuild

## Preconditions

- Run with `uv run --no-sync python contract/e2e/run-peer-restart-relearn.py`
  and `HYPRIAL_BIN` pointing to this checkout's executable.
- Both daemon homes and state directories are disposable.
- Up to two independent case processes run at once. Their worker HOME/XDG,
  daemon homes, owner and node IDs, OrgFS spaces, roster and key files, and
  private Unix endpoints are separate. Fixed golden synthetic public key
  values do not represent a shared membership store or a real key exchange.
- Both nodes use E2E-021's narrow forwarding exception:
  `HYPRIAL_NETWORK_ISOLATED=0`, explicit private Unix endpoints, with peer discovery,
  gossip, usage fetches, Tailnet, and Headscale disabled.
- Both nodes use E2E-021's protocol-v7 leased-roster fixture. A starts with
  `HYPRIAL_ZENOH_SLOT_POOL=1`. The fixture reports exact `{peer, slot}`
  mappings and proxies only owned private Unix streams. It does not implement
  Tailcat, DERP, cryptography or a remote control authority.
- Every start and restart validates effective endpoints with
  `local_topology_violations`: only each node's owned private Unix paths are
  allowed. B's loss sockets live inside B's root. There is no global
  `ALLOW_UNISOLATED` bypass.
- The in-process private Unix stream forwarder is the only A/B data-path intermediary. It can
  swallow A's final bytes, preserve B's client connection, and blackhole a
  reverse leg.
- Host, target and agent observations, message sends and acknowledgements use
  the same public daemon IPC methods as the CLI through the kernel client,
  retaining a four-second request bound and no automatic write retry.
  Initialization, real agent creation, OrgFS setup and authority pins remain
  public CLI operations.
- Each direction freezes its conversation and idempotency key before one send.
  Only `SUBMIT_OUTCOME_UNKNOWN` permits public read-only `message.send.lookup`:
  the lookup must confirm the same command, conversation and original receipt's
  message ID. Send and lookup share the original four-second submission budget.
  Unknown at its deadline, identity mismatch, missing receipt or other errors
  remain failures. No replacement command is submitted.

Directory-address changes now rebind an existing slot and must not rebuild the
Zenoh session. This scenario is the positive control for the one remaining
legitimate rebuild: fixed-slot pool exhaustion and growth.

## Steps

1. Start A with one fixed forwarding slot and both nodes' v7 sidecars.
   Publish an explicit leased roster: B's fixture client key orders B above A
   and makes B dial A through the loss intermediary, while A initially maps
   no business peer. Join one org directory and require mutual host visibility.
2. Publish an owner-authored fixture-device row and an explicit leased roster
   entry whose ordering makes A the dialer. Require it to bind slot 0 without any
   `zenoh.forwarding.redialed` event.
3. Explicitly pin A as the existing roster authority using the public
   `org pin-authority` command. Kill and restart only B, require its readiness
   to start unleased, publish a fresh authority exchange, require a new B zid,
   and wait for both host views to recover.
4. Publish a second owner-authored fixture-device row plus leased roster entry. It exhausts the pool,
   grows slot 1, and must produce a forwarding redial that retains slot 0 and
   dials exactly two slots.
5. Run four independent loss modes: swallow-close with immediate resplice,
   blackhole without resplice, and swallow-close with 2 s and 10 s delays.
   Observe peer re-learning for at most three times the product's 30 s default
   lease plus 8 s retry cap, then send and acknowledge one message in each
   direction. The stale verdict uses one lease plus the retry cap.
   Durable lookup confirmation still requires the original three-second
   receiver-pending check and the existing acknowledgement request; a sender
   copy alone cannot establish delivery.
6. All success, assertion, timeout, SIGINT and SIGTERM paths stop both owned
   daemons, both sidecars, loss listeners, stream connections and worker
   threads. Report cleanup failures alongside the original failure, never as
   a passing result. The overall deadline remains 300 seconds and its exception
   cannot be consumed by transient probe retries. The parent drains every
   launched case worker and confirms all daemon/sidecar PIDs collected only
   from its owned case metadata. Pending cases cannot start during this drain.

## Assertions

- The protocol-v7 fixture retains the E2E-021 golden wire/capabilities,
  rejects retired v5/v6 and directory `allow` requests, and records daemon-selected
  fixed slots. Only explicitly leased active roster members can be mapped.
- The first peer fits the pool and does not redial. The second peer grows the
  pool and produces at least one `zenoh.forwarding.redialed` event.
- A changes zid in-process without changing its daemon PID; B observes the old
  zid deletion and new zid discovery within the lease-plus-backoff bound.
- All four loss modes deliver and acknowledge messages in both directions
  after the pool-growth rebuild, using exactly one frozen command per direction.
- A compact `peer-restart-summary` records verdicts, command/receipt identities,
  recovery phase and unchanged budgets. Complete JSON is saved in the configured
  E2E artifacts directory before the temporary case roots are removed. The
  original large event remains available locally; CI log APIs may clip its line.
- The test uses no external network, production home, Tailnet, Headscale, or
  unowned process, and cleanup is explicit.
