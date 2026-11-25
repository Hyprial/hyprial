# Self-contained GUI Docker debug implementation plan

**Goal:** Run the PR debug GUI and a matching real Hyprial daemon entirely in an ephemeral Docker container.

**Architecture:** The host stages committed backend source and a remote `dev` GUI snapshot, builds one debug image, and publishes one loopback port. The image entrypoint owns daemon, source GUI, proxy, and one-use handoff.

**Tech stack:** Bash, Python 3.12, Node 24, uv, Docker, pytest.

## Global constraints

- Never mount or read a host Hyprial home, daemon socket, or credential into the image.
- Preserve the source GUI daemon admission, one-use handoff, and same-origin checks.
- Do not commit, push, or alter the daily daemon as part of this work.

### Task 1: Container ingress

- [ ] Extend `gui-debug-proxy.py` with an explicit public origin and ingress bind address.
- [ ] Write a failing proxy test for a container port different from the public port; verify Host/Origin rejection and internal rewrite.
- [ ] Implement and run the focused proxy test.

### Task 2: Image and entrypoint

- [ ] Write a failing entrypoint test that starts a fake daemon and fake GUI, then checks startup order, handoff, no grant in stdout, and cleanup.
- [ ] Add `Dockerfile.gui-live` with locked Python dependencies, Node 24, and a source checkout.
- [ ] Add a container entrypoint with a synthetic owner, isolated state, real daemon admission, normal/dev selection, proxy, and private handoff.
- [ ] Run the entrypoint test and shell syntax checks.

### Task 3: Host launcher

- [ ] Replace the old normal/dev fake Docker tests with failing tests for the new image context, loopback publication, handoff copy, and no host mounts.
- [ ] Update `run-gui-debug.sh` to stage the backend/GUI composite, build and run the image, copy the handoff, and clean up its container.
- [ ] Run focused tests and update `docs/guides/gui-docker-debug.md`.

### Task 4: Live verification

- [ ] Run normal and dev modes with a disposable container, inspect public health and private handoff permissions, and try the browser grant without printing it.
- [ ] Run Ruff, shell syntax, structural gates, and `git diff --check`; report the full-suite status and remaining business acceptance scope.
