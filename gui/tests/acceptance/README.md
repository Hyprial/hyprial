# GUI Browser Acceptance Suite

End-to-end browser acceptance tests driving the real `hyprial gui` / `hyprial gui dev` CLI, the Python session host, Contract Mock, isolated real `hyprial daemon run`, and headless Chromium.

## Layout

```text
gui/tests/acceptance/
  support/
    home.mjs        Isolated temp roots (`withRoot`), `cleanEnvironment`, `prepareDaemonHome`, `withSourceWorktree`, driver filter (`HYPRIAL_ACCEPTANCE_DRIVERS`)
    ipc-proxy.mjs   Minimal network helper: `freePort` and `startIpcDropProxy` (fault-injection proxy on daemon AF_UNIX socket)
    cli.mjs         `hyprial gui` / `hyprial gui dev` launcher, startup JSON / private `launch.url` handoff checks, process-group cleanup (`terminateGuiAndCheck`, `stopChild`)
    daemon.mjs      Isolated `hyprial daemon run` startup, `ipcCall`, readiness polling, `onboarding.firstRun.skip`
    browser.mjs     Playwright Chromium launcher, HTTP `probe`, chat layout capture, ordinary App & dev-tools browser flows
    assertions.mjs  Built dev-tools path enumeration and Settings read-only catalog assertions
  shell/
    lifecycle.acceptance.mjs   Ordinary App driver parity, `gui dev` handoff consumption, launch expiry, SIGTERM reclamation
    settings.acceptance.mjs    Settings wired & read-only checks for both drivers
    dev-tools.acceptance.mjs   `--with-dev-tools` matrix (`N`, `N-daemon`, `M`, `D`) and `/docs` Try-it-out checks
    live-source.acceptance.mjs Live-source HMR/reload (`L`, `L-plain`) and `gui dev` over real daemon (`L-daemon`)
  org/
    org-create.acceptance.mjs   Local org creation, nullable publication, member projection, file/folder trash & restore, invalid/duplicate create, delete refusal
    org-unknown.acceptance.mjs  IPC response drops (`orgfs.write`, partial `orgfs.restore`, post-write `orgfs.ls` refresh failure) without automatic retry
    org-surfaces.acceptance.mjs Invites, join, machines, file move, settings segments, `org.fetch` sync, and leave preview
  agents/
    agent-create-loss.acceptance.mjs  G6 `agent.create` lost response without retry
    lark-unbind.acceptance.mjs        Lark adapter conditional unbind across 5 non-replaying IPC-drop scenarios
  chat/
    chat-mock.acceptance.mjs    Contract Mock chat workspace reads (`conversations.history`), send, and recovery state
    chat-daemon.acceptance.mjs  Real-daemon 1:1 chat happy path, reply, messageId parity, polling rate, IPC-drop recovery, legacy id, and daemon SIGTERM restart recovery
    group-daemon-flow.acceptance.mjs  Real-daemon group chat list, polling isolation, messages, reply, departed member names (`G1–G5`, `G10`, `G12`), and 390px narrow viewport / create-dialog keyboard accessibility (`G11`)
    group-daemon-admin.acceptance.mjs Real-daemon group chat dropped `conversations.post` recovery (`G6`), UI create + members add/remove + real `AGENT_NOT_FOUND` rejection (`G7`, `G8`), and dropped `conversations.create` retry (`G9`, `G12`)
```

Every `.acceptance.mjs` file allocates an isolated temp root (`withRoot`), ephemeral loopback port (`freePort()`), isolated `HYPRIAL_HOME` / `HARNESS_STATE_DIR`, and its own Playwright browser instance so individual acceptance files can still be invoked standalone without shared state.

## Running

Build the GUI first (`npm --prefix gui run build`), then run with proxy variables unset so loopback HTTP/WebSocket traffic never routes through an external proxy:

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  HYPRIAL_ACCEPTANCE_ACTOR_BACKEND=reference-abi \
  npm --prefix gui run test:acceptance
```

- Concurrency: `test:acceptance` runs `node --test --test-concurrency=1 'tests/acceptance/**/*.acceptance.mjs'` (quoted so `/bin/sh` passes the recursive glob to Node's test runner). File-level execution is serialized (`--test-concurrency=1`) because source-mode `hyprial gui` runs `npm run build` (`vite` with `emptyOutDir: true`) into the shared `gui/dist/` directory on every non-live launch, so running multiple acceptance files concurrently races on `gui/dist/assets`. Per-file isolated roots, ports, and homes are preserved so individual acceptance files can still be invoked standalone.
- Driver filtering: set `HYPRIAL_ACCEPTANCE_DRIVERS=contract-mock` or `HYPRIAL_ACCEPTANCE_DRIVERS=daemon` to run one driver side; skipped legs/tests emit `NOT_RUN <name> <reason>`.

## Actor Backend (`HYPRIAL_ACCEPTANCE_ACTOR_BACKEND`)

- `reference-abi` (default): used on PR CI (`gate-gui.yml` `gui-acceptance` job) where the Rust native actor engine is not built.
- `native`: built and verified by the nightly `schedule` run in `.forgejo/workflows/gate-gui.yml`. Override locally with `HYPRIAL_ACCEPTANCE_ACTOR_BACKEND=native`.
