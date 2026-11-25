# Self-contained GUI Docker debug run

## Decision

`make debug-gui MODE=normal|dev` runs a disposable Hyprial daemon and its
trusted source GUI in one Docker container. It uses a synthetic owner and an
ephemeral container-only home/state directory. No host Hyprial home, daemon
socket, credentials, or worktree is mounted. The backend source is the caller's
committed PR head; the GUI source is one freshly fetched remote `dev` commit.
The image is a debug composite, not a release wheel.

## Entry and trust boundary

The image installs Python dependencies from the lockfile and Node 24, and
contains a Git worktree assembled from those two source snapshots. A container
entrypoint starts the matching daemon, waits for real GUI admission, then starts
`hyprial gui` or `hyprial gui dev` with the `daemon` driver. The existing GUI
issues a one-use grant and serves static/Vite assets and `/daemon/*` over an
internal loopback port. A small container proxy binds the container ingress,
validates the browser's public Host and Origin, and rewrites them to that
internal origin. Docker publishes only the selected host loopback port.

The entrypoint writes a public-origin handoff file inside the container. The
host launcher copies it to a private mode-0600 file and prints only its path,
not the grant. The container's synthetic home and grant disappear on removal.
The host launcher owns only the container it created and never stops a host
daemon. Startup failure prints a stable code and preserves private diagnostic
files without logging a grant.

## Verification

Tests cover mode selection, source staging, no host-state mounts, daemon/GUI
startup order, public-origin proxy behavior, private handoff transfer, and
container cleanup. A live isolated run must prove `MODE=normal` serves the
GUI through the public port and a one-use browser grant reaches a real daemon;
`MODE=dev` must prove the Vite path. Specific business actions remain a
separate acceptance check.
