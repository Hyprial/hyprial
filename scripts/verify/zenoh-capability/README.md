# Zenoh Python capability spike

This spike is the P1 risk gate from `docs/design/design.md` §7.1. It runs only over an
explicit loopback TCP endpoint with multicast and gossip disabled.

```sh
./spikes/zenoh/run.sh
```

Expected classification:

- `queryable`: request/reply must pass between two Python peer sessions.
- `liveliness`: token appearance and disappearance must pass between peers.
- `filesystem-storage`: Python has no direct storage declaration API, so this
  must report `FALLBACK_REQUIRED`. The checked-in JSON5 is the configuration
  boundary for a `zenohd` storage-manager plus filesystem backend.

If local runtime binaries are available, pass their exact paths so readiness is
reported without changing system installation:

```sh
./spikes/zenoh/run.sh \
  --zenohd /path/to/zenohd \
  --filesystem-backend /path/to/libzenoh_backend_filesystem.dylib
```

Without binary arguments the storage script validates binding absence and the
fallback config shape. With pinned binaries it starts a test-owned router,
proves put/get, restarts that router, and proves the value persists; all router
state and filesystem data stay under a temporary directory.

The Python dependency is pinned to `eclipse-zenoh==1.9.0` because Zenoh warns
that router/plugins/backends must be built from compatible releases. Runtime
assets used for the recorded spike were the official 1.9.0 macOS arm64
standalone router and filesystem-backend archives; nothing was installed into
the host system.

Primary references:

- https://zenoh.io/docs/manual/plugin-storage-manager/
- https://zenoh.io/docs/manual/abstractions/#storage
- https://github.com/eclipse-zenoh/zenoh-python
- https://github.com/eclipse-zenoh/zenoh-backend-filesystem
