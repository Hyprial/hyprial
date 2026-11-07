# Harness model providers

`hyprial agent start` separates the harness runtime from the model vendor:

```sh
hyprial agent start pi --name worker --headless \
  --provider deepseek --model deepseek-flash -- --approve

hyprial agent start claude --name worker --headless \
  --provider deepseek --model deepseek-flash

hyprial agent start codex --name worker --headless \
  --provider deepseek --model deepseek-flash -- -a never -s workspace-write

hyprial agent start dsh --name worker --headless \
  --provider deepseek --model deepseek-flash
```

`start dsh` spawns and owns its own `dsh --profile web --host 127.0.0.1
--port 0` child per worker; there is no endpoint override.  The worker's real
endpoint and private `DSH_HOME` are reported by `hyprial status --json`
(`endpoint` / `dshHome`).

The schema-v1 desired-state entry keeps its historical `provider` key for the
harness (`pi`, `claude`, `codex`, or `dsh`). The model vendor and model are
stored separately as `modelProvider` and `model`. Credentials are never stored
in desired state or argv.

## Reasoning effort

Use the official `--effort` option before `--` for Codex or Claude, with either
interactive or headless launches, including `--resume`:

```sh
hyprial agent start codex --name worker --headless --provider deepseek --effort max
hyprial agent start codex --name worker --resume <thread-id> --effort max
hyprial agent start claude --name worker --headless --provider deepseek --effort low
```

