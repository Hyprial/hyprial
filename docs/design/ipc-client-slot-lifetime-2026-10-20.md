# IPC client slot lifetime and unbounded-wait audit

## Incident boundary

Production reached 64 live `hyprial-daemon-ipc-client` threads, equal to the
fixed IPC client limit. All new local CLI connections were closed until daemon
restart. The existing 15-second idle timeout only covers `recv()` before a full
request. Once `_response()` entered a handler, the same transport thread could
wait for domain settlement forever.

The fix separates transport ownership from domain settlement. Each full frame
runs its handler on a daemon request thread. The transport worker polls for
handler completion and peer hang-up and has a 90-second fallback deadline.
Peer close or deadline releases the transport slot without claiming that an
already-admitted mutation was cancelled.

## Wait audit

The synchronous IPC dispatch surface was checked for `Event.wait`,
`Condition.wait`, `Future.result`, and `Queue.get` calls reachable from
`handle()`.

| Path | Bound before this change | Can retain an IPC worker | Settlement rule |
|---|---:|---:|---|
| `message.pending.wait` | Yes: caller hold capped by `MESSAGE_WAIT_HOLD_SECONDS`, sliced for shutdown | During the bounded hold | Read-only long poll |
| `org.fetch` mesh/orgfs request | Yes: validated caller timeout, maximum 30 s | During the bounded fetch | Read-only fetch |
| service-connect persistence and I/O ports | Yes: `SERVICE_CONTROL_TIMEOUT_SECONDS` or explicit timeout | During the bounded call | Typed timeout |
| lifecycle result observation | Yes: explicit operation timeout and periodic condition wait | During the bounded call | Durable operation remains authoritative |
| lifecycle durable admission (`LifecycleCoordinator.submit`) | No: `Queue.get()` waits for the actor's reservation reply | Yes | Accepted operation must not be reported as cancelled |
| state persistence `call_settled` | No after its initial bounded observation window | Yes | Exact accepted command continues to settlement |
| user-delivery ledger `_call` | No | Yes | Native delivery result continues to settlement |
| orgfs replica/space/document authorities | No | Yes | Accepted journal/CRDT operation continues to settlement |
| orgfs directory lifecycle and shared mesh creation | No | Yes | One creation/operation remains authoritative |
| harness `_AwaitSettlement` requests | No | Yes when reached by lifecycle/transfer work | Existing correlation remains authoritative |
| identity migration waits | Yes where callable from IPC; waits use explicit timeouts or one-second shutdown polling | Only within their bound | Typed operation result |
| forwarding and network controller calls | Yes where callable from IPC; controller requests use explicit deadlines | Only within their bound | Sidecar request correlation remains authoritative |
| PAC workflow/routine calls | Actor admission is bounded; synchronous storage calls can enter the state/orgfs settlement paths above | Indirectly | Existing graph operation key remains authoritative |

Background queue consumers with bare `Queue.get()` are not request waits: they
own daemon services and are stopped with sentinels. Process `wait()` calls in
harness supervisors are likewise outside a live IPC transport or are wrapped by
an explicit lifecycle deadline.

## Why the transport does not cancel domain work

Several unbounded waits intentionally prevent a caller from turning a committed
write into a false negative. Killing those waits at the socket boundary would
allow retries to duplicate non-idempotent effects. The transport therefore
returns the existing `IPC_TIMEOUT` code at its own deadline while the exact
handler continues. Its lifetime remains visible as `ps.daemon.ipcRequests.active`;
live socket pressure is separately visible as `ps.daemon.ipcClients`.

This change keeps `_IPC_MAX_CLIENTS` at 64. The incident showed leaked lifetime,
not legitimate simultaneous socket demand. Raising the limit would only delay
saturation and increase the number of transport threads hidden from operators.
