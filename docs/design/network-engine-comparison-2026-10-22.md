# Hyprial peer connectivity engine comparison

**Date:** 2026-10-22

**Status:** study; no engine decision

**Repository baseline:** `e19a74d9fbbbf350bc8e7d630239fd5aab985168`
**Experiment evidence:** [network-engine-study-2026-10-22](../notes/evidence/network-engine-study-2026-10-22/README.md)

## 1. Recommendation for the next decision

The lowest-risk next implementation is Tailcat A2: one multi-peer client engine
plus the existing multi-peer server engine. A2 keeps the current sidecar and
Zenoh TCP boundary. It also reduces engine count from `1 + (N-1)` to 2 per node.
This is a recommendation for the next prototype, not a final engine selection.
Confidence is **medium-high** because the server already proves that one Tailcat
engine can hold many peers. The client API and key migration still need code.

Iroh is the strongest replacement candidate. One Rust endpoint natively holds
many QUIC peers and provides direct paths plus relays. The local forced-relay
experiment passed at 2, 4, and 8 endpoints. Confidence is **medium** because the
experiment did not test real NAT combinations, relay failover, Zenoh mTLS, or
Python packaging. A Rust sidecar that maps an iroh stream to loopback TCP is the
first useful spike. A custom Zenoh link is not the first spike.

Go libp2p remains a viable replacement. It has a mature multi-peer host,
Direct Connection Upgrade through Relay (DCUtR), and Circuit Relay v2. It needs
libp2p relays and a stream-to-loopback bridge. Its protocol surface is larger
than Hyprial needs. Confidence is **medium**.

Innernet and Headscale can place their coordinator on an org member node. That
shape satisfies the corrected peer-to-peer criterion. Neither project documents
active-active control-plane replicas in the versions reviewed. Both also add
bootstrap and failover work. Innernet has no general relay fallback. Headscale
would restore the full Tailscale control client that Tailcat removed.

Zenoh's native TLS, TCP, and QUIC links remain the simplest option for reachable
peers. Zenoh does not supply general NAT hole punching or a DERP-equivalent
relay. It cannot consume magicsock or DERP through a Zenoh endpoint setting.

## 2. Decision boundary

The comparison uses these invariants:

1. The connectivity engine is infrastructure. It cannot know Zenoh, OrgFS,
   org, space, topic, or agent concepts.
2. Identity belongs to the daemon device. A future agent identity chains from
   the daemon identity.
3. Authenticated daemons inside the Hyprial network are trusted.
4. Zenoh mTLS and ACL remain an application-transport layer. A tunnel identity
   can add defense in depth, but cannot replace that work.
5. Hyprial uses library APIs. A bundled executable is acceptable as a sidecar;
   a user-facing engine CLI is not required.
6. A coordinator is acceptable when an org member node can run it. Multiple
   coordinators or a supported standby are advantages.
7. Dependence on a vendor tailnet or separately administered external control
   plane remains a risk.

The earlier phrase “no per-org control server” was too broad. This study does
not reject an engine merely because it has a coordinator. It separately asks:

- Can an org member daemon run the coordinator?
- Can the org run replicas or fail over without losing authoritative state?
- Can a new device reach the coordinator before joining the private network?
- Does ordinary peer traffic continue while the coordinator is unavailable?

## 3. Existing boundary

The current sidecar owns Tailcat keys, admission, relays, and loopback mappings.
`Engine.Up` creates one Tailcat server and installs the client-key admission
hook (`sidecar/hyprial-tailcat/internal/engine/engine.go:170-240`). `MapPeer`
allocates a loopback TCP port and dials a peer through a client lease
(`engine.go:345-390`). The client pool creates one `tailcat.Client` per secret
Tailcat address (`internal/engine/clients.go:40-53`).

Zenoh receives ordinary endpoints. Discovery is off by default, and configured
peers are desired links with retry (`src/hyprial/daemon/impl/transport/zenoh/config.py:92-109,178-213`).
The Python daemon opens a native Zenoh session from that configuration
(`transport.py:129-157`). This boundary keeps engine and application semantics
separate and should survive an engine change.

### 3.1 Scaling terms

This report separates four costs:

- **engine:** a complete network stack or host runtime;
- **peer state:** keys, addresses, path state, and timers for one remote node;
- **connection:** one direct or relayed transport connection;
- **stream:** one application flow multiplexed over a connection when supported.

A full mesh always has `N(N-1)/2` peer relations. The target is one or a constant
number of engines per node, with `O(N)` peer state. The target does not remove
the peer relations.

## 4. Criteria table

