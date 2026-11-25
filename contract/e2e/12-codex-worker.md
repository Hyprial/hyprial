# E2E-012: Codex app-server worker launch, roundtrip, and recover

Status: executable via `run-codex-worker.py` with a DeepSeek API key.

E2E-006 exercises Codex as one of three residents through the shared
capability-table branch; this scenario is the dedicated Codex gate: it pins
the managed app-server path (`hyprial agent start codex --headless`), the exact desired
state it persists, the daemon-mediated roundtrip, and the recovery semantics
after a daemon restart.  The app-server thread identity assertion reads the
capability table: native `SESSION_LIFECYCLE` means the daemon persists the
thread id to desired state and resumes that same thread; if the capability is
ever degraded again, the scenario retains the older fresh-thread assertion.

## Preconditions

- [REAL ENV] The real `codex` executable is on `PATH` (override the binary
  with `HARNESS_CODEX_BIN`).
- [REAL ENV] `deepseek-flash` is available through `DEEPSEEK_API_KEY` or
  the local Pi `deepseek` API-key entry.
- [REAL ENV] Codex writes thread rollouts under `$CODEX_HOME/sessions`
  (default `~/.codex/sessions`). The runner only reads this directory to
  observe thread identity; it never writes into the real Codex home.
- The runner owns disposable `HYPRIAL_HOME` and `HARNESS_STATE_DIR` roots under a
  short temp path and never uses the production daemon socket; the inherited
  environment's Zenoh endpoints are stripped by the shared isolated-daemon
  helper.

## Steps

1. Preflight: verify the Codex binary and DeepSeek credential; start a real Python
   daemon in a temporary state root.
2. `hyprial agent start codex --name e2e-codex-worker --nickname e2e-codex
   --cwd <temp> --headless --provider deepseek --model deepseek-flash
   --json -- -a never -s
   workspace-write`. The managed headless default is the app-server path;
   `-a never` is deliberate because the managed worker has no WorkerChannel
   wiring, so an approval request could never be forwarded and would only
   fail closed.
3. Assert the connector reports `running=true`, exactly one
   `codex app-server --stdio` child is a live descendant of the daemon PID,
   and the schema-v1 desired state records the spec exactly (provider, name,
   nickname, cwd, headless, ownership, args).
4. Send a Harness message carrying a unique nonce through `hyprial message send`, wait
   for the correlated reply in the same conversation, and acknowledge it.
5. [REAL ENV] Locate the thread rollout containing the nonce under the Codex
   sessions directory and record the thread ID from its filename.
6. Restart the daemon (`hyprial daemon stop` + `hyprial init`) and assert the desired
   Codex worker recovers to `running=true`.
7. Assert the recovered app-server child is a fresh PID (a descendant of the
   new daemon PID, different from the pre-restart child).
8. Send a second nonce roundtrip and locate its rollout.  The runner reads
   the codex headless `SESSION_LIFECYCLE` capability instead of hard-coding
   the expectation: when native, desired state must converge to the
   pre-restart thread id and the post-restart rollout must resume that exact
   thread; if support is degraded/removed and no `sessionRef` is persisted,
   the post-restart thread must differ.  Child-level reconnect resuming the
   *same* thread is covered separately by `run-codex-app-server.py`.

## Assertions

- Codex provider/model, approval, and sandbox selection survive CLI-to-daemon
  persistence (`modelProvider=deepseek`, `model=deepseek-flash`, plus
  `-a never` and `-s workspace-write`).
- The app-server worker process is visible as running: connector
  `codex:e2e-codex-worker` reports `running=true` and exactly one
  `codex app-server --stdio` descendant of the daemon PID exists.
- Schema-v1 desired state is exact for provider, name, nickname, cwd,
  headless, ownership, and args.
- [REAL ENV] A Harness send produces a correlated reply in the same
  conversation, and the reply acknowledgement succeeds.
- Daemon restart restores the desired Codex worker to `running=true` from
  desired state alone.
- The restored worker is a fresh app-server child PID.
- [REAL ENV] The recovered worker completes a second correlated roundtrip
  (send, correlated reply, acknowledgement).
- [REAL ENV] Codex worker restart follows the capability-declared thread
  lifecycle: native resume means desired state persists the pre-restart
  threadId and the restarted worker resumes that exact thread.  Process
  identity stays fresh (separate assertion); a different post-restart thread
  while the old rollout is still readable means resume silently cold-started.
  If the capability is degraded/removed and no `sessionRef` is persisted, the
  older fresh-thread requirement remains.
- Every line and the final summary is classified `PASS`, `FAIL`, or
  `BLOCKED`.

## Known non-fatal symptom

Real `codex app-server` runs can log
`ERROR ... failed to refresh available models: timeout` on stderr shortly
after startup. This is a known, non-fatal symptom of the real environment:
the thread still starts and turns complete. The runner therefore never uses
stderr content or a transient `connectors[].error` string as a failure
criterion — only `running`, desired state, process identity, and the
roundtrip itself are asserted.

## Interaction with worker-liveness convergence (#106)

After a child exit the supervisor first exposes one observable stopped/error
interval and withdraws the actor registration; desired-state recovery happens
on a later maintenance pass behind a one-second restart backoff. The runner
only ever polls for convergence (`running=true` within a bounded window) and
never asserts instant recovery, so it is compatible with that semantics by
construction.

Run:

```sh
uv run python contract/e2e/run-codex-worker.py
```

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

DRIVER→codex worker 是 agent→agent 首发，硬切后需要可写组织空间；跑者已在注册 DRIVER 后调用 `_runner_shared.org.ensure_local_org(prefix="codex-worker")`（代码补齐；真实 codex 腿本轮未跑，记 UNKNOWN）。
