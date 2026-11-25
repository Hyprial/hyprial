# E2E scenario inventory

Phase 0 documents the executable contracts; automation arrives with the owning
implementation phases. `scenarios.json` is the machine-readable inventory and
the per-scenario Markdown files contain prerequisites, ordered steps, and
assertions.

`scenarios.json` is the machine-readable inventory. All twenty scenarios
have standalone runners;
E2E-001's runner builds a real Docker two-container mesh
(cross-network TCP semantics) and covers discovery, roundtrip, offline
queueing, and restart self-heal. E2E-004, E2E-006, E2E-008,
E2E-012, and E2E-013 are executable but intentionally require real provider
environments.
E2E-009 proves that a clean first run (`hyprial init`, a repeated init and a
daemon restart) creates no agent, no adapter, no route and no pin, and that
every retired `hyprial init onboarding` command and old step id exits 2. It
needs no credential, so it runs in the credential-free isolated gate
(`e2e-macos-isolated.yml`) and never depends on the controlled gate's
preflight.
E2E-011 Lark App onboarding and one-click scope authorization
automate everything up to the verification URL being presented; the
device-authorization approval behind that URL is a human action the platform
cannot delegate. E2E-013 asserts the Lark reply bridge (PR #110) in full —
native reply plus the `fetched`/`ACK_RECEIVED` sender-side terminal
record; its `--skip-reply-bridge` flag is only meaningful on checkouts
predating #110. Its Lark leg also makes the pinned agent proactively run
`hyprial message send --image` and `--file`, then verifies both user-side downloads are
byte-identical; attachment-bearing `harness_reply` is pinned as a loud
unsupported boundary. E2E-013 additionally asserts config *effectiveness*, not
just config persistence: OS-level worker cwd (lsof on the daemon-descendant
PID), behavioral skill load through pi `--skill` (a CFGPROBE roundtrip must
echo a nonce token that exists only in the runner-generated skill file), and
headless busy FIFO on pi and codex (E2E-008's cc TUI counterpart).

| Scenario | Phase that automates it | Current status | Real environment |
|---|---:|---|---|
| Two-machine send/reply/ack | P1 | executable (Docker containers) | required for attended run |
| Daemon restart survival | P2 | executable | not required |
| Pin restart survival | P3 | executable | not required |
| Feishu user/adapter roundtrip | P3 | executable | required |
| uv install/upgrade/uninstall | T2 | executable | not required |
| Resident provider launch/reply/recovery | Switch gate | executable | required |
| Four CLI-added Lark adapters online/recovery | Switch gate | executable | not required |
| CC TUI worker layer-2 smoke (non-gating); full attended flow is forensics | P3 | executable | Claude login |
| Clean first run creates no agent (E2E-009) | P4-1; isolated gate | executable | not required |
| Upgrade a running deployment (E2E-010) | T2 | executable | not required |
| Lark App onboarding and one-click scope authorization (E2E-011) | P4-1 | executable to the verification URL; approval documented | required for real App creation |
| Codex app-server worker launch/roundtrip/recovery (E2E-012) | Switch gate | executable | Codex login |
| All-harness lifecycle + mid-life harness/model switch (E2E-013) | Switch gate | executable | Claude, Pi, and Codex logins; Lark leg needs the dedicated test App |
| Workflow v2: Allen graph + flag reactor + read-only projection (E2E-018) | Workflow v2 isolated graph | executable | not required |
| orgfs forwarding redial remains live (E2E-021) | Nightly isolated loopback | executable | not required |
| forwarding-driven zid re-learn, direct and spliced TCP (E2E-024) | Nightly loopback-only | executable | not required |
| Group conversations on one node (E2E-031) | Isolated gate; mock-model-harness-e2e `hosted` job with `--harness pi` (GC-11) | executable | not required |
| Switch the viewed org in `hyprial gui` with a real browser (E2E-032) | Stage F row 4; gate-gui.yml (gui-acceptance) | executable; BLOCKED without Node, `gui/node_modules` or a browser | not required |

Retired scenario: E2E-014 (DSH API worker launch, roundtrip, interrupt, and
recover) retired when the experimental `dsh` and `dsh_tui` harnesses were
removed; its runner, helper package, document, controlled-gate step, and
inventory entry were removed together.

Retired scenario: E2E-022 (public install + self-hosted tailnet) retired
with the tsnet sidecar (Tailcat cutover, 2026-10). The scenario depended on
Headscale pre-auth join (`tag:e2e` key) and the pin-verified public tsnet
binary release; both are gone. The Tailcat sidecar now ships inside the
hyprial wheel and org networking uses `org create/invite/join`, so the
scenario's join path has no live replacement and its runner, node helper,
document, controlled-gate step, and inventory entry were removed together.

Retired runner: `run-pac-wave2.py`, removed with the E2E-018 workflow v2 port
(2026-09-25). It was an unreferenced real-provider release probe from the
2026-09-08 PAC wave-2 migration. It could not run after ccd1ddc6 retired the
direct PAC CLI, which it still called nine times. Its surfaces have no E2E
replacement:
- preflight isolation;
- A.5 launch failure durable and unflagged, plus sibling-graph isolation;
- A.5 actor up plus assignment;
- A.5 restart reconciled exactly once;
- D real v2 dispatch;
- F.5 routine coordinator restore;
- F.5 journal wake;
- F.5 archive persistence across a fresh daemon generation.

Unit-level coverage of those behaviours has not been checked. The run's
evidence remains in `docs/notes/evidence/pac-wave2-isolated-e2e.log`.

The first attended inbound-only evidence is retained in
[`feishu-live-roundtrip-evidence.md`](feishu-live-roundtrip-evidence.md); the
executable E2E-004 runner is the authoritative roundtrip gate.

Validate documentation completeness:

```sh
uv run python contract/e2e/validate.py
```

Run the executable daemon, Feishu, and installation flows:

```sh
# E2E-001: Docker two-container mesh (offline queueing + restart self-heal)
uv run python contract/e2e/run-two-machine-roundtrip.py
# fallback without Docker: single-host dual-home loopback sim
uv run python contract/e2e/run-two-machine-roundtrip.py --local
# Supporting defect #39 gate: two daemons, half-open TCP blackhole, reconnect
uv run python contract/e2e/run-zenoh-half-open-reconnect.py
uv run python contract/e2e/run-daemon-restart-survival.py
uv run python contract/e2e/run-pin-restart-survival.py
uv run python contract/e2e/run-feishu-roundtrip.py
uv run python contract/e2e/run-uv-install-flow.py
uv run python contract/e2e/run-resident-providers.py
uv run python contract/e2e/run-four-lark-adapters.py
# E2E-008 layer-2 smoke (what the controlled gate runs; non-gating):
uv run python contract/e2e/run-cc-tui-worker.py --attended --smoke
# E2E-008 full attended deep flow (manual forensics, not a release gate):
uv run python contract/e2e/run-cc-tui-worker.py --attended
# E2E-009: clean first run creates no agent, adapter, route or pin
uv run python contract/e2e/run-first-run-no-agent.py
# E2E-010: running-deployment upgrade continuity
uv run python contract/e2e/run-upgrade-continuity.py
# E2E-011: Lark App onboarding + one-click scope authorization, to the human handoff
uv run python contract/e2e/run-lark-app-onboarding.py
# E2E-012: dedicated Codex app-server worker gate (DeepSeek Responses API)
uv run python contract/e2e/run-codex-worker.py
# E2E-013: all-harness lifecycle + A9 harness switch + model switch + Lark
# agent image/file outbound + reply bridge + teardown persistence
# (real Claude/Pi/Codex, all on deepseek-flash, + test App)
uv run python contract/e2e/run-all-harness-lifecycle.py
# E2E-021: two loopback daemons, active orgfs, real forwarding redial
uv run python contract/e2e/run-orgfs-forwarding-redial.py
# E2E-024: forwarding-driven zid re-learn over lossy loopback TCP modes
uv run python contract/e2e/run-peer-restart-relearn.py
# E2E-031: group conversations on one isolated daemon (add --harness pi for the
# real headless Pi + mock-model turn)
uv run python contract/e2e/run-group-conversation.py
# E2E-032: isolated daemon on a GUI-ready home, `hyprial gui` and a headless
# browser switching the viewed org (needs `npm --prefix gui ci` and a browser
# that gui/tests/support/browser.mjs finds; BLOCKED otherwise)
uv run python contract/e2e/run-org-switch-browser.py
```

The daemon runners use the real `hyprial init`, IPC-backed commands, and graceful
`hyprial daemon stop`. The Feishu runner uses production adapter seams with a real
user and dedicated test App. Every test-owned `HYPRIAL_HOME`,
`HARNESS_STATE_DIR`, socket, database, correlation store, and Zenoh endpoint is
disposable. The E2E-001 runner additionally guarantees Docker cleanup: all
resources use the fixed `hyprial-e2e001-*` prefix plus the `hyprial-e2e001=1` label,
a pre-clean removes leftovers before each run, cleanup runs on success,
failure, exception, and Ctrl-C, and a post-run self-check fails loudly if
any container, network, or volume under the prefix survives (manual
fallback: `docker rm -f $(docker ps -aq --filter name=hyprial-e2e001)`). The
`e2e-macos-isolated.yml` workflow adds a CI-level `if: always()` sweep for the one path the
runner cannot cover itself (SIGKILL / job timeout), and fails the run if
anything survives the sweep.

## PR policy while the isolated gate is degraded (spec-e2e001, 2026-09-14)

Until the `e2e-macos-isolated.yml` isolated-macos gate demonstrably produces per-scenario
readings again, every PR that changes `contract/e2e/*` MUST attach
readings from running the corresponding runner(s) standalone on the macOS
host, explicitly labeled as **替代性、宿主类证据** (substitute, host-side
evidence — NOT CI evidence): the command line used, the scenario's RESULT
lines verbatim, and the zero-residue cleanup reading for runners that touch
Docker. A green CI run of `gate-python.yml` does not substitute for this, and
neither does an isolated-macos run that stopped at E2E-001.

The E2E-001 image is reused only while its `hyprial.e2e001.inputs` label
matches a hash of the Dockerfile's build inputs (`_image_inputs.py`); a
mismatch, a dirty build input, or `--rebuild` rebuilds it, and only the
newest two labeled images are kept on the host.

## When a scenario goes red, read the harness's own session log first

A scenario can only report what hyprial can see, and hyprial sees a worker through
delivery state: sent, not answered. Why it did not answer is frequently
recorded nowhere on this side and plainly on the other. E2E-006 is the worked
example -- from hyprial the symptom was "codex never replied"; codex's own log
said:

```
harness_read  -> Err: "MCP tool call requires approval, but approval policy is never"
                 (codex then went around it: sqlite3 straight into inbox.sqlite3
                  to find the messageId)
harness_reply -> Err: same
"approval_policy": "never"
```

No observation surface on the hyprial side carries that sentence. So the first
move on a red is not to re-read the runner output more carefully -- it is to
go get an observation with more dimensions in it.

Locations, all four checked on the macOS host on 2026-08-27 by listing the
directory and confirming files from that day. Two of them are traps rather
than lookups, marked below.

| harness | where its session log lives |
| --- | --- |
| codex | `$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl` (default `~/.codex`) |
| claude | `$CLAUDE_CONFIG_DIR/projects/<escaped-cwd>/<session-uuid>.jsonl` (default `~/.claude`) |
| pi | `~/my-pi-setup/agent/sessions/--<escaped-cwd>--/<ts>_<uuid>.jsonl` |

⚠️ **Claude's directory follows `CLAUDE_CONFIG_DIR`, and the controlled gate
moves it.** `e2e-macos-credentialed.yml` pins it to `~/.claude-ci` and refuses to run
otherwise, so a CI-run E2E-006/E2E-008/E2E-013 writes to
`~/.claude-ci/projects/...`. Looking in `~/.claude/projects/...` finds the
developer's own sessions instead: not empty, not relevant, and not obviously
either. `$CODEX_HOME` moves the same way for the codex runners.

`~/.pi` is a symlink to `~/my-pi-setup`, so those are one location and not two
places to check.

Nothing here is a substitute for the scenario's own report. It answers a
different question: the report says which assertion failed, the harness log
says what the harness was doing at the time.

## Docker resource lifecycle rule

**谁创建，谁销毁——异常路径也要销毁。** Any script, runner, test, or
workflow step in this repository that creates a Docker resource (image,
container, volume, network) must remove that resource on EVERY exit path,
and must be able to prove it did. In practice:

- Containers: prefer `docker run --rm`. When the container must outlive the
  command (detached daemons whose logs are captured on failure), remove it
  in a `finally` / trap, never as a linear tail step.
- Compose: one-off projects run `compose down -v`; keeping a volume requires
  an in-file comment naming what it preserves and who deletes it.
- Networks: removed with the project (`compose down` or `docker network rm`
  in the same cleanup); never leave an orphan bridge behind — subnet pools
  are finite.
- Images: CI-built one-off images are deleted at the end of the job via an
  `if: always()` step. Reused cache images keep a fixed tag so rebuilds
  overwrite in place, and dangling labeled layers are pruned after each
  build.
- Discovery: resources get a fixed name prefix and/or label so a pre-clean
  can reap leftovers from a previous abnormal exit, and a post-run
  self-check can fail loudly when anything survived.
- Sweep: CI jobs that build or run containers end with an `if: always()`
  sweep + verify step, because a SIGKILLed process cannot run its own
  finally.

`tests/integration/e2e/e2e001/test_e2e001_docker_lifecycle.py` guards this rule against the real
Docker daemon for the E2E-001 runner; the full inventory of Docker-creating
spots lives in `docs/notes/docker-resource-lifecycle-audit.md`.

The distribution runners use an isolated Git configuration to redirect the
current canonical source to a disposable local bare remote. They publish
synthetic version tags and prove tag-only install/upgrade/uninstall without
reading or writing either real repository.

Execution reports must classify each scenario `PASS`, `FAIL`, or `BLOCKED`.
Missing dedicated hosts, App credentials, scopes, accounts, or chats are
`BLOCKED`; they are not evidence of behavioral equivalence.

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

硬切后 agent→agent 首发要求发送方在同一可写组织空间内，否则以
`LINK_SELF_FULL`/`org-unavailable` 永久 hold。跑者前置：单节点在发送前
`hyprial org create group-<场景>-<nonce> --json` 并轮询 `link list` 的
`orgAvailable`；跨节点由一方 `fs invite <space> user:<owner> --no-notify`
后另一方 `fs join <space>`，双方都等 `orgAvailable=true`；首发后轮询
`outbox list` 直到 held 行消失（上限写死）。若干跑者（01 Docker A/B、
resident、codex-worker、hosted-start、hosted-upgrade）已补该前置；未改造
的跑者按 UNKNOWN 记录，不报通过。hosted suite 还执行 AT02 capability-ledger 与 AT03 hosted-start。
逐项证据见
`docs/evidence/org-structure-constrain-2026-10-14/wp8/r2/`。
