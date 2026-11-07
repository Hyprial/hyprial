# Pending upstream proxy startup patch

**Status on `fix/tailcat-sidecar-http-proxy-nofork`: the checkout no longer depends
on this patch.** The consumer was rewritten against the unmodified pinned
`tailscale.com` (`v1.103.0-pre.0.20260929142145-a0e471a35b8f`): the typed
region-exhaustion consumer is gone, so `go build ./...` and `go vet ./...` pass
on the plain module. This patch is retained as the candidate for the cloud-PC
fork decision (KP-001); it is not activated by the checked-in `go.mod` and must
not ship in a binary.

On the fork-candidate branch (`fix/tailcat-sidecar-http-proxy`@ebb675788) the
consumer required the typed notification API, so a normal unactivated checkout
was compile-blocked there. Activation steps and the proposed fork are recorded in
the [round-3 evidence](../../../docs/evidence/tailcat-http-proxy-round3-2026-10-31.md#immutable-activation-gate);
no-fork measurements are recorded in
[`tailcat-http-proxy-nofork-2026-10-31.md`](../../../docs/evidence/tailcat-http-proxy-nofork-2026-10-31.md).

Base: `tailscale.com v1.103.0-pre.0.20260929142145-a0e471a35b8f`.
Consumer: `github.com/tailscale/tailcat v0.7.1-0.20260929145319-b4dc28e8aa89`.

`derp-proxy-node-timeout.patch` applies the direct path's 1500 ms per-node timeout
to proxy TCP/TLS/CONNECT work. It derives the deadline from the caller and stops
the cancellation callback before canceling the temporary context, so a successful
tunnel survives. Tests cover a hanging response, earlier caller cancellation,
hanging proxy TLS, and tunnel survival.

The same upstream file adds typed `ProxyConnectError` / `ProxyRegionError` and
`SubscribeProxyRejections`. A notification requires actual non-200 CONNECT
responses from every eligible node in one region. A healthy later node suppresses
it; mixed rejection/timeouts retain the existing retry behavior. Subscriptions
match client public identity, region ID and node definitions, and capture a
subscription generation when the regional dial starts. Probe clients are excluded;
unsubscribe invalidates delayed results and removes the registry entry. Only status
codes leave this boundary. Outbound subscribes per startup Ping, cancels and joins
that Ping on exhaustion, then closes before identity reuse. Established transports
are unchanged. No diagnostic drives cancellation. Tests cover 403/407/503, mixed
nodes, identity/region/probe isolation, delayed generations and concurrent cleanup.

The rest of this file reproduces the fork candidate on a disposable patched
dependency. The nofork checkout never uses that procedure; its measurements and
gates run directly against the pinned module.

## Reproduce the candidate without changing the module cache or repository

From `sidecar/hyprial-tailcat`, with the pinned dependencies already downloaded:

```sh
scratch=$(mktemp -d /tmp/hyprial-proxy-patch.XXXXXX)
version=v1.103.0-pre.0.20260929142145-a0e471a35b8f
cp -R "$(go env GOMODCACHE)/tailscale.com@$version" "$scratch/tailscale"
chmod -R u+w "$scratch/tailscale"
patch -d "$scratch/tailscale" -p1 < patches/derp-proxy-node-timeout.patch
(cd "$scratch/tailscale" && GOMAXPROCS=2 go test -race ./derp/derphttp -run '^TestProxy' -count=1)
cp go.mod "$scratch/candidate.mod"
cp go.sum "$scratch/candidate.sum"
go mod edit -modfile="$scratch/candidate.mod" -replace="tailscale.com=$scratch/tailscale"
GOMAXPROCS=2 go test -modfile="$scratch/candidate.mod" ./...
go vet -modfile="$scratch/candidate.mod" ./...
GOMAXPROCS=2 go test -modfile="$scratch/candidate.mod" -race ./...
go test -modfile="$scratch/candidate.mod" -c -o "$scratch/membership.test" ./internal/membershiptest
sandbox-exec -p '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote tcp "localhost:*"))' \
  "$scratch/membership.test" -test.run '^TestReviewProxy(Fallback|Budget|RegionHint)$' -test.v -test.count=20 -test.timeout=100m
go build -modfile="$scratch/candidate.mod" -o "$scratch/sidecar" ./cmd/hyprial-tailcat
go build -modfile="$scratch/candidate.mod" -o "$scratch/labrelay" ./cmd/hyprial-tailcat-labrelay
go build -modfile="$scratch/candidate.mod" -o "$scratch/address-fixture" ./internal/proxyfixture
```

The sandbox command is macOS-only. Linux/Windows real-environment equivalence is
not claimed. Keep the scratch directory until its processes have exited and the
results are recorded; remove only that owned scratch directory afterwards.
The repeat includes six fallback cases, per-exchange five-second headroom, and
three sequential exchanges that must not repeat rejected regions after the first.
The region hint retains only bounded, expiring metadata, never a transport.

## Real daemon/CLI timeout reproduction

From the repository root, use the three binaries above and an explicit checkout
CLI. The only additional network permission is Unix sockets inside the disposable
daemon fixture directory; UDP and non-loopback TCP remain denied.

```sh
HYPRIAL_BIN="$PWD/.venv/bin/hyprial" sandbox-exec -p \
  '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote tcp "localhost:*"))(allow network-outbound (remote unix-socket (regex #"^(/private)?/tmp/hr-px-")))' \
  uv run --no-sync python scripts/verify/tailcat_proxy_join.py \
  --binary "$scratch/sidecar" --relay "$scratch/labrelay" \
  --address-fixture "$scratch/address-fixture"
```

The runner creates two real disposable daemons, a local authenticated CONNECT
proxy and a local relay. It creates an org normally, stops the fixture authority,
uses fenced offline maintenance to install a synthetic authority pin with three
rejected regions before the healthy region, then restarts and issues an invite.
The fixture changes only prepared test pin data; no product hook, dial policy or
membership timeout is replaced. An empty/canceled TCP connection is not counted
as an authentication failure. It executes real `org join - --json` and checks the
join steps and all four proxy destinations. It never prints the invite or proxy
credentials.

The retained pre-notification candidate produced **FAIL** with
`MEMBERSHIP_SIDECAR_TIMEOUT` after 10.454 seconds on the original Python chain.
The new fail-fast candidate can sometimes fit that short budget; such a lucky run
does not validate the Python budget chain. To reproduce the old slow path, generate
`docs/evidence/tailcat-http-proxy-round3/notification-disabled-overlay.py` from the
repository root and build a separately labelled sidecar with
`-overlay=/tmp/sc3-no-notification.json`; do not count that differential as acceptance.
The integration branch must combine this candidate with the approved Python
operation-budget and diagnostic changes and rerun it for green acceptance. A red result is not a successful user-visible join. Cloud acceptance
still requires an authorized operator.
