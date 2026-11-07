# Tailcat engine layering: what Tailcat is, tsnet, and the path to one engine per node

Date: 2026-10-21. Status: **decision record** (Allen ↔ R&D head). Updated 2026-10-26: option B (Tailcat) shipped in 0.5.5 as protocol v5 (#1884); the engine A survey is #1752. Allen 2026-10-26: 「网络引擎不修改」, so A is not started. Items marked DECIDED stand.
Related: #1708 (cross-node loss under DERP), [Tailcat cutover architecture](tailnet-cutover-architecture-2026-10-03.md), [experiment](tailcat-casdoor-experiment.md) §18.4.

## 1. Why we moved from tsnet to Tailcat (recap)

- The earlier connectivity layer embedded **tsnet**. tsnet runs a full Tailscale node (`ipnlocal` backend + `controlclient`), so it needs a **control server**: the official tailnet or a self-hosted Headscale.
- With one Headscale per organization, every new org would mean deploying and operating another Headscale instance for that user. Avoiding this was the main reason to switch.
- With Tailcat, creating an org is in effect deploying a **decentralized Headscale**. The only external dependencies are the identity we provide (Casdoor/OIDC, device records in the org directory) and DERP relays.

## 2. What Tailcat actually is (measured on the pinned module `tailcat@v0.7.1-0.20260929145319-b4dc28e8aa89`)

- Tailcat is **not** built on tsnet. It assembles Tailscale's lower-level components directly (README "Network stack"; `tailcat.go`):
  - the userspace WireGuard engine, `wgengine.NewUserspaceEngine` (`tailcat.go:1903`);
  - magicsock (direct UDP, DERP, disco, NAT traversal);
  - gVisor netstack (`netstack.Create`, `tailcat.go:1868`);
  - DERP as rendezvous and fallback path.
- **Tailcat is its own control plane.** `locoBackend` (`tailcat.go:333`, `:1020`) synthesizes the network map locally and installs it with `SetNetworkMap` / `SetPeerConfigFunc` (`tailcat.go:1660-1700`). No control server exists.
- The library's limitation is in that synthesized map, not in the engine underneath:
  - a client map contains exactly one peer, "server.tailcat." (`tailcat.go:1675`);
  - `Client` has a single `Server` field (`tailcat.go:1920`);
  - each `Client` builds its own backend (`tailcat.go:1984-2002`).
- `wgengine` and magicsock themselves handle many peers per engine; that is how every Tailscale node works.
- **Why point-to-point.** Tailcat's stated goal is "a remix of Tailscale open source pieces to act like netcat, but over Tailscale's data plane, without Tailscale's control plane" (README). In the netcat model one side listens and one side connects, with metadata exchanged out of band per connection. So the client half is one `Client` per server address.
- **The server half is already one engine with many peers.** A server's network map starts with no peers. Each client's "meow" adds that client to the same engine lazily (`SetPeerConfigFunc` / `SetPeerByIPPacketFunc`, `tailcat.go:1620-1700`). The single-peer limit exists only in the client half.
- We use only Tailcat's library API (`tailcat.Client` / `Server`), not its CLI.

## 3. Why #1708 happens, in these terms

- A node runs one sidecar process with one server engine and one client engine per peer server. All client engines shared one client node key.
- DERP keeps one active connection per node key, so frames for one engine were delivered to another.
- The 10-03 cutover already split server and client keys for the same reason (experiment §18.4), but not client keys per server.

## 4. Cost model (engines, not processes)

| Shape | Processes per node | Engines per node | Engines in mesh | Peer relations |
|---|---|---|---|---|
| Today (shared client key: incorrect) | 1 | 1 + (N−1) | ~N² | N(N−1)/2 |
| **B**: per-server derived client keys (DECIDED for #1708) | 1 | 1 + (N−1) | ~N² | N(N−1)/2 |
| B + one tunnel per node pair (no library change: Zenoh links are bidirectional, so only one side dials, e.g. the lower node ID; optional, if both directions are built today) | 1 | ~1 + (N−1)/2 | ~N²/2 | N(N−1)/2 |
| **A**: one engine per node, many peers (Tailscale shape) | 1 | 1 | N | N(N−1)/2, as cheap entries inside each engine |

Peer relations are inherently O(N²) in a full mesh, and Tailscale has the same. Only A makes the **engine** count O(N).

## 5. Options for A, and the tsnet question

| Option | What it means | Assessment |
|---|---|---|
| A1. Switch to tsnet | Embed a full Tailscale node | **Reintroduces a control server** (Headscale or tailnet), which is the dependency we left. Not recommended unless we choose to run control again. |
| A2. Extend Tailcat (fork or upstream) | Make the client half multi-peer, mirroring the server half's existing lazy peer admission: per-peer server key, PSK, disco key and "meow" readiness; `DialTCPPort` takes a peer handle | **Recommended.** A local extension of `locoBackend`'s client branch, not a rewrite. Fork first, then propose upstream. |
| A4. One engine per node in both roles | A single engine that both serves and dials many peers, with one key and one DERP registration | Exactly Tailscale's shape (1 engine and 1 DERP connection per machine). **Needs code changes, larger than A2.** In the public API, `Server` has no `Dial` and admits peers only through their meow; `Client` cannot `Listen`; the two roles are separate types with separate network maps. A4 needs: adding known peers without a meow, dialing any peer, and merging the two roles into one peer type. |
| A3. Own engine host on Tailscale's packages (as Tailcat does) | Our sidecar builds `wgengine` + magicsock + netstack and synthesizes the network map from our org directory | In effect a rewrite of Tailcat. Unnecessary given A2. |

**Layering rule (Allen, 2026-10-21):** the Tailcat or engine layer is infrastructure. It must not know about Zenoh, OrgFS, orgs or spaces. It provides a tunnel to whichever peer the layer above asks for. Tunnel lifetime follows tunnel use (mapping in use, idle timeout), never space membership.

## 5a. Beyond Tailscale (Allen, 2026-10-21: widen the view)

The engine choice is compared across three families, against one criteria table: per-node resource growth with peer count, NAT traversal success, relay availability and cost, fit with the identity model (device keys and certificates), layering with Zenoh and mTLS, maintenance burden, and upstream activity.

| Family | Shape | Notes |
|---|---|---|
| Tailcat extension (A2/A4) | magicsock + DERP via a Tailcat fork | NAT traversal and relays proven at Tailscale scale. magicsock is an internal Tailscale package, not built for standalone reuse. |
| libp2p | One host with many peers; built-in multiplexing, DCUtR hole punching, circuit relay v2; the peer ID is the public key | O(N) natively. Cannot use Tailscale DERP as a relay without an adapter, so it needs its own libp2p relays. Hole-punch and relay quality must be measured, not assumed. A "libp2p + magicsock + DERP" hybrid means a magicsock-based libp2p transport, which is feasible but maintains the seam between two stacks (highest risk). |
| Zenoh-native | Zenoh's own QUIC/TLS transport and routing with our mTLS, plus our own relay/rendezvous | Could remove the tunnel layer entirely. NAT traversal becomes ours to solve. |

Proposed: a read-only study plus small experiments with the same measurements for each family (two-node and multi-node, forced relay), then a comparison report before any direction is chosen. B (#1708) proceeds regardless.

**Release (DECIDED, Allen 2026-10-21):** B ships as a flag day in 0.5.5, which is not compatible with older versions.

## 5b. Identity scope (DECIDED, Allen 2026-10-21)

- Only daemon (device) identity is guaranteed. Authenticated daemons inside our network are trusted.
- No author signatures for data relayed through other nodes, and no independent agent-level identity.
- If agent identity is needed later, it is added on the daemon side as a chain: device certificate → daemon → agent.
- Consequence: the Zenoh mTLS+ACL work meets the identity goal once complete.

**Study added (Allen, 2026-10-21):** iroh (n0-computer/iroh) and innernet (tonarino/innernet) join the comparison in §5a. Dispatched as PAC `wf-489a8e7d` (branch `design/network-engine-study`). Its headline question is whether Zenoh links over iroh connections give Tailscale-style traversal and relays without a sidecar engine per peer.

## 6. Status and next steps

- DECIDED: B for #1708, dispatched as PAC `wf-71783fb7` on `fix/tailcat-shared-client-1708`. Its design note will record derivation, authenticated admission of derived keys, rotation/migration/revocation, and whether pairs currently build tunnels in both directions.
- DECIDED (Allen 2026-10-26: 「网络引擎不修改」): do not start A; no A2 or A3 prototype is scheduled. Previously open: whether to start A, and A2 versus A3. Proposed next step: a read-only feasibility assessment of A2 (exact change surface in the client branch, tests, upstream appetite) and of the A4 single-engine dual-role variant.
- This document stays WIP until that decision; the PR is the discussion record.
