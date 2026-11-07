# Child lifetime binding

The public kernel function
`bind_current_process_to_parent(parent_pid, parent_identity, *, before_exit=None)`
binds the calling child to the launching parent's PID and stable birth identity.
Call it in the child after spawn. Invalid identity arguments fail immediately;
a changed parent PID, changed birth marker, missing process, or failed identity
probe causes the child to exit with status zero.

The kernel checks identity every 0.2 seconds. Optional `before_exit` cleanup
runs on a daemon thread with a 0.25-second allowance; callback exceptions and
blocked callbacks cannot prevent `os._exit(0)`. Cleanup is best effort, so the
caller must tolerate scratch remaining after an unavailable filesystem.

The returned release function disables the binding and joins its watcher
within 1.5 seconds. It is idempotent and raises if watcher cleanup cannot be
confirmed. A release cannot undo termination already committed after parent
loss. The binding terminates the calling child; its process-group owner still
owns explicit termination and reaping of the group.

Request deadlines, authorization expiry, retry, and readiness remain with
their existing owners. The kernel binding has no business deadline.

Verification uses disposable local processes: the kernel tests cover PID and
birth mismatch, unavailable identity, throwing/blocked callbacks, and release.
The Lark worker's real host-crash test also disables this public binding in a
control child: that child remains alive and must be explicitly reaped, proving
the binding is responsible for the positive result. Linux is verified;
native macOS/Windows behavior remains unverified in this change.
