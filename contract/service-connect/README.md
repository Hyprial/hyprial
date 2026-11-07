# Service connect baseline-C contract

This directory freezes the shared v0.4 contracts before any service-connect
runtime is activated. `fixtures.json` is the machine-readable cross-slice
fixture; this document defines the validation and authority rules that cannot
be expressed as field lists alone. The settled source is
`docs/design/service-connect/design-service-connect.md` section 8.

## Authority and catalog

The public catalog entry is exactly
`{name,deviceId,remotePort,protocol,localPort,usage,env,auth,guide}`. Entries
live at the root of the pinned orgfs services space. Names and device IDs match
`[a-z0-9][a-z0-9_-]{0,62}`. Ports are integers, never booleans: remote ports
are 1..65535 and a non-null preferred local port is 1024..65535. Protocol is
`tcp` or `http`; both are byte-for-byte TCP. All keys are required. Unknown,
duplicate, legacy target/tailnet/address/PSK, unsafe env-template, and
credential-bearing URL fields are rejected.

The private `service-access.json` record has schema version 1, a catalog trust
pin, and device records with exactly
`{deviceId,serverPublic,address,generation,ports,enabled}`. It is daemon-only
operator material. It never enters public IPC, logs, argv, environment,
desired state, the catalog cache, or repository evidence. Missing, unreadable,
invalid, disabled, unapproved-port, key-mismatched, or changed-same-generation
records fail closed. There is no last-good fallback for this protected record.

Catalog approval permits an attempt. The protected record approves the exact
device, server key, generation, and port. The service host separately admits
the client public key, and the application still authenticates its own users.
No layer substitutes for another.

## Wire and observations

Service operations require protocol v3 capability `service-connect-v1` and a
unique `requestId`. The exact request and response field sets are in
`fixtures.json`; closed-schema validation rejects unknown or mistyped fields.
The private Tailcat address appears only in daemon-to-sidecar `map-service` and
is never returned.

Every mapping observation M has all `mappingFields`. Its path is
`direct|relay|unknown`; dial state is `unknown|ok|error`. Counts, times, and
durations are finite and nonnegative, with booleans rejected. Timestamps are
Unix milliseconds. Fresh observations use zero/null/unknown, and status never
fabricates stale telemetry. `pathDetail`, `lastError`, and every wire message
are sanitized and never include a Tailcat address or raw library error.

Daemon result R adds `state` (`listening|pending|failed`) and nullable
`errorCode`. An unbound row may use null binding fields. A remembered local
port on a failed row is not proof of a listener.

## Durability and ordering

The only new durable fields are `serviceConnections` and optional
`serviceRegistry`. Connections store names and ports; the registry cache stores
only normalized orgfs overrides and its trust metadata. Neither stores private
addresses, keys, sockets, or telemetry. Old documents load with no connections
and no cache. A valid empty override is distinct from an absent cache.

Validation of the catalog, sidecar capability, and current protected record
precedes desired-state mutation. Connect persists intent, maps, then persists
the actual local port before success. Disconnect durably removes intent before
unmapping. Late results retain exact attempt custody and cannot satisfy a later
operation or resurrect removed intent.

## Errors

The nine `SERVICE_*` values in `fixtures.json` are grow-only daemon IPC codes.
`SERVICE_TAILNET_MISMATCH` does not exist. The existing
`SERVICE_BINDING_NOT_FOUND` is unrelated and unchanged.

Unknown RPC fields and target/address/bind/credential overrides fail at the
daemon boundary with `INVALID_ARGUMENT`. The frozen Go `service-failed` code set is the nine existing protocol codes in
`fixtures.json`. `goToService` is the only Go-to-daemon translation: malformed
requests map to `SERVICE_INVALID_TARGET`, bind collisions map to
`SERVICE_PORT_IN_USE`, unknown peers map to `SERVICE_DEVICE_UNAVAILABLE`, and
the remaining backend/start/dial/internal failures map to
`SERVICE_UNAVAILABLE`. Daemon-owned catalog, capability, identity, and
authorization failures use their specific `SERVICE_*` code before a wire call.

No SERVICE_* code is transient in `TRANSIENT_CODE_CLASSES`. Accepted service
work retains custody and is reconciled instead of being blindly resubmitted.
There is no generic DaemonRequestError mapping for service codes; runtime code
must use a specific domain error and translate once at the daemon IPC boundary.

## Budgets and lifecycle

Python uses the exported 3-second control budget, 1-second registry/protected
record read budget, and 5-second reconcile cadence. The Go service dial budget
is 5 seconds. These are bounded policy defaults, not measured promises. A
control budget applies per wire operation, while an established stream has an
unbounded stream lifetime. The service host must implement bounded host shutdown
that closes listeners and active streams; the implementation slice
must pin its concrete shutdown deadline and tests before activation.

## Daemon log events

The complete v0.4 service-connect daemon event namespace is frozen below. No
implementation slice may add another event without updating this contract and
the startup-event reconciliation gate.

- `service.connect.registry.updated`
- `service.connect.registry.fallback`
- `service.connect.access.invalid`
- `service.connect.mapping.failed`
- `service.connect.mapping.closed`
- `service.connect.reconcile.completed`
- `service.connect.reconcile.failed`
- `service.connect.restore.failed`

Events carry only safe identifiers, codes, counts, and timings. They never
carry the protected address, server public key, application credentials, raw
catalog text, or raw exception text.

## Generic host command and config

The source distribution provides `hyprial-tailcat host --config <absolute-path>`
and a reusable operator-built image. The command owns one Engine lifecycle;
it is not a daemon and does not create an inbox. The config is a closed object
with exactly `keyFile`, `addressFile`, `clientPublic`, `allowAny`, and
`exposures`. Both file paths are absolute. `clientPublic` is the public caller
key allow-set, `allowAny` must be `false`, and an empty set denies every caller.

Each exposure is exactly `{port,target,proxyProtocol}`. `port` is 1..65535;
`target` is either `tcp:127.0.0.1:<port>` or an absolute Unix socket; and
`proxyProtocol` is `""` by default or explicit `"v2"` for a separately reviewed
PROXY-aware ingress. Unknown fields, non-loopback TCP targets, invalid ports,
and permissive admission fail before Engine startup. SIGHUP atomically applies
a valid config and retains the prior config on invalid reload. SIGTERM closes
listeners and streams within the host's bounded shutdown. Key generation is a
separate explicit `genkey` operation; host startup never regenerates a key.

`sidecar/hyprial-tailcat/deploy/Dockerfile` and `deploy/compose.yaml` are the v1
artifact contract. They are source-only: this repository publishes no registry
image. The Compose host shares the application's network namespace, has a
read-only filesystem plus a private state volume, and uses no published app
port, Docker socket, host networking, privilege, NET_ADMIN, or TUN device.
