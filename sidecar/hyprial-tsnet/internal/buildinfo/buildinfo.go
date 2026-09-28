// Package buildinfo contains the independently released sidecar versions.
package buildinfo

// SidecarVersion is injected at build time from the tsnet-vX.Y.Z tag:
//
//	go build -ldflags "-X code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/buildinfo.SidecarVersion=X.Y.Z"
//
// A binary built without the injection reports "dev" and never impersonates a
// release. It must stay a var: -X is silently ignored on a const, which is how
// tsnet-v0.1.1 shipped binaries announcing 0.1.0.
var SidecarVersion = "dev"

// TailscaleVersion is the pinned tsnet dependency (go.mod).
const TailscaleVersion = "v1.102.3"
