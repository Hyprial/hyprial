# Codex interactive attach: manual review card

## Capability boundary

This attach mode is intentionally reply-only. It supports interactive attach,
daemon wake/push, and the inbox carrier's automatic reply to an inbound hyprial
message. It does **not** inject the Harness MCP tools, so the Codex model does
not have `harness_send` or `harness_reply` and cannot proactively send to an
arbitrary new target. Use the Codex headless app-server worker or Pi
interactive attach when proactive Harness sending/tool injection is required.

This card is the human follow-up to the automated attach gate. It exists to
separate a real Codex TUI input failure from a PTY/key-encoding artifact.

Run on a macOS host with Codex, tmux, and a DeepSeek API key. The probe uses a
disposable `CODEX_HOME` and a secret-safe exec wrapper; it creates only
temporary app-server, socket, tmux, and workspace state and never places the
key in argv.

## Start the window

From the `feat/tui-attach` checkout:

```sh
uv run python contract/e2e/run-tui-attach-manual.py \
  --harness codex \
  --model deepseek-flash \
  --setup-timeout 300 \
  --discovery-attempts 10 \
  --discovery-backoff 2 \
  --manual-timeout 300
```

The first line is immediate. Leave this terminal running. It prints stage
progress such as:

```text
STAGE=boot START
STAGE=framework START harness=codex
CLI_VERSION=codex codex-cli ...
STAGE=boot OK setup_timeout=300s
STAGE=preflight START
STAGE=preflight OK codex+tmux+auth-source
STAGE=codex-version OK version=codex-cli ...
STAGE=login-status START
STAGE=login-status OK
STAGE=app-server START
STAGE=app-server OK socket=...
STAGE=tui START
STAGE=tui OK tmux=codex-attach-gate-...
STAGE=rpc-initialize OK
STAGE=thread-discovery START
MANUAL_BOOTSTRAP_READY=1
MANUAL_TMUX_ATTACH=tmux attach -t codex-attach-gate-...
MANUAL_BOOTSTRAP_MARKER=MANUAL_TUI_BOOTSTRAP_...
```

At this point, in a second terminal, run the exact printed tmux attach command.
In the TUI, type exactly:

```text
Reply with exactly MANUAL_TUI_BOOTSTRAP_... and do not use tools.
```

Press the physical keyboard Enter key. The probe retries discovery and waits
for that seed marker to appear in `thread/read`. It prints
`STAGE=thread-discovery WAIT` for each retry. Only after the seed is observed
does it inject the external turn. After that turn completes, it prints:

```text
MANUAL_READY=1
MANUAL_TMUX_ATTACH=tmux attach -t codex-attach-gate-...
MANUAL_THREAD_ID=...
MANUAL_EXTERNAL_MARKER=ATTACH_GATE_EXTERNAL_...
MANUAL_FOLLOWUP_MARKER=MANUAL_TUI_FOLLOWUP_...
```

The printed attach command is also repeated in the later output for convenience:

```sh
tmux attach -t codex-attach-gate-...
```

The probe has already started an authenticated normal Codex TUI. Confirm
visually that the bootstrap turn, then the external prompt and response, are
present before continuing.

## Human action

After `MANUAL_READY=1`, in the attached TUI, type exactly:

```text
Reply with exactly MANUAL_TUI_FOLLOWUP_... and do not use tools.
```

Replace the suffix with the exact `MANUAL_FOLLOWUP_MARKER` printed by the
probe. Press the physical keyboard Enter key. Do not use `tmux send-keys` for
this decision.

Record two observations independently:

| Observation | Allen records | Probe records |
|---|---|---|
| Screen | Did the text leave the editor and become a submitted user turn? Did the assistant respond? | Not applicable; the probe must not infer this from PTY bytes. |
| Thread fact | Not applicable | `THREAD_FOLLOWUP_PASS` means the marker was found in `thread/read`, proving a submitted user message. |

The expected successful ending is:

