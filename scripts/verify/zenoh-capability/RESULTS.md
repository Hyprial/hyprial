# P1 Zenoh capability result

Date: 2026-08-07

Baseline: locked `eclipse-zenoh==1.9.0`, macOS arm64, explicit loopback TCP,
multicast/gossip/shared-memory disabled. The runtime persistence check used the
official matching 1.9.0 standalone `zenohd`, storage-manager plugin, and
`libzenoh_backend_fs.dylib` extracted under `/tmp`; no system install occurred.

| Primitive | Result | Evidence | P1 decision |
|---|---|---|---|
| queryable | PASS | Python peer A declared a complete queryable; peer B received the exact reply bytes | use zenoh-python directly for registry/discovery and receipt queries |
| liveliness | PASS | observer received PUT on token declaration and DELETE on undeclare | use zenoh-python directly for actor/mailbox presence |
| filesystem storage | DIRECT BINDING GAP; FALLBACK PASS | `Session` exposes no storage declaration API; config-driven zenohd returned a put value and returned it again after router restart | supervise a pinned zenohd plus storage-manager/filesystem backend; Python remains the client/control layer |

Additional finding: Zenoh 1.9 enables POSIX shared-memory transport by default,
which failed in the managed test runtime with an OS permission error. The
isolated spike disables `transport/shared_memory/enabled`; formal transport
configuration must make shared memory an explicit opt-in until a host gate
proves it is available.

Wire TDD baseline before product implementation:

```text
RESULT RED 0/30 Python wire adapter is not implemented: expected h2b.contracts.wire.evaluate_case
```

This is an intentional red gate. No proto, transport, or inbox behavior was
implemented in the spike commit.