| Option | Coordinator and member-node fit | NAT traversal and relay | Per-node growth | Runtime and Zenoh integration | Migration assessment |
|---|---|---|---|---|---|
| Tailcat today | No control server. Directory is out of engine. DERP can be org-hosted. | magicsock, disco, STUN, DERP | `1 + (N-1)` engines; `O(N)` paths | Existing Go sidecar and loopback TCP | Baseline only; engine growth is unacceptable for a large mesh |
| Tailcat A2 | No control server. Existing directory distributes peers. | Same Tailscale data plane and DERP | 2 engines; `O(N)` peer state | No daemon or Zenoh boundary change | **Lowest migration cost; first prototype** |
| Tailcat A4 | No control server. Existing directory distributes peers. | Same Tailscale data plane and DERP | 1 dual-role engine; `O(N)` peer state | Same sidecar protocol; larger Tailcat fork | More code and lifecycle risk than A2 |
| go-libp2p 0.50.0 | No required coordinator. Org nodes can run discovery and relay services. Multiple relays are application-selected. | DCUtR and Circuit Relay v2; no DERP | 1 host; `O(N)` connections and peerstore | Go sidecar maps streams to loopback TCP | Medium-high change; mature components |
| iroh 1.4.0 | No required coordinator when the directory supplies addresses. Org nodes can run relays and address lookup. | QUIC hole punching and iroh-relay; no DERP | 1 endpoint; `O(N)` connections and paths | Rust sidecar is direct; custom Zenoh link is version-coupled | Medium change for TCP bridge; high change for custom link |
| innernet 2.0.0 | One org member can run `innernet-server`. No documented active-active server mode. | WireGuard endpoint discovery; no built-in general relay fallback | 1 WireGuard interface; `O(N)` peers and routes | Ordinary IP is easy for Zenoh; OS interface and privileges are not | High change and weaker difficult-NAT coverage |
| Zenoh native 1.9.0 (upstream 1.10.1 reviewed) | No coordinator is required. Org nodes can run multiple Zenoh routers. | Native links do not provide general hole punching or DERP fallback | Full mesh `O(N)` links; router clients `O(R)` | Already in the Python daemon; mTLS fits directly | Low only when peers are reachable |
| tsnet + Headscale 0.29.3 | A member can run a Headscale subprocess. Active-active control replicas are not documented. | magicsock and DERP through a full Tailscale node | 1 engine; `O(N)` peer state | Go sidecar or embedded tsnet; restores control client | High reversal cost and bootstrap complexity |

## 5. Experiment result

All required local relay experiments passed. Each row ran all endpoints and the
local relay in one process. RSS is therefore a topology-growth observation, not
an isolated per-node value.

| Engine | 2 endpoints max RSS | 4 endpoints max RSS | 8 endpoints max RSS | 2/4/8 aggregate payload rate |
|---|---:|---:|---:|---:|
| iroh 1.4.0 | 21.78 MiB | 25.92 MiB | 33.83 MiB | 30.17 / 57.70 / 64.10 MiB/s |
| go-libp2p 0.50.0 | 34.31 MiB | 39.20 MiB | 44.31 MiB | 32.34 / 66.36 / 81.69 MiB/s |
| current Tailcat sidecar | 53.98 MiB | 83.17 MiB | 117.33 MiB | 17.82 / 34.25 / 39.29 MiB/s |

Each sender used one 60-second echo stream. The iroh harness disabled direct IP
transports and checked the selected relay path. The libp2p harness supplied only
a circuit address and checked `ConnStats.Limited`. The Tailcat test relay forced
DERP. Full commands, bytes, connection counts, scripts, versions, and raw
`/usr/bin/time -l` output are in the evidence directory.

The experiment supports two bounded conclusions:

1. iroh and go-libp2p can keep 7 simultaneous relayed peer streams in one
   multi-peer runtime.
2. The current Tailcat star grew faster because the harness instantiated the
   current per-address client shape.

The experiment does not rank production throughput or NAT success. It used one
Apple Silicon host, loopback relays, one stream per peer, and no packet loss.
It also did not implement Tailcat A2 or A4.

## 6. Tailcat extension

### 6.1 Architecture

Tailcat assembles Tailscale's userspace WireGuard engine, magicsock, disco,
netstack, and DERP without `tsnet` or a control client. Its local backend
synthesizes a network map. The current client map contains one server, while the
server admits many clients into one engine. The pinned module is
`github.com/tailscale/tailcat@b4dc28e8aa89`.

Hyprial already separates inbound server and outbound client keys. The key file
and secret address stay outside argv and logs
(`docs/design/tailnet-cutover-architecture-2026-10-03.md:43-51`). The sidecar
proxies the local Zenoh TCP listener without understanding Zenoh
(`tailnet-cutover-architecture-2026-10-03.md:67-92`).

### 6.2 A2: multi-peer client

A2 changes the client half of Tailcat:

1. One client backend receives a locally synthesized map with many servers.
2. Each peer record carries the server WireGuard key, preshared key, disco key,
   relay addresses, and readiness state.
3. `DialTCPPort` receives a peer handle instead of relying on `Client.Server`.
4. Map updates add, rotate, and remove one peer without rebuilding the engine.
5. Existing admission and loopback forwarding remain above Tailcat.

