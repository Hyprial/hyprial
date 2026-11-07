# IPC client slot lifetime

The daemon accepts at most 64 simultaneous local IPC transports. The limit is a
transport bound, not a bound on domain settlement.

After a complete request frame is received, the transport worker waits for the
request result independently of the domain handler. It releases its client slot
when any of these events occurs:

- the response is written or the write fails;
- the peer closes the socket while the handler is still settling;
- the 90-second daemon request deadline expires; or
- daemon shutdown closes the transport.

A deadline expiry returns the existing registered `IPC_TIMEOUT` wire code. A
peer hang-up sends no response. Neither event cancels an already-admitted domain
mutation: its request handler continues to the domain's existing settlement
boundary, and `ps.daemon.ipcRequests.active` continues to include it.

`ps --json` exposes the additive block:

```json
{"daemon":{"ipcClients":{"active":0,"highWater":0,"limit":64,"saturated":false},"ipcRequests":{"active":0,"highWater":0}}}
```

`active` is the current number of live transport slots. `highWater` is the
maximum concurrent count observed since daemon start and never decreases.
`ipcRequests.active` and `ipcRequests.highWater` apply the same definitions to
admitted in-flight request handlers. `saturated` is exactly `active >= limit`.

The `daemon.ipc.client_capacity_exhausted` local log event includes
`clientCount`, `clientLimit`, `saturated`, and `activeClients`. Each
`activeClients` row contains `peerPid`, `connectedSinceMs`, `lastRequestMs`, and
`currentMethod`. The event also contains those four fields for the rejected
peer; its request fields are null because the request was not admitted.
