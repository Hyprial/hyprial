// Package buildinfo pins the versions reported by the sidecar so the
// daemon can verify it launched the binary it installed.
package buildinfo

// SidecarVersion is the semantic version of this sidecar release.
const SidecarVersion = "0.1.0"

// TailcatCommit is the exact github.com/tailscale/tailcat commit this
// sidecar is built against, matching go.mod.
const TailcatCommit = "b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f"
