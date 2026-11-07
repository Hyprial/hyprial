# dsh-tui interactive E2E runner

`scripts/verify/dsh_tui_e2e.py` is the real-PTY acceptance runner for
`hyprial agent start dsh` (interactive dsh-tui carrier). It is not the headless
E2E-014 Web/API runner.

It needs a PTY/tmux-capable executor; a code-level fake-carrier test is not a
substitute. R&D's readings (kill, socket, kill with `--fail-first-wake`, all
`RESULT PASS`) are in `docs/evidence/dsh-tui-interactive-2026-10-19/`.

## Prerequisites

Required executables:

```text
hyprial (resolved by contract/e2e/_daemon_runner.hyprial_executable)
tmux
dsh-tui
dsh
node
```

Required isolated inputs:

- `HYPRIAL_DSH_TUI_E2E_DSH_HOME_SEED`
  - path to a throwaway `DSH_HOME` template containing
    `profiles/dsh-tui/` and its node modules.
  - must not contain production credentials, sessions or secrets. The runner
    copies it into the disposable temp root.
- a provider, one of:
  - `--mock-provider` (default choice): `contract/e2e/_messages_tool_mock.py`
    on loopback with a synthetic key. It answers a wake with
    `mcp__harness-bridge__harness_read`, the read result with
    `harness_reply` for the pending `messageId`, and logs only tool names and
    decisions to `provider-requests.jsonl`;
  - `HYPRIAL_DSH_TUI_E2E_PROVIDER_BASE_URL` + `HYPRIAL_DSH_TUI_E2E_PROVIDER_API_KEY`
    for an explicitly authorized test endpoint. The key reaches the wrapper
    only through the new tmux server's environment; it is never written to
    `run_tui.sh` or to any argv.

`contract/e2e/_fake_provider.py` is **not sufficient**: it streams text only and
omits the usage object and stop reason dsh's Messages adapter requires. A
production key must not be copied into the isolated `DSH_HOME` or this runner.

The runner itself creates:

- an isolated `IsolatedDaemon` with `HYPRIAL_NETWORK_ISOLATED=1`;
- an isolated `HOME`, `DSH_HOME`, `HYPRIAL_HOME`, `HARNESS_STATE_DIR` and work
  directory under one disposable temp root;
- a separate tmux server (`tmux -L <unique>`) so host tmux state cannot be
  reused or modified;
- a fresh sender agent and a fresh dsh-tui actor per run.

## Run

```bash
export HYPRIAL_DSH_TUI_E2E_DSH_HOME_SEED=/path/to/isolated-dsh-home
uv run python scripts/verify/dsh_tui_e2e.py --mock-provider --crash-mode kill --evidence-dir /tmp/ev-kill
uv run python scripts/verify/dsh_tui_e2e.py --mock-provider --crash-mode socket --evidence-dir /tmp/ev-socket
uv run python scripts/verify/dsh_tui_e2e.py --mock-provider --crash-mode kill --fail-first-wake --evidence-dir /tmp/ev-fail
```

Full interactive acceptance requires all three to produce `RESULT PASS`; each
uses its own isolated daemon, actor and `tmux -L` server. `--evidence-dir`
must be outside the checkout and receives pane captures (`pane-mcp.txt` is
the raw `/mcp` text), the mock's request log, the carrier worker log, the
process tree and dsh-tui's crash log, on PASS and on FAIL.

## Stages and assertions

The runner emits one `STAGE=<name> <status>` line per stage and one final:

```text
RESULT PASS dsh-tui-interactive actor=<uri> marker=<marker>
RESULT FAIL dsh-tui-interactive <error>
RESULT BLOCKED dsh-tui-interactive <missing prerequisite>
```

Exit codes: `0=PASS`, `1=FAIL`, `2=BLOCKED`.

Covered stages:

1. `preflight` — executable/tmux/PTY/seed/provider prerequisites; missing
   prerequisites are `BLOCKED`, never skipped.
2. `isolated-daemon` — disposable daemon only.
3. `sender-agent` / `session-register` — a fresh sender and a dsh-tui
   `source=dsh-channel` session.
4. `onboarding` / `mcp-tools` — the first-run guide is closed (or never
   shown) and `/mcp` lists `harness-bridge`.
5. `draft-typed` / `draft-preserved` — type a partial draft without Enter,
   send a Harness message, and assert the draft text survives the wake.
6. `wake-single-signal` — exactly one `wake.signalled` log entry for the
   original message id; repeated daemon lists/re-signals before custody are a
   failure.
7. `wake-read-reply` + `mcp-tool-called` — the sender inbox receives the
   reply marker and (mock) the model called `harness_read` and
   `harness_reply`. With `--fail-first-wake`, `wake-failure-reported` also
   requires one `wake.failed` naming `TURN_ERROR` before the retried wake.
8. `carrier-crash` + `crash-cleanup` (kill mode) — SIGKILL the
   `dsh-carrier`, then require the TUI to stop and the launch directory to be
   removed.
9. `injection-socket-removed` + `socket-loss-unregister` (socket mode) —
   remove the plugin control socket and require the carrier to unregister
   rather than remain online and unwakeable.
10. `crash-cleanup` — no test TUI/carrier/daemon state remains after either
    mode.

## Evidence format to paste back

Preserve and sanitize the following:

- exact commit SHA and `hyprial version --json`;
- exact command and crash mode;
- isolated root path shape only (never credentials or full env dump);
- dsh/dsh-tui/dsh-mcp-client/Node versions;
- provider type (`mock`/authorized test account) with no API key;
- every `STAGE=` and final `RESULT` line;
- tmux pane capture around wake, draft and crash;
- daemon log excerpt for `wake.signalled`, `message.body_read`, reply/ack;
- carrier/TUI PIDs and final launch-directory cleanup observation;
- failure tree copied by `scenario_tempdir` if the run fails.

Do not report `PASS` for a stage that was blocked, manually inferred, or run
without PTY. Do not leave a daemon, tmux server or dsh-tui process behind.