| Harness | Accepted effort values | Omitted with DeepSeek or hyprial provider | Omitted with native provider |
| --- | --- | --- | --- |
| Codex | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra` | `high` (unchanged) | No injected effort; native configuration applies (unchanged). |
| Claude | `low`, `medium`, `high`, `xhigh`, `max` | `max` (unchanged) | No injected effort; native configuration applies (unchanged). |

For `deepseek` and `hyprial`, omission intentionally injects the provider default.
Codex receives `-c model_reasoning_effort="high"`, which takes precedence over
`config.toml`; use `--effort max` when that launch must use `max`. Claude receives
`CLAUDE_CODE_EFFORT_LEVEL=max`. With native OpenAI or Anthropic, omission injects
nothing, so native configuration remains authoritative.

Model/client support varies: accepting a level here does not promise that the
selected endpoint supports it. Codex's current protocol represents effort as a
nonempty string; the flag validates the known levels above. Pi, DSH, Jev and user-proxy reject
this flag.

The flag is encoded in existing launch `args`: Codex uses
`["-c", "model_reasoning_effort=\"max\""]`, Claude uses `["--effort", "low"]`.
Managed desired state retains it for restart; saved per-harness `harnessArgs`
can use the same native form. There is no new credential or environment source.
P2 launches still use only their approved complete child environment.
An interactive launch's option is session-scoped; specify it again on a later
manual `start`/`--resume` when desired.

Codex applies explicit effort to the app-server and to thread start/resume;
Claude applies it to `CLAUDE_CODE_EFFORT_LEVEL` as well as its native arguments,
so provider defaults cannot mask an explicit selection. Combining the public
flag with a native effort argument after `--` is rejected. Existing native
argument lists retain last-option-wins precedence.

References: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[Claude effort](https://code.claude.com/docs/en/model-config#adjust-effort-level).

## Session resume

`hyprial agent start --resume <session-id>` is strict: a missing native session is
an error and never becomes a fresh conversation. Interactive Codex resumes the
exact app-server thread and verifies readable history before registering it;
interactive Pi opens the exact native session and refuses create-if-missing.
Both require an existing agent and defer registry changes until a guarded
Agent-plus-Session commit succeeds.

```sh
hyprial agent start codex --name worker --resume <thread-id> --cwd /moved/project
hyprial agent start pi --name worker --resume <session-id> --cwd /project
```

## Interactive Codex launch

Native OpenAI Codex starts require an agent-owned credential before either Codex
process is launched. Seed it explicitly:

```sh
hyprial agent provider login codex --actor worker
```

A missing, empty, non-regular, or non-0600 `auth.json` returns
`CODEX_CREDENTIAL_MISSING`. Its JSON `data.reason` and remediation are specific:

- `missing`: run `hyprial agent provider login codex --actor <name>`;
- `empty`: run the same command with `--force`;
- `permissions`: run the reported `chmod 600 <path>` command;
- `not-regular`: manually replace the reported path with a non-empty 0600 regular
  file; provider login cannot replace a directory, symlink, or other special file;
- `shared-target`: restore the reported external target or change/remove
  `config.sharedCredentials.codex`; provider login only seeds the canonical agent
  credential and therefore cannot repair an external target;
- `inaccessible`: repair access manually and restore a non-empty 0600 regular file.

The agent-home authority runs this classification before runtime materialization.
After a shared target is repaired, the designated `auth.json` link is allowed by
the Codex native inventory so the next real start advances past credential setup.
Hyprial also disables Codex's startup update check by default so the update prompt
cannot block thread discovery; a later user-supplied
`-c check_for_update_on_startup=true` still overrides that default.

`hyprial agent start codex --tmux` retains a failed pane only long enough to
capture its last output and then removes the session. A successful detached pane
does **not** have a durable inbox carrier and cannot receive Harness Network
messages; the CLI reports `CODEX_TMUX_INBOX_UNAVAILABLE`. For a persistent,
message-capable TUI, open your own tmux window and run the foreground launcher
inside it:

```sh
tmux new-session -s worker
hyprial agent start codex --name worker --cwd /project
```

If startup reports `account/read failed` together with
`workspace routing discovery timed out`, check `HTTPS_PROXY` and the other proxy
variables in the runtime profile. Agent-home P2 filtering preserves approved
proxy variables; do not remove them from the daemon or worker environment.

## Interactive to headless switch

An interactive connector blocks a headless start because two connectors may
not speak for one agent. To move the same agent from an interactive session
to a headless worker without killing the CLI parent, use the explicit switch:

```sh
hyprial agent start pi --name worker --headless --resume <session-id> --replace-interactive
```

The daemon signals the interactive carrier process (PID plus birth identity),
waits for it to exit with a timeout, then starts the worker with the exact
`--resume` value. If the process does not exit, or has no PID/birth fence or an unknown
process state, the switch is refused with `INTERACTIVE_CARRIER_STOP_FAILED`,
not `STRICT_RESUME_FAILED`. Without `--replace-interactive` (and `--resume`),
the start stays refused. The CLI never signals a process itself.

| Harness | Interactive | Headless |
| --- | --- | --- |
| Claude | Supported by the native channel launcher. | Supported by the Agent SDK. |
| Pi | Supported by exact native session lookup. | Supported by the RPC worker. |
| Codex | Supported by exact `thread/resume` plus `thread/read`. | Supported by the app-server worker. |
| DSH | No interactive TUI connector. | Not supported: each launch mints a fresh worker MCP identity that cannot attach an existing DSH session. |
| Jev | No interactive runtime. | Not supported: requests are stateless and model selection is per request. |
| user-proxy | No interactive runtime. | Not supported: it is a stateless relay. |

## DeepSeek credential

Set `DEEPSEEK_API_KEY` in the daemon environment. For a local installation
that already uses Pi, HYPRIAL also reads the `deepseek.key` entry from either
`~/.pi/agent/auth.json` or `~/my-pi-setup/agent/auth.json`. The value is used
only in the child-process environment.

## Harness translations

| Harness | Translation |
| --- | --- |
| Pi | Native `--provider` and `--model` arguments. |
| DSH | `session.selectModel` after creating or resuming the worker session; public `deepseek` maps to DSH's `deepseek-official` route and the host-global default is not changed. |
| Claude | DeepSeek Anthropic endpoint and model environment (`ANTHROPIC_BASE_URL`, token, default model aliases, subagent model, and the context window below). |
| Codex | Responses custom-provider config overrides using `env_key = "DEEPSEEK_API_KEY"`; the key itself is never placed in config or argv. |

DeepSeek currently exposes only `deepseek-flash` through its Codex
Responses integration, so HYPRIAL rejects a different model for the
`codex/deepseek` combination before launch. Native Claude/Anthropic and
Codex/OpenAI behavior remains the default when `--provider` is omitted.

## Claude context window and compaction

Claude Code sizes its proactive auto-compaction from a context window it
derives from the model id. `deepseek-flash` is not an id it can resolve, so
HYPRIAL states the two numbers at launch. Without them (2026-10-17 runtime
incident, six managed workers) a session grew to ~1,017,000 input tokens plus
a 32,000-token output reservation and was rejected at the vendor's 1,048,576
limit with no compact boundary anywhere in the transcript.

| Variable | Launch value | Meaning |
| --- | --- | --- |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `1048576` | the window the model actually has |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `200000` | where auto-compaction runs, inside that window |

This applies to every Claude launch shape — the daemon-managed Agent SDK
worker (a fresh session and a resume alike), the PTY connector, and the
interactive CLI — for `--provider deepseek`. Auto-compact itself is left at
its default (enabled in current Claude Code): HYPRIAL does not set
`autoCompactEnabled`, because doing so would override a user who turned it
off.

The Claude number is the vendor's observed rejection limit (2²⁰) rather than
the round `1_000_000` in pi's registered catalog entry for the same model
(`pi_model_definitions`): the two serve different consumers and are
deliberately not unified — pi prices and sizes its own sessions, while this
value must match what the endpoint actually enforces for Claude Code.

To choose your own numbers, export either name in the daemon environment (or
in the launch environment for a CLI launch). A value that is already present
wins over the default, and a blank one counts as unset — the same rule as
`HYPRIAL_ANTHROPIC_BASE_URL` / `HYPRIAL_OPENAI_BASE_URL`. Both names are in
the child-environment vocabulary, so an exported value reaches
daemon-managed workers instead of being dropped during composition.

Only ids whose window is known are sized. `--provider anthropic` launches are
untouched, and `--provider hyprial` models are Anthropic ids routed by the
gateway, so HYPRIAL states no window for them rather than guessing one.

## Model routing proposal (not implemented)

Current `start --tier` resolves a static harness/provider/model choice at launch;
endpoint overrides do not provide a managed request router. The proposed
[`start --router` integration](../design/design-model-router-integration-2026-10-09.md)
describes agent-owned profiles, session preservation, and reuse of the existing
Jev harness with bounded, non-retrying requests. Its flags and configuration are
design proposals, not supported commands in the current release.

References:

- [DeepSeek Claude Code integration](https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/claude_code/)
- [DeepSeek Codex integration](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference/)