```text
THREAD_FOLLOWUP_PASS marker observed in thread/read
RESULT PASS
```

## Decision table

| Screen observation | Thread fact | Decision |
|---|---|---|
| Submitted and assistant responds | `THREAD_FOLLOWUP_PASS` | Follow-up input works; reopen attach review. |
| Looks submitted | No thread marker before timeout | TUI display and thread state disagree; keep attach closed. |
| Does not submit | `THREAD_FOLLOWUP_PASS` | Screen/tooling observation is inconsistent; repeat with a clean physical session. |
| Does not submit | No thread marker | Reproduce the original failure; attach remains closed. |

If the five-minute window expires without the marker, the probe prints
`BLOCKED` because no human action was observed; that is not a new product
verdict.

`--setup-timeout` is the global deadline for preflight, app-server/TUI startup,
thread discovery, and the external turn. A timeout prints the current
`STAGE=... BLOCKED` line and a bounded `DIAGNOSTIC_LOG_TAIL` before cleanup.
Quota warnings are printed during `STAGE=external-turn WAIT`; confirmed
rate-limit/429/exhaustion evidence is reported as `BLOCKED` with the detected
hint instead of waiting indefinitely. A mere “less than 10% weekly limit”
warning is reported alongside the setup timeout, not mislabeled as confirmed
exhaustion. Pressing Ctrl-C runs the same cleanup path; the terminal should end
with `STAGE=cleanup OK` and no `codex-attach-gate-*` tmux session or app-server
process remaining.

The optional busy experiment is intentionally not part of this two-minute
human decision. Run it only after the normal follow-up result is recorded.

The unified entrypoint also supports all three legs:

```sh
uv run python contract/e2e/run-tui-attach-manual.py \
  --harness all \
  --model deepseek-flash
```

The Claude leg is real: it starts the branch's hyprial through `uv run`, uses a
short `/tmp` Hyprial home with ambient daemon/socket/Zenoh variables removed,
asserts `channelConfirmed`, sends one marked message, and prints a
`MANUAL_ACTION` for the TUI observation. For self-testing its isolated trust
and Channel prompts can be approved with:

```sh
uv run python contract/e2e/run-tui-attach-manual.py \
  --harness claude --attended
```

Pi remains the explicit `SKIP waiting attach-pi PR2` leg until its owner
supplies the PR2 implementation.

## Troubleshooting discovery

Two observed failure shapes are intentionally preserved:

```text
FAIL thread discovery returned no candidate: {"loadedIds":[],"listedCount":0,...}
```

This means the TUI had not created/loaded a discoverable thread yet. In
`--inject-only` mode, have the human submit the bootstrap marker first; the
probe then retries until `--manual-timeout`, with `--discovery-attempts` no
longer limiting the attended bootstrap window.

The earlier machine showed `thread/list` empty but a populated `loadedIds`,
which the probe handled through `loaded/list + thread/read fallback`. The
bootstrap path covers the stricter case where both projections are empty.

On an empty loaded thread, `thread/read` may also return:

```text
code -32601: list_turns is not supported yet
```

The manual bootstrap treats this as retryable until the first human turn is
persisted. It is not a human verdict by itself.

The output also includes:

```text
STAGE=codex-version OK version=codex-cli ...
```

Record this line with the manual result. Codex version differences can change
whether an empty TUI has a loaded thread.

## Environment comparison from the worker

This was a read-only comparison; no files under the real Codex home were
copied or modified. The real `~/.codex` contained:

```text
config.toml          present
models_cache.json    present
version.json         present
auth.json            present
```

A fresh temporary `CODEX_HOME` using only an `auth.json` symlink contained:

```text
config.toml          absent
models_cache.json    absent
version.json         absent
auth.json            present (symlink)
```

