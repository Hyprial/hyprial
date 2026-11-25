# E2E-002: pending message survives daemon restart

Status: executable via `run-daemon-restart-survival.py`.

This scenario is safe for local automation only when every process and path is
test-owned. Do not stop the production daemon. The test harness must address
the isolated daemon by its explicit socket/state path and terminate only the
PID it started.

## Preconditions

- The runner can resolve an installed `hyprial` executable.
- The daemon uses one dedicated temporary `HYPRIAL_HOME` and `HARNESS_STATE_DIR`.
- The harness owns the daemon subprocess PID and can restart it against the
  same temporary state.
- No production credential, socket, or daemon is needed.

## Steps

1. Start the isolated daemon with `hyprial init`.
2. Send two requests with real `hyprial message send` commands and capture their IDs.
3. Send `SIGTERM` to the captured test-owned PID and wait for graceful socket
   cleanup, preserving its test-owned state.
4. Restart it with `hyprial init` and inspect recovered messages through `hyprial daemon status`.
5. Acknowledge both messages in FIFO order with `hyprial message ack`.
6. Restart once more and inspect pending messages again.
7. Send to a nonexistent actor with a one-second test-only hold TTL, wait for
   `hyprial message status` to report the durable `expired` verdict, then read the
   sender's daemon inbox twice through the public IPC seam. Acknowledge the
   notice, then read once more to prove it is gone.

## Assertions

- The recovered first delivery has the same stable Harness `messageId` and a
  fresh ephemeral `deliveryId`.
- The pending sequence preserves per-target FIFO order.
- After both acknowledgements, the second restart produces no replay.
- The nonexistent-actor send first reaches the pullable
  `expired`/`TTL_EXPIRED` terminal state, then the sender receives exactly
  one correlated `intent=system` notice containing the original `messageId`
  and reason.
- The second read contains the same notice: `message.pending.list` is a
  non-consuming doorbell. A notice ACK dismisses the recipient-scoped row,
  and the next read contains no copy. This local dismissal creates no
  receipt-tracking or FIFO-settlement duty. See
  [inbox-batch-v1](../wire/inbox-batch-v1.md), “Notice consumption change”.
- The notice is a latency optimization only. The persisted `message status`
  verdict remains the correctness floor and is asserted before the notice.
- A stale pre-restart `deliveryId` fails as inactive; it never marks the stable
  message processed.
- No production socket, desired-state file, provider process, or credential is
  read or changed.

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

同一节点上 agent→agent 发送在硬切后需要可写组织空间；该跑者本地发送如未自建 `group-` 空间，首发会以 `LINK_SELF_FULL`/`org-unavailable` 永久 hold。本条为前置说明；WP8 返工 2-E 未改造该跑者（记 UNKNOWN，不报通过）。
