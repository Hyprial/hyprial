# Structured logging

Every in-process component writes through `from hyprial.log import Logger`. The
library, rather than each caller, owns the JSONL schema, recursive redaction,
file permissions, routing, and thread/process append locking.

Each line has the reserved fields `ts`, `level`, `component`, `name`, and
`event`, followed by event-specific fields. `Logger` rejects attempts to
override the reserved fields and filters secret/token/authorization keys,
credential-shaped text, JWT-shaped tokens, and HTTP/WebSocket URLs recursively
before serialization. Directories are mode `0700`; files and append lock files
are mode `0600`.

## API and routes

```python
from hyprial.log import Logger

daemon = Logger.daemon(state_dir, name=node_id)
adapter = Logger.adapter(state_dir, name=adapter_name)
worker = Logger.worker(state_dir, runtime="pi", name=worker_name)

daemon.info("daemon.ready", pid=pid)
worker.error("worker.exited", pid=pid, returnCode=23, stderrTail=tail)
```

Routes are centralized in `hyprial.log.route_path`:

- daemon and daemon-owned subsystem scopes: `state/logs/daemon.jsonl`;
- every Lark adapter, distinguished by `name`: `state/logs/lark-gateway.jsonl`;
- managed workers: `state/logs/workers/<runtime>-<name>.jsonl`;
- explicitly standalone components: `state/logs/<component>.jsonl`.

Pi retains `state/logs/workers/pi-<name>.jsonl` and the existing
`worker.started`, `worker.ready`, `worker.exited`, and `worker.stopped` event
model. E2E-013 continues to use that public diagnostic seam for its killed-PID
assertion.

`hyprial log` recursively queries all of these routes. It supports exact
`--component`, `--name`, and `--level` filters in addition to actor,
conversation, correlation ID, `--since`, and `--until`; `--json` emits the
machine-readable result.

### Lark dead-letter visibility

Each newly created Lark dead letter also emits a `warn` record on the normal
adapter route with `component=adapter`, `event=dead-letter`, and only safe
references: `reason`, `message_id`, `chat_id`, `chat_type`, and
`conversation_id`. The message body, error detail, and sender data are never
passed to `Logger`; they remain in the bounded SQLite recovery record. A
duplicate upsert does not emit a second event. Consequently `hyprial log
--component adapter` covers dead letters without a special query command.

The adapter also performs lightweight backlog discovery in fixed one-hour UTC
windows. At 10 recent dead letters by default, it sends the configured operator
route one message containing only the count and reason distribution. The
threshold is configurable with `HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD` (integer
1 through 500; invalid values fall back to 10). A SQLite claim allows only one
notification per adapter/window across threads, processes, and restarts; a
failed send releases the claim so a later dead-letter write may retry. This is
visibility only: there is no replay command or dashboard in this release.

### Terminal alarm vocabulary

`AlarmEmitter` is a message-plane component that depends on `Logger`; Logger
does not depend on it and remains a pure sink. A newly persisted terminal
failure emits `terminal.failure`, followed by `alarm.raised`; an unthrottled
attempt then emits exactly one of `alarm.delivered` or `alarm.failed`. Every
record carries `correlationId` equal to the original message ID.
Same-conversation/same-reason notifications are claimed in a short persistent
window; a suppressed duplicate is marked `throttled=true`. An alarm delivery
failure ends at `alarm.failed`, keeps the throttle claim, and never raises
another alarm.

The pre-existing Lark dead-letter backlog notification now passes through the
same emitter and event vocabulary while retaining its aggregate one-hour claim;
there is no second threshold mechanism.

## Rotation and legacy files

Rotation is not enabled in this release. `Logger(rotation_hook=...)` provides
the future policy hook: it runs under the same cross-process route lock just
before the canonical append is reopened. A later rotation policy can therefore
be added without returning path ownership to callers.

The old state-root `codex-app-server.log` and `claude-<name>.log` files are no
longer write targets. Harness Bridge deliberately does not silently delete or
move them because they can contain operator-owned diagnostics and sensitive
history. During a maintenance window, the owner may archive existing files:

```text
mkdir -m 700 "$HARNESS_STATE_DIR/logs/archive"
mv "$HARNESS_STATE_DIR/codex-app-server.log" "$HARNESS_STATE_DIR/logs/archive/"
mv "$HARNESS_STATE_DIR"/claude-*.log "$HARNESS_STATE_DIR/logs/archive/"
chmod 600 "$HARNESS_STATE_DIR"/logs/archive/*.log
```

Resolve the glob and review the exact targets before moving them. Retain or
delete the archived history according to the deployment's retention policy;
the application performs neither action automatically.