The same account's direct temporary-home `codex exec --skip-git-repo-check
--model deepseek-flash` returned `RC 0` and the expected marker, with no 401,
429, or usage-exceeded response. The TUI/app-server path separately logged:

```text
failed to refresh available models: timeout waiting for child process to exit
```

Therefore quota exhaustion is not supported by the evidence. The probe now
copies these three non-secret runtime files as ordinary files into each fresh
temporary home before startup:

```text
config.toml
models_cache.json
version.json
```

`auth.json` remains a read-only symlink and is never copied. This tests the
cache/config hypothesis without allowing the experiment to write back to the
real home. If model refresh still blocks after this change, record it as a new
app-server/TUI environment failure rather than a quota conclusion.

The cache-enabled self-run produced a historical split result: bootstrap seed
and `loaded/list + thread/read` discovery passed after the three files were
copied, but the probe waited for an event that the second app-server client
does not receive. The later `thread/read` marker poll proved the external turn
had completed; this historical observation is superseded by the corrected
gate below. Quota exhaustion was never evidenced.

### MCP configuration isolation follow-up

The next bounded diagnostic used the same disposable `CODEX_HOME`, copied
`config.toml`, `models_cache.json`, and `version.json`, and retained only the
read-only `auth.json` symlink. It ran two config variants:

1. Remove every `[mcp_servers.*]` table while retaining the Cloudflare plugin
   declaration.
2. Remove every `[mcp_servers.*]` table and the
   `[plugins."cloudflare@openai-curated"]` table.

Both variants passed Codex startup, authenticated model-ready TUI startup,
RPC initialize, and `loaded/list + thread/read` discovery. Both then produced
the same bounded external-turn result: `turn/start` returned
`status=inProgress` with `items=[]`, only `thread/status/changed` events were
observed, and the turn never completed. The app-server tail still contained:

```text
rmcp::transport::worker: worker quit with fatal: Transport channel closed,
when AuthRequired(AuthRequiredError { ... resource_metadata="https://mcp.cloudflare.com/.well-known/oauth-protected-resource/mcp" })
```

Therefore stripping the inherited MCP-server and Cloudflare-plugin config did
**not** unblock that historical probe. The Cloudflare authentication path
remains an observed correlation, not a proven cause; it may be injected by
another Codex/plugin runtime source. This config-isolation experiment remains
`BLOCKED` as a causal hypothesis, but it does not override the corrected
direct-attach gate verdict below.

The second stripped run also reported `FAIL host auth.json metadata changed`.
That is a test-boundary failure: the runner's read-only symlink contract did
not prove that `codex login status` leaves the real auth file metadata alone.
Do not run another live probe against the shared auth symlink until the auth
fixture is made read-only or otherwise isolated without copying credentials.

Operational decision: optional MCP/plugin configuration should not be
implicitly inherited by an isolated attach gate, but removing it alone is not
currently a sufficient fix for this停摆.

## Reversal: direct attach is the accepted route

The earlier external-turn and follow-up failures were probe false negatives.
The app-server does not broadcast the complete turn lifecycle to the second
client while it does expose the committed conversation through `thread/read`.
The gate now polls `thread/read` every five seconds after `turn/start` is
accepted and treats the requested external marker appearing in both the user
and assistant records as completion. Lifecycle events are supporting
observation only.

Fresh automatic gate evidence on the corrected probe:

```text
PASS normal TUI reached authenticated model-ready state
PASS second client found exactly one TUI thread via loaded/list + thread/read fallback
PASS external turn and response visible in thread/read and TUI pane
RESULT PASS
```

The synthetic `tmux send-keys` follow-up remains explicitly non-gating: it is
not a valid substitute for physical keyboard submission. Allen's attended
run supplied the authoritative observation that the follow-up was submitted
and answered normally. The `--inject-only` window now uses
`manual-timeout` with an unbounded discovery loop and periodic, deduplicated
manual instructions, so normal attach-and-type time is supported.

Decision: the direct attach route is **PASS/accepted**. The collaborative
launch/relay seam remains an archived fallback rather than the active route;
the earlier 8–12 person-day implementation estimate is reactivated for
review, not treated as a prerequisite for direct attach acceptance.
