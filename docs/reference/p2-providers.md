# P2 provider connectors

`hyprial.harnesses.HarnessLauncher` is the concrete implementation of the
daemon-owned `HarnessLauncher` protocol (the package and symbols were renamed
from `hyprial.providers`/`Provider*` in 0.4.0.dev1; on-disk and IPC keys keep the
historical `provider` spelling). The daemon depends only on
`hyprial.daemon.api`; harness construction, injection, retrieval, and shutdown
stay in this package.

Pi and Codex children use a real POSIX PTY for stdin/stdout and a separate
stderr pipe. The explicit Claude command override retained by connector tests
uses the same legacy seam. `inject(message)` returns an opaque lookup
reference; `retrieve(reference)` reports `inProgress`, `completed`, or
`failed`. Managed shutdown sends SIGTERM to the child's process group, waits
for the bounded grace period, and then uses SIGKILL if necessary. Repeated
`stop()` calls are safe.

Managed headless Claude uses `ClaudeAgentSdkProcess` instead. One provider-owned
asyncio thread serializes daemon deliveries through a thread-safe FIFO and
holds a long-lived isolated worker process. That worker keeps one
`ClaudeSDKClient` streaming session open while idle. A later delivery calls
`query()` on that live client, and the worker consumes through `ResultMessage`.
Only a successful result lets `DaemonRuntime` submit a durable reply and ACK the
source delivery. Interrupts and error results remain non-terminal; an SDK
connection loss reconnects and retries the same queue head before later work.
Daemon restart is safe because unacknowledged messages remain authoritative in
the inbox and are redispatched to the replacement provider process.

SDK options retain the configured working directory, Claude Code system prompt
and settings sources. `--model` is mapped to the typed SDK option; other flags
use `extra_args`; caller session/resume flags are discarded because the long
lived client owns session identity.

The pinned SDK release predates MCP 2.0 and declares `mcp<2`, while the
daemon's fixed-identity Channel and `agent-channel` stdio MCP servers require
MCP 2. The launcher therefore runs the packaged worker through
`uv run --isolated --no-project --with
claude-agent-sdk==0.2.125 --python <running interpreter>`: the daemon
environment remains dependency-clean and the worker receives its supported
MCP 1.x. The interpreter is requested by absolute path (the one running
Hyprial) because a managed agent launch hands uv an isolated HOME/XDG and a
PATH that is free to offer a `python3` below the SDK's 3.10 floor; leaving the
choice to uv's discovery resolved one such launch against Python 3.9 on
2026-10-16. A desktop build that ships its own bootstrap keeps spawning it
with the running interpreter directly. JSONL over stdio carries only
query, result, interrupt, and stop control messages. A worker exit is treated
as a connection loss, so the provider reconnect loop retains and retries the
same delivery head.

## Codex model-provider credentials

Agent-home Codex uses `cli_auth_credentials_store = "file"` for OpenAI and
keeps the agent-owned or explicitly shared `auth.json`. A custom model provider
uses its process-only environment grant and an `ephemeral` credential store;
its active `CODEX_HOME` must not contain `auth.json`.

When provider-aware home preparation finds an existing `auth.json` for a custom
provider, Hyprial does not delete it. It moves the credential to the agent's
private state under
`$HYPRIAL_HOME/agents/<agent>/state/credential-backups/codex/auth.json` (with a
numeric suffix if needed), hardens the backup to mode `0600`, and removes the
active path before Codex starts. The runtime response emits the typed
`CODEX_CUSTOM_PROVIDER_AUTH_BACKUP` notice with the agent and exact backup
path. `hyprial doctor --json` also reports retained backups under the
`codex-credential-backups` finding. To restore one after changing the agent
back to provider `openai`, run `hyprial agent provider login codex --actor
<agent> --force --source <absolute-backup-path>` and restart the agent; do not
copy it into the projected native home. Retain the backup until that OpenAI
launch is verified, then move it to an approved private archive or remove it
according to the operator's credential-retention policy. Doctor continues to
report files left in the backup directory.

An immediate child failure raises `HarnessStartError`. Its public stderr tail
contains at most eight non-empty lines and 4 KiB, retains the newest output,
and redacts authorization/bearer values plus names containing API key, token,
secret, password, credential, or authorization. Errors raised before a child
exists, including an invalid `cwd`, preserve the original operating-system
reason.
