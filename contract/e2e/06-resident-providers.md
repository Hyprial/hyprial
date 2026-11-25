# Resident provider recovery and roundtrip

## Preconditions

- The real `pi`, `codex`, and `claude` executables are on `PATH`.
- [REAL ENV] `deepseek-flash` is available through `DEEPSEEK_API_KEY` or
  the local Pi `deepseek` API-key entry. All three harnesses use that one
  provider/model; ChatGPT and Anthropic subscription quotas are not inputs.
- [REAL ENV] A real Pi skill directory exists; override it with
  `HYPRIAL_RESIDENT_SKILL=/path/to/workspace/kanban/.claude/skills/kanban` when
  needed.
  **Mandatory when running from a git worktree**: the default locates the skill
  via `ROOT.parents[2]`, which silently resolves wrong under a worktree and
  surfaces as a misleading `BLOCKED preflight.pi-skill`.
- The runner owns disposable `HYPRIAL_HOME` and `HARNESS_STATE_DIR` roots and never uses the production daemon socket.
- Pi receives a disposable Git project containing the selected skill under
  `.pi/skills/`, with explicit `--approve` project trust. The retired `--skill`
  passthrough is not used. Agent-owned model credentials are still required;
  missing credentials are `BLOCKED`, never a successful live roundtrip.

## Steps

1. Start a real Python daemon in a temporary state root.
2. Start Pi, Codex, and Claude resident workers through `hyprial agent start`, selecting
   `--provider deepseek --model deepseek-flash` at the public Hyprial layer.
3. Assert `hyprial daemon status`, schema-v1 desired state, Harness send/reply, and reply acknowledgement for each worker.
4. Restart the daemon after each provider and assert desired-state recovery.
5. For Claude, capture the session transcript before restart; after the
   daemon restarts and recovers the worker, run a second roundtrip and
   require that the persisted `sessionRef` names the pre-restart transcript
   and that the second roundtrip appends to that same native session. A
   missing persisted ref, new transcript or missing resume target FAILS.

## Assertions

- Provider processes are running and visible through the public CLI.
- Model, skill, approval, nickname, CWD, and headless settings survive CLI-to-daemon persistence.
- Each resident whose capability table declares native headless execution
  receives an injected Harness request and produces a correlated reply
  (codex now qualifies through its managed app-server default).  A resident
  honestly declared degraded (as codex was on the PTY path) is asserted
  against that baseline instead: the send is accepted and durably queued,
  never silently dropped, and no phantom reply appears.  The runner consults
  `hyprial.harnesses.capabilities` directly, so promotions and demotions
  re-select the matching assertion automatically.
- Claude and Pi must obey the explicit `harness_reply` probe. The DeepSeek
  Codex leg accepts either that tool path or the deterministic synthesized
  final-result path: model tool choice is not a stable product invariant,
  while static/runtime tests independently lock the Codex MCP configuration.
- All desired residents recover after daemon restart.
- Claude's post-restart roundtrip resumes the same native session. The
  historical assertion ID `claude.fresh-session-id` is retained for report
  compatibility; it now requires continuity after an established session.
- Every line and the final summary is classified `PASS`, `FAIL`, or `BLOCKED`.

Run:

```sh
uv run python contract/e2e/run-resident-providers.py
```

Codex app-server has an additional real-runtime gate (including native child
SIGKILL recovery, same-thread context, turn interrupt, and fail-closed command
approval):

```sh
HYPRIAL_BIN="$(uv run --project . which hyprial)" \
  uv run python contract/e2e/run-codex-app-server.py
```

Codex child-process recovery resumes the same native thread. A full hyprial daemon
restart restores the desired worker but does not yet persist that native thread
ID, so conversation continuity across daemon restart remains degraded and is
declared that way in the capability table.

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

DRIVER→resident worker 是 agent→agent 首发，硬切后必须在同一可写组织空间内，否则请求以 `org-unavailable` 永久 hold。跑者已在注册 DRIVER 后调用 `_runner_shared.org.ensure_local_org(prefix="resident")`（代码补齐；真实 provider 腿本轮未跑，记 UNKNOWN）。
