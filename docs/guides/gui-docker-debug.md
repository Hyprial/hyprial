# Self-contained GUI Docker debug run

`make debug-gui` runs a disposable Hyprial daemon and the trusted source GUI
inside one Docker container. The daemon and CLI come from the current worktree's
**committed HEAD**. The GUI comes from a fresh remote `dev` snapshot. Both modes
use an isolated synthetic owner and empty container-only home/state; neither
mode mounts or reads the host's Hyprial home, daemon socket, or credentials.

Requirements: Docker, Git, Make, `tar`, access to the repository's `origin`, and
network access for the image's locked Python/npm dependencies on first build.
The host does not need Node, npm, uv, or a running Hyprial daemon.

```sh
cd /home/lenovo/doer/repos/hyprial-gui-docker-20261109
make debug-gui MODE=normal PORT=4382
```

The image installs Node 24 and the source Hyprial package, then the container
starts its own daemon and source GUI. A container ingress proxy publishes only
`127.0.0.1:4382` on the host, validates Host/Origin, and routes static assets
and `/daemon/*` to the trusted GUI service. The source GUI's normal catalog
admission and one-use browser grant remain active. The command prints the
backend commit, remote `dev` commit, public origin, and a path to a private
mode-0600 handoff file. Open the URL inside that file in the local browser
within 60 seconds. Do not paste the grant into chats, logs, or tickets. Opening
only the public origin shows `SESSION_REQUIRED` because it lacks a grant.

For Vite hot reload from the same remote `dev` GUI snapshot:

```sh
make debug-gui MODE=dev PORT=4382
```

Both commands stay in the foreground. Press Ctrl-C to remove the owned
container; its ephemeral home, daemon, and browser grant are then discarded.
The locally built image remains cached. The caller worktree and daily daemon
are untouched. The GUI source snapshot is a debug composite with the committed
backend head; uncommitted backend changes are not packaged.

A startup failure prints a stable code and a private diagnostics directory.
Its files may contain local details and should be inspected privately. `MODE`
accepts only `normal` or `dev`; the requested port must be free. This is an
isolated real-daemon test, not access to daily data or a complete login and
first-run journey. Validate specific business actions separately; a loaded
page alone does not prove they work.