A2 leaves two engines per node: one server engine and one outbound client
engine. Engine count becomes constant. Peer and path state remain `O(N)`.

The #1708 per-server derived client key is a transition constraint. One
WireGuard engine normally has one local node key. A2 therefore needs one stable
outbound device key, or an upstream mechanism for multiple local identities in
one engine. The migration must let remote admission accept the new stable key
before old per-server engines disappear. This key change is part of A2, not an
application-directory exception inside Tailcat.

### 6.3 A4: one dual-role engine

A4 merges server and client roles into one engine. The engine has one local
WireGuard identity and one DERP registration. It admits inbound peers and dials
known peers from the same network map.

A4 reaches one engine per node, but it changes more state transitions:

- known peers can enter before a Tailcat “meow” announcement;
- one peer object must support listen and dial roles;
- inbound admission, outbound readiness, rotation, and removal share one map;
- listener and dial failures cannot rebuild unrelated peers;
- the public `Server` and `Client` split no longer models ownership.

A4 is a second step only if measurements show that A2's second engine matters.
A direct A4 implementation has more lifecycle and upstream-rebase risk.

### 6.4 Coordination, identity, and relay

Tailcat has no control server. The Hyprial directory supplies peer records.
DERP supplies rendezvous and fallback data transport. An org can run DERP on a
reachable member node, and a DERP map can list multiple regions. Relay
redundancy is independent of org or space semantics.

Tailcat device keys authenticate the tunnel. Zenoh certificates authenticate
the daemon session. The directory must bind both public identities to the same
device record and generation. Tailcat cannot inspect a Zenoh certificate or
ACL.

### 6.5 Runtime, maturity, and risk

Tailcat and the current sidecar are Go. A2 preserves build, supervision,
protocol, diagnostics, and the loopback Zenoh handoff. Tailcat is BSD-3-Clause
and had an upstream v0.7.0 release on 2026-09-19. The reviewed Hyprial pin is a
newer pseudo-version.

Tailcat's upstream product remains point-to-point. A2 and A4 would be a fork
until upstream accepts a multi-peer API. Main risks are:

- a private fork tracks Tailscale internal package changes;
- key migration can split admission during mixed versions;
- one engine makes a map-update bug affect all peers;
- local relay success does not prove hostile-NAT success;
- secret Tailcat addresses still require careful directory custody.

