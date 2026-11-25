# E2E-008: Claude Code TUI worker

Status: executable via `run-cc-tui-worker.py` with a real Claude Code login.

**Layering (docs/notes/tui-test-layering.md): this scenario is layer 2 — real-TUI
smoke — and is gating (promoted 2026-08-19).** The controlled gate runs it as
`--smoke`, records its exit code, and the verdict requires it to be green: the
CC >=2.1.227 channel-contract break (#94) proved this smoke catches real
regressions (it was red from the first run while three wrong attributions
waved it away), and its CI form does not depend on reply delivery. Standing
rule: if it ever goes red again, a matching production signature means an
incident gets filed — never waive it back to non-gating. The protocol
assertions that used to gate through this
scenario (registration, supersede, generation confirm, lease, ref-guarded
unregister) are layer-1 fake-carrier contract tests in
`tests/daemon/sessions/test_session_carrier_protocol_contract.py`; Channel carrier
wake/reply/ack semantics are pinned in `tests/test_mcp_channel*.py`. Human
experience items are layer 3: `docs/notes/tui-human-acceptance-checklist.md`.
The full deep flow below remains available for attended forensics.

```sh
# Layer-2 smoke (what the controlled gate runs; gating):
uv run python contract/e2e/run-cc-tui-worker.py --attended --smoke
# Full attended deep flow (manual forensics, not a release gate):
uv run python contract/e2e/run-cc-tui-worker.py --attended
```

## Smoke mode (`--smoke`, what CI runs)

Starts the real CC TUI against an isolated worker daemon, asserts registration
with `channelConfirmed=true` and a fresh session identity, stops the TUI, and
asserts the actor's session leaves (or reports offline in) `ps`. Nothing else:
no message is sent, no turn is driven, no FIFO or restart assertion. Allowed
to be red, allowed to be retried, never verdict-blocking.

## Preconditions

- `[REAL ENV]` Claude Code with Channels support plus a DeepSeek API key;
  the runner launches CC with DeepSeek's official CC environment mapping
  (pro/flash via ANTHROPIC_* variables, key from `DEEPSEEK_API_KEY` or Pi's
  auth.json — never printed or written) and selects the catalog-known
  `haiku` alias, which the mapping resolves to `deepseek-flash`; it does
  not consume Claude OAuth quota.
- `tmux` and the checkout's current `hyprial` executable.
- `[ATTENDED]` One-time workspace or local Channel consent may be required.

## Steps

The runner creates two disposable Python daemon roots and one private Zenoh
endpoint. The CC actor is deliberately distinct from the worker daemon node ID;
registration must publish its own presence and inbox route. A distinct sender
daemon sends requests and receives MCP results across the real transport.

It proves Channel-triggered idle wake without keyboard input or a Stop hook,
tool read/reply/ack, two messages delivered while the first turn is busy in
FIFO order, and re-signalling of an offline pending request after the CC process
is killed and a fresh TUI starts.

Claude may require one-time workspace trust or local Channel server consent.
Without `--attended`, that is reported as `BLOCKED`; `--attended` approves only
those startup dialogs in the test-owned tmux session. No input is injected to
trigger or advance any Harness turn — every turn starts from an inbound
Channel notification and finishes on its own via `harness_reply`/`harness_ack`.
CC runs in an empty disposable workspace, and only the test-owned Bash
sentinel command is pre-approved; global permission checks are not disabled.

The pre-approval is a `command:*` glob-prefix `--allowedTools` entry per
Bash fragment (CC splits a compound command on `;` and checks each fragment
independently), so it survives CC composing the call slightly differently
(e.g. appending a verifying `ls -la`) without a prompt. `--attended` also
recognizes CC's interactive Bash-permission dialog as a backstop in case CC
composes the call in some other unanticipated way; if that backstop fires,
the log line `[ATTENDED] approving isolated Claude Bash command in
cc-workspace prompt` names it explicitly. That approval is scoped to a
pending tool call inside a turn Channel already started — it is not new
input that starts or steers a turn.

CC's config/credentials come from the CI-dedicated login via
`CLAUDE_CONFIG_DIR` (CI job env pins `/Users/ci-runner/.claude-ci`; local
runs default to `~/.claude-ci`). The runner validates that the directory
exists, holds its own `.credentials.json`, and has completed onboarding —
it never reads credential contents and never writes or copies anything
into the directory. Copying `~/.claude/.credentials.json` into a disposable
config dir was retired: a copy duplicates the OAuth refresh token, and
whichever side refreshes first revokes every other copy (the E2E-006/
E2E-013 401 root cause). After registration the runner also asserts from
CC's own `--debug-file` that CC really used the directory (startup lines
resolve plugins/skills/settings under it; a healthy run references the
developer's `~/.claude/` prefix zero times). This still does not fully
sever the disposable TUI from the
developer's real Claude Code installation — version-lock and update-check
state under `~/.local/share/claude/versions/` is outside `CLAUDE_CONFIG_DIR`
and is still shared with the real account's background CC supervisor
daemon.

```sh
uv run python contract/e2e/run-cc-tui-worker.py --attended
```

## Assertions (full attended flow — forensics, not the release gate)

- `[REAL ENV]` The MCP Channel child registers the CC TUI actor with a fresh
  session identity and `channelConfirmed=true`.
- An inbound Channel notification starts a turn without keyboard input or a
  Stop hook, and the turn calls `harness_read`.
- `harness_reply` returns exact content to the remote sender and terminally
  acknowledges the request; a separate no-reply delivery is drained through an
  explicit `harness_ack` call.
- A test-owned filesystem sentinel proves the first turn has entered an
  eight-second Bash operation before the second delivery is sent; both replies
  then arrive in FIFO order without loss.
- The old TUI pane is stopped and CC reports its MCP child exited cleanly before
  the offline delivery; restarting overwrites any stale recovery metadata with
  a fresh fenced identity and signals that pending work.

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

agent→agent 段在硬切后需要可写组织空间（单节点建 `group-` 空间并等 orgAvailable，跨节点 invite+join）。本条为前置说明；该跑者本轮未改造（记 UNKNOWN，不报通过）。
