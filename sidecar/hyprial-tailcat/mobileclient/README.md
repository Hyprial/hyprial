# Native single-service Tailcat dial boundary

This Go package is a first host-tested slice for embedding the official userspace
Tailcat client in Core. It does not start the desktop sidecar, local daemon or a
harness. It does not open a client-side loopback forwarding socket.

The native composition root obtains the device/client identity and approved
service address from the official identity/admission APIs, constructs a pinned
`*tailcat.Client`, then transfers exclusive ownership with
`New(transport, servicePort, dialTimeout, connectionLimit)`. The port is fixed for
that client generation. `DialContext(ctx)` accepts no address/network/port and
returns an owned `net.Conn`; close each stream after use. Pending dials plus open
streams are capped at 32, with a per-dial timeout capped at 30 seconds. Idle/read
and application request deadlines must still be set by the protocol caller.

`Close()` cancels pending dials, closes streams and the transport, and permanently
rejects new dials. Suspend, identity revocation or target replacement requires
closing this generation and constructing a fresh one after revalidation. No
automatic send retry exists here: a request may have been accepted before a
connection failed, so the caller must preserve the original idempotency key.
Underlying dial/transport-close errors are replaced with fixed errors to avoid
leaking secret Tailcat addresses. Do not log transport structs or connection
addresses, and do not expose raw connections to a WebView.

This is **not yet a gomobile or C ABI**: `context.Context` and `net.Conn` are Go
interfaces. The native binding must expose bounded request/response handles and
lifecycle cancellation internally, while the WebView receives only approved chat
operations. No app endpoint, key persistence, request MAC, DERP-map protocol or
mobile device enrollment is invented here. Network admission is not application
authorization. The existing N-A2 work must supply the final signing integration.

Run with the exact toolchain pinned in the parent `go.mod`:

```sh
cd sidecar/hyprial-tailcat
go test -race -timeout 90s ./mobileclient
go vet ./...
go test -timeout 180s ./...
```

`compat_test.go` checks the actual pinned `tailcat.Client` interface.
`tailcat_test.go` uses temporary test keys, a local DERP/STUN relay and isolated
server to exchange real bytes over userspace Tailcat. It does not contact
production nodes, authenticate an App chat request, start an Agent, or establish
Android/iOS portability. See
`docs/evidence/app-tailnet-native-dial-2026-10-09.md` from the repository root for
platform gaps and the next Android/iOS work items.