Fixed sources: Tailcat commit
[`b4dc28e8aa89`](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/tailcat.go#L1-L32)
describes its magicsock, direct-path, and DERP shape. The pinned Tailscale
pseudo-version resolves to commit
`a0e471a35b8f38ee6abcfded937d3979ede68234`, which supplies
[`magicsock.Conn`](https://github.com/tailscale/tailscale/blob/a0e471a35b8f38ee6abcfded937d3979ede68234/wgengine/magicsock/magicsock.go#L158-L212),
[disco messages](https://github.com/tailscale/tailscale/blob/a0e471a35b8f38ee6abcfded937d3979ede68234/disco/disco.go#L34-L76), and
[DERP semantics](https://github.com/tailscale/tailscale/blob/a0e471a35b8f38ee6abcfded937d3979ede68234/derp/README.md#L1-L36).
The Go pseudo-versions and selected file hashes are retained in
[`source-audit-round-2.txt.md`](../notes/evidence/network-engine-study-2026-10-22/source-audit-round-2.txt.md).

### 6.6 Headscale and tsnet under the corrected criterion

An org member node can run Headscale as a subprocess. This is acceptable in
principle because the coordinator belongs to the org. Headscale can also run an
embedded DERP server. Ordinary direct peer traffic can continue during a short
control outage.

The reviewed Headscale 0.29.3 shape still has four costs:

1. Headscale is a service with configuration, database migrations, TLS, and a
   stable reachable URL. It is not documented as a small embeddable library.
2. SQLite is the recommended database. Upstream documentation does not describe
   active-active Headscale control replicas.
3. A standby needs replicated database, private keys, and endpoint failover.
   That operating model would be Hyprial-owned.
4. A new device must reach Headscale before the private network exists. A
   member-hosted coordinator therefore needs a public address or another
   bootstrap transport.

Restoring tsnet would also restore `ipnlocal`, `controlclient`, netmap policy,
and control-plane enrollment. The 2026-10-03 cutover removed those dependencies
(`docs/design/tailnet-cutover-architecture-2026-10-03.md:23-29,179-206`). The
corrected criterion removes the conceptual rejection, but it does not remove
migration or availability costs. Headscale is BSD-3-Clause and remains active;
0.29.3 was released on 2026-07-29. The project maturity does not supply the
missing active-active control contract.

Fixed source: Headscale commit
[`5aff68b5`](https://github.com/juanfont/headscale/tree/5aff68b5b9921db5ccb88013bb1740077ab872fb).
Its [database FAQ](https://github.com/juanfont/headscale/blob/5aff68b5b9921db5ccb88013bb1740077ab872fb/docs/about/faq.md#L111-L124)
recommends SQLite, its
[requirements](https://github.com/juanfont/headscale/blob/5aff68b5b9921db5ccb88013bb1740077ab872fb/docs/setup/requirements.md#L5-L43)
require a public server address and persistent state, and its
[DERP guide](https://github.com/juanfont/headscale/blob/5aff68b5b9921db5ccb88013bb1740077ab872fb/docs/ref/derp.md#L1-L35)
documents the embedded relay.

## 7. Go libp2p

### 7.1 Architecture and scaling

One libp2p `Host` owns identity, transports, secure channels, peerstore,
connections, stream multiplexing, and protocol handlers. A host has many peers.
A full mesh uses one host and up to `N-1` peer connections per node. Application
streams reuse those connections.

Circuit Relay v2 lets a private node reserve capacity on a relay. Another peer
dials a `/p2p-circuit/` address. DCUtR uses an existing relay connection to
coordinate simultaneous direct dials. If the punch succeeds, traffic upgrades
to a direct connection.

### 7.2 Coordination and failover

Libp2p does not require a central coordinator. Hyprial can distribute signed
`peer.AddrInfo` and relay addresses through its existing directory. DHT,
rendezvous, mDNS, or pubsub discovery can remain optional.

Org members can run relay and rendezvous services. Peers can reserve on more
than one relay and publish more than one circuit address. The application must
refresh reservations, choose addresses, and retry another relay. There is no
single authoritative relay database to replicate.

### 7.3 Identity and Zenoh fit

A libp2p Peer ID derives from the host public key. Hyprial can use a dedicated
connectivity key and bind the Peer ID to the daemon certificate in the device
record. Reusing the certificate private key is only suitable if key type,
rotation, and custody match libp2p's identity API.

Libp2p's secure channel authenticates the tunnel peer. Zenoh mTLS still
terminates above the loopback bridge and enforces daemon identity and ACL.
Libp2p must not inspect certificate subjects or org membership.

The practical integration is a Go sidecar protocol implementation:

- `up` starts one host and relay reservation manager;
- `map-peer` opens a local listener for one Peer ID;
- each accepted TCP flow becomes a libp2p stream;
- the remote handler proxies the stream to the configured local target;
- status reports paths and relay reservations without application names.

This preserves the current Python and Zenoh boundary. Libp2p streams are not an
IP interface, so a bridge is still required.

### 7.4 Maturity, license, and risk

The reviewed version is go-libp2p 0.50.0, released on 2026-09-21. The module is
MIT-licensed and actively maintained. Circuit Relay v2 and DCUtR are protocol
specifications implemented by multiple libp2p stacks.

The main risks are:

- Circuit Relay v2 has quotas and reservation lifetimes. The experiment raised
  its default 128 KiB circuit data limit for the 60-second run.
- DCUtR success still depends on both NATs and UDP reachability.
- Relay operations, abuse controls, reservations, and discovery become Hyprial
  responsibilities.
- The protocol suite and resource manager add more policy surface than a simple
  device tunnel needs.
- Libp2p relays cannot use DERP without a new transport adapter. A
  magicsock-backed libp2p transport would couple two complex stacks.

Fixed source: go-libp2p v0.50.0 commit
[`3c2c9021`](https://github.com/libp2p/go-libp2p/tree/3c2c902122cb6fae3897f729a3dbb9d4665ae90d).
The [`Host` interface](https://github.com/libp2p/go-libp2p/blob/3c2c902122cb6fae3897f729a3dbb9d4665ae90d/core/host/host.go#L24-L66)
provides peer state and multiplexed streams. The
[hole-puncher](https://github.com/libp2p/go-libp2p/blob/3c2c902122cb6fae3897f729a3dbb9d4665ae90d/p2p/protocol/holepunch/holepuncher.go#L20-L32)
starts DCUtR from a relay connection, and its
[direct-connect path](https://github.com/libp2p/go-libp2p/blob/3c2c902122cb6fae3897f729a3dbb9d4665ae90d/p2p/protocol/holepunch/holepuncher.go#L87-L185)
coordinates direct dialing and punching. Circuit Relay v2
[resource defaults](https://github.com/libp2p/go-libp2p/blob/3c2c902122cb6fae3897f729a3dbb9d4665ae90d/p2p/protocol/circuitv2/relay/resources.go#L9-L64)
fix the quotas used by the experiment.

## 8. Iroh

### 8.1 Architecture and scaling

One iroh `Endpoint` owns an Ed25519 `EndpointId`, QUIC connections, address
lookup, direct addresses, path selection, and relay connections. The Endpoint ID
is the public key. An endpoint connects by identity and uses direct or relay
transport addresses. A connection can move from a relay path to a direct path.

One endpoint holds many peer connections. Per-node engine count is 1, peer and
path state are `O(N)`, and QUIC multiplexes streams. The experiment confirmed
one endpoint runtime shape through 7 forced-relay peer streams.

### 8.2 Coordination and failover

Iroh does not require one control server. Hyprial can store `EndpointId`, direct
addresses, and relay URLs in its device directory. Iroh address-lookup providers
such as DNS, Pkarr, mainline DHT, or mDNS are optional.

An org member can run `iroh-relay` or embed the Rust relay server. The endpoint
can know more than one transport address. A deployment still needs to prove how
quickly version 1.4.0 moves to another relay after loss. Relay address updates
must remain available through the directory or an address-lookup provider.
There is no org authority inside the relay.

### 8.3 Identity and Zenoh fit

Iroh uses an Ed25519 endpoint key. Hyprial can bind the `EndpointId` to the
daemon certificate in the signed device record. Iroh encryption authenticates
the connectivity endpoint. Zenoh mTLS independently authenticates the daemon
and applies ACL.

Three integration paths answer the Zenoh question:

1. **Rust iroh tunnel with loopback TCP.** A sidecar maps each accepted local TCP
   flow to an iroh QUIC stream. This matches today's Tailcat shape and keeps
   Zenoh unchanged. It is the smallest useful iroh prototype.
2. **Zenoh over an iroh connection.** A Rust process implements a Zenoh link on
   an iroh bidirectional stream. This removes one TCP framing hop but joins two
   Rust runtimes and their reconnect semantics.
3. **Custom Zenoh link transport.** `zenoh-link` exposes low-level link traits,
   but the crate is internal. A custom link must integrate with
   `zenoh-transport`; it is not a normal `zenohd` plugin. The implementation
   would be coupled to Zenoh release internals and to the `zenoh-python` build.

The Python daemon cannot pass an arbitrary Rust iroh stream into the current
`zenoh.open()` call. Paths 2 and 3 therefore need a Rust component or a custom
Python extension. Path 1 reuses the existing sidecar protocol.

### 8.4 Maturity, license, and risk

The experiment pinned iroh 1.4.0 at
`55908a5809b61c72ec8deeff024038f4a9d68ab7`. The crate is licensed
MIT OR Apache-2.0. Version 1.4.0 was current and actively maintained during this
study. The relay derives from the same broad DERP design, but it is not the
Tailscale DERP protocol.

The main risks are:

- real NAT traversal and relay failover remain unmeasured;
- the project API has evolved from `NodeId` terminology to `EndpointId`;
- a Rust sidecar adds a second build and release toolchain;
- path 2 must reconcile iroh QUIC lifecycle with Zenoh link lifecycle;
- path 3 carries the highest Zenoh-version maintenance cost;
- production relay access control, limits, and observability need a design.

Fixed source: iroh 1.4.0 commit
[`55908a58`](https://github.com/n0-computer/iroh/tree/55908a5809b61c72ec8deeff024038f4a9d68ab7).
The crate's
[connection documentation](https://github.com/n0-computer/iroh/blob/55908a5809b61c72ec8deeff024038f4a9d68ab7/iroh/src/lib.rs#L60-L117)
specifies relay bootstrap, hole punching, authenticated Endpoint IDs, and relay
fallback. Its
[connection and stream documentation](https://github.com/n0-computer/iroh/blob/55908a5809b61c72ec8deeff024038f4a9d68ab7/iroh/src/lib.rs#L120-L174)
specifies multiplexed QUIC streams and address lookup. `RelayMode` is fixed in
[`endpoint.rs`](https://github.com/n0-computer/iroh/blob/55908a5809b61c72ec8deeff024038f4a9d68ab7/iroh/src/endpoint.rs#L1999-L2021).

## 9. Innernet

### 9.1 Architecture and scaling

Innernet manages a WireGuard mesh. `innernet-server` assigns network addresses,
stores peer records, organizes CIDRs, and shares endpoint changes. Clients
configure an OS WireGuard interface and routes. Ordinary payload traffic flows
directly between WireGuard peers rather than through the coordination server.

One interface holds `O(N)` WireGuard peers. This is a better engine-count shape
than current Tailcat clients. It also changes the host network namespace, route
table, and privilege boundary. Tailcat, iroh, and libp2p can stay in userspace
and expose only loopback ports.

### 9.2 Member coordinator and failover

An org member with a reachable HTTPS address can run `innernet-server`. This
satisfies the corrected coordinator criterion. The server uses a local SQLite
database. Version 2.0.0 does not document active-active servers or state
consensus. A cold or warm standby would need external database and key copying,
a stable endpoint, and a tested promotion procedure.

Existing WireGuard sessions can continue during a coordination outage. New
joins, peer changes, endpoint updates, and revocation wait for the server. A new
device also needs a bootstrap route to the member-hosted server.

### 9.3 NAT, identity, and Zenoh fit

The server reports observed endpoints, and WireGuard peers can roam between
endpoints. This can establish paths through compatible NATs. Innernet does not
provide a general relay fallback comparable to DERP, iroh-relay, or Circuit
Relay v2. A separately routed WireGuard gateway would add another topology and
operating model.

WireGuard public keys identify network peers. The server invitation and server
certificate add control identity. Hyprial would bind the WireGuard key to the
daemon certificate. Zenoh can then use ordinary WireGuard IP addresses and keep
mTLS and ACL above the interface.

### 9.4 Maturity, license, and risk

The reviewed release is innernet 2.0.0 under the MIT license. Its README states
that the project has not had an independent security audit and should be
considered experimental.

Migration cost is high because packaging must configure WireGuard on macOS,
Linux, and Windows, handle privileges, allocate CIDRs, avoid route conflicts,
and remove current loopback mappings. The absent relay fallback is the primary
connectivity risk. Innernet should be reconsidered only if Hyprial accepts a
managed OS VPN interface or innernet adds a suitable relay and supported server
failover.

Fixed source: Innernet v2.0.0 commit
[`dbdb0097`](https://github.com/tonarino/innernet/tree/dbdb0097b397fa5b10566ae58d33c699142102f2).
The README defines the
[required coordinator](https://github.com/tonarino/innernet/blob/dbdb0097b397fa5b10566ae58d33c699142102f2/README.md#L14-L28),
[invitation bootstrap](https://github.com/tonarino/innernet/blob/dbdb0097b397fa5b10566ae58d33c699142102f2/README.md#L38-L60), and
[fallible endpoint discovery](https://github.com/tonarino/innernet/blob/dbdb0097b397fa5b10566ae58d33c699142102f2/README.md#L124-L135).
The server opens one local SQLite file in
[`server/src/lib.rs`](https://github.com/tonarino/innernet/blob/dbdb0097b397fa5b10566ae58d33c699142102f2/server/src/lib.rs#L157-L174).
The exact source tree is identified by
the upstream archive <https://github.com/tonarino/innernet/archive/dbdb0097b397fa5b10566ae58d33c699142102f2.tar.gz> (SHA-256 `287f6aeffda76e29483299917ec15c5efd3b7e487c15f3531ea4454f22da737a`, not stored in the repository)
and the [round-2 source audit](../notes/evidence/network-engine-study-2026-10-22/source-audit-round-2.txt.md).

## 10. Zenoh-native transports

### 10.1 Pinned and current source result

The workspace pins `eclipse-zenoh==1.9.0` (`pyproject.toml:20` and
`uv.lock:352-355`). Zenoh 1.9.0 commit
`81c6c933b6e41d72a05f04c4442ef57717ddc72b` is therefore the baseline. Zenoh
1.10.1 commit `1211779c3647f5a96713dade452c546a07823580` was the current release
when this study ran. Version 1.6.2 was tagged on **2025-10-17**, not
2026-10-17. Versions 1.9.0, 1.10.0, and 1.10.1 were tagged on 2026-04-10,
2026-08-14, and 2026-09-07. The fixed tag objects, commits, and dates are in the
[round-2 source audit](../notes/evidence/network-engine-study-2026-10-22/source-audit-round-2.txt.md).

Zenoh 1.9.0 provides feature-gated TCP, TLS, QUIC, QUIC-datagram, UDP,
WebSocket, and Unix-socket links. Peer mode can form a mesh. Client mode can
connect to one or more routers. Multicast and gossip scouting discover
reachable nodes. The pinned configuration itself warns that its ZID ordering
strategy may fail when a peer is behind a private IP. Neither the exhaustive
1.9.0 `LinkKind` list nor the 1.10.1 list contains hole punching or a relay link.
No reviewed configuration provides a DERP-equivalent fallback.

A full peer mesh owns `O(N)` Zenoh links per node. A router topology gives each
client `O(R)` links to R routers while each router owns `O(N)` links. Org member
nodes can run several routers, and clients can configure several endpoints.
This gives application-level routing failover without a separate coordinator.
It does not create a generic tunnel for other daemon services.

Zenoh TLS can terminate mTLS directly. This is the cleanest device-certificate
fit. The current Python configuration already accepts endpoint locators and
retry policy. Native Zenoh is suitable when direct addresses or reachable
member routers already exist.

### 10.2 Can Zenoh reuse magicsock and DERP?

Zenoh 1.9.0 and 1.10.1 cannot directly configure a magicsock or DERP locator.

- Magicsock is a Go component inside Tailscale's `wgengine`. Its fixed source
  contains disco keys, DERP connections, path state, and network-map updates.
- DERP relays encrypted packets addressed by WireGuard public key. A Zenoh QUIC
  endpoint cannot send ordinary QUIC packets to a DERP URL.
- Zenoh links are Rust crates selected by Cargo features. In both reviewed
  releases, `LinkKind` is a closed enum and `LinkManagerBuilderUnicast` uses a
  fixed match over built-in managers.
- The dynamic plugin API starts application plugins such as REST and storage.
  It does not register a new `LinkKind`. The pinned `zenoh-python` extension
  compiles Zenoh 1.9.0 into the wheel and exposes no foreign link registration.

There are three concrete reuse shapes:

1. **Go magicsock tunnel carrying Zenoh TCP or QUIC.** This is today's Tailcat
   sidecar shape. Zenoh sees loopback TCP. The sidecar can later expose UDP if a
   Zenoh QUIC tunnel has a measured benefit. This keeps the stacks separate.
2. **Rust iroh connection carrying Zenoh.** Iroh supplies analogous traversal
   and relays in Rust. A loopback TCP bridge keeps the pinned Python wheel and
   Zenoh mTLS unchanged. A native Zenoh link over an iroh stream requires a
   custom Rust build and joins both runtimes' reconnect semantics.
3. **Custom Zenoh link over magicsock.** This needs a Go/Rust FFI boundary or a
   reimplementation. It also needs source changes in the internal `zenoh-link`
   and transport crates, followed by a custom `zenoh-python` build. This is not
   a deployable runtime plugin in 1.9.0 or 1.10.1.

The concrete answer is therefore **yes only through a tunnel boundary**:
magicsock plus DERP can carry traffic that Zenoh sees as TCP today. Zenoh cannot
reuse magicsock or DERP as a native locator. Iroh offers the plausible all-Rust
traversal layer, but its lowest-coupling integration is still a TCP bridge.

### 10.3 Maturity, license, and risk

The pinned 1.9.0 and current 1.10.1 source use EPL-2.0 OR Apache-2.0. Built-in
links ship with Zenoh. Both releases mark the link crates as internal and warn
that their API can change even in patch releases. The meaningful 1.10.1 change
in this area is `uring` feature plumbing; the closed link-kind and builder shape
remain. The custom-link maintenance conclusion is unchanged after reviewing the
actual pinned and current releases.

Native Zenoh removes a tunnel only for reachable deployments. Using member
routers for every flow changes failure and bandwidth placement. It also mixes
application routing with connectivity fallback. A custom link should wait for
a stable upstream extension contract or a jointly maintained upstream change.

Fixed sources: Zenoh 1.9.0
[`zenoh/Cargo.toml`](https://github.com/eclipse-zenoh/zenoh/blob/81c6c933b6e41d72a05f04c4442ef57717ddc72b/zenoh/Cargo.toml#L30-L75),
[`DEFAULT_CONFIG.json5`](https://github.com/eclipse-zenoh/zenoh/blob/81c6c933b6e41d72a05f04c4442ef57717ddc72b/DEFAULT_CONFIG.json5#L18-L200),
[`zenoh-link`](https://github.com/eclipse-zenoh/zenoh/blob/81c6c933b6e41d72a05f04c4442ef57717ddc72b/io/zenoh-link/src/lib.rs#L85-L177), and
[link warning](https://github.com/eclipse-zenoh/zenoh/blob/81c6c933b6e41d72a05f04c4442ef57717ddc72b/io/zenoh-links/README.md#L1-L5).
The fixed 1.9.0
[plugin README](https://github.com/eclipse-zenoh/zenoh/blob/81c6c933b6e41d72a05f04c4442ef57717ddc72b/plugins/README.md#L15-L38)
describes application plugins. Zenoh 1.10.1 retains the
[`LinkKind`](https://github.com/eclipse-zenoh/zenoh/blob/1211779c3647f5a96713dade452c546a07823580/io/zenoh-link/src/lib.rs#L85-L177)
and
[fixed builder](https://github.com/eclipse-zenoh/zenoh/blob/1211779c3647f5a96713dade452c546a07823580/io/zenoh-link/src/lib.rs#L385-L425).
The pinned Python binding compiles the Rust crate in
[`Cargo.toml`](https://github.com/eclipse-zenoh/zenoh-python/blob/0659dbd3733f6af06307785ccd2f3bcc171bc75f/Cargo.toml#L35-L55).

## 11. Cross-option identity model

The engine choice does not need one private key format for every layer. A device
record can bind these values:

- stable Hyprial device ID;
- daemon mTLS certificate chain and generation;
- connectivity public key: Tailcat node key, libp2p Peer ID, iroh Endpoint ID,
  innernet WireGuard key, or Tailscale node identity;
- advertised direct and relay addresses;
- validity interval and replacement generation.

The directory authority signs or otherwise authenticates that binding. The
engine consumes only its connectivity fields. Zenoh verifies the daemon
certificate and ACL. Key rotation publishes the new generation before removing
the old generation. No engine receives org, space, topic, or agent identity.

## 12. Migration plan and decision gates

### Stage 0: keep the current repair independent

Ship the #1708 per-server client-key repair as planned. The engine study does not
weaken the current correctness fix. Record that A2 later changes the outbound
key model again.

### Stage 1: freeze an engine-neutral sidecar contract

Keep `up`, `map-peer`, `unmap-peer`, status, admission, and diagnostics in terms
of device peers and local ports. Do not add Zenoh, org, or space fields. Add
engine capability and path-kind fields only if every candidate can implement
them.

### Stage 2: build the A2 prototype

Implement multi-peer client state in a pinned Tailcat fork. Test:

- 2, 8, and 32 peers in one real sidecar process;
- add, rotate, remove, and redial without rebuilding unrelated peers;
- direct-to-relay and relay-to-direct path changes;
- stable outbound key migration from #1708;
- two local relays with loss of the selected relay;
- Zenoh mTLS and ACL through unchanged loopback endpoints.

A2 remains preferred only if a map change is peer-local and the fork can track
the pinned Tailscale packages with bounded patches.

### Stage 3: build the iroh TCP-bridge prototype in parallel

Implement the same engine-neutral sidecar operations in Rust. Use one endpoint
per daemon and one stream per accepted TCP flow. Test the same topology, key
rotation, relay loss, and Zenoh mTLS cases. Add a real NAT matrix across Linux,
macOS, home routers, carrier-grade NAT, and UDP-blocked networks.

Do not implement the custom Zenoh link in this stage. The TCP bridge first
answers whether iroh traversal and operations are good enough.

### Stage 4: choose with new evidence

The following findings would change the current recommendation:

- Choose iroh over A2 if its bridge passes the real NAT and relay-failover matrix
  and its packaging cost is acceptable.
- Keep A2 if it stays a small fork and matches current Tailscale path success
  with constant engine count.
- Escalate from A2 to A4 only if the second engine has a measured cost that
  justifies the larger role merge.
- Reconsider native Zenoh if Zenoh adds supported NAT traversal and relay
  semantics, or a stable custom-link API.
- Reconsider libp2p if iroh fails the NAT matrix or relay operations, and
  libp2p passes the same cases.
- Reconsider innernet if it gains a suitable relay, cross-platform privilege
  handling, and documented coordinator failover.
- Reconsider Headscale if an embeddable member-node deployment and supported
  multi-replica control state become available.

## 13. Confidence and unresolved questions

| Finding | Confidence | Missing evidence |
|---|---|---|
| Current Tailcat creates one outbound client engine per address | High | None; repository source and baseline agree |
| A2 can reduce engine count to 2 | Medium-high | Working multi-peer client and key migration |
| A4 can reduce engine count to 1 | Medium | Dual-role lifecycle implementation |
| iroh is the strongest replacement candidate | Medium | Real NAT, failover, packaging, Zenoh mTLS E2E |
| go-libp2p is technically viable | Medium | Same real NAT and operational relay tests |
| Innernet coordinator can be an org member | High | Supported active-active or standby procedure |
| Native Zenoh alone covers general NAT | High confidence that it does not | An upstream traversal feature would change this |
| Headscale member hosting is feasible but not active-active | Medium | Upstream-supported replica design and bootstrap test |

Allen retains the engine decision. The next review should compare the A2 and
iroh prototypes with the same NAT, failover, identity-rotation, and process-level
resource tests.

## 14. Source versions

This study read these fixed sources:

- Hyprial repository `e19a74d9fbbbf350bc8e7d630239fd5aab985168`;
- WIP Tailcat layering record at branch commit
  `3e0d78e9a5c6e308267684117f1a9b45e674744e`;
- Tailcat `b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f` and Tailscale
  commit `a0e471a35b8f38ee6abcfded937d3979ede68234`, resolved from the
  pseudo-version in `sidecar/hyprial-tailcat/go.mod:6-8`;
- iroh 1.4.0 commit `55908a5809b61c72ec8deeff024038f4a9d68ab7`;
- go-libp2p 0.50.0 commit `3c2c902122cb6fae3897f729a3dbb9d4665ae90d`;
- Innernet 2.0.0 commit `dbdb0097b397fa5b10566ae58d33c699142102f2`;
- workspace-pinned Zenoh 1.9.0 commit
  `81c6c933b6e41d72a05f04c4442ef57717ddc72b`;
- current Zenoh 1.10.1 commit
  `1211779c3647f5a96713dade452c546a07823580`;
- zenoh-python 1.9.0 commit `0659dbd3733f6af06307785ccd2f3bcc171bc75f`;
- Headscale 0.29.3 commit `5aff68b5b9921db5ccb88013bb1740077ab872fb`.

The [round-2 source audit](../notes/evidence/network-engine-study-2026-10-22/source-audit-round-2.txt.md)
records tag dates and the SHA256 of each reviewed source file, so the claims can be
checked against those upstream revisions. The source trees themselves are not kept in
the repository; see the evidence README and SHA256-ORIGINALS.md.

The requested #1708 design path did not exist at branch commit
`24f2baaf9a58a03c112726126bbd60fbdf392063`. The study therefore treats the WIP
layering record and current sidecar source as the authority for that transition.
