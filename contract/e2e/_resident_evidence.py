"""Bounded, schema-filtered protocol metadata; never raw provider payloads."""

from contextlib import contextmanager
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
from uuid import UUID, uuid4

from hyprial.kernel import ipc_errors

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _daemon_runner import artifacts_root

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_LIST_ITEMS = 32
MAX_RECORDS = 4096
ID_FIELDS = frozenset({"toolCallId", "messageId", "deliveryId", "conversationId", "replyMessageId", "replyTo"})
CONTAINERS = frozenset({"message", "content", "arguments", "params", "result", "data", "error", "toolCalls"})
PAYLOAD_CONTAINERS = frozenset({"arguments", "params", "result", "data", "error"})
TOOLS = frozenset({"harness_reply", "harness_read", "harness_send", "harness_ack", "bash", "exec", "read", "write", "edit", "grep", "find", "ls", "python"})
ENUMS = {
    "role": {"assistant", "user", "system", "tool", "toolResult", "function"},
    "type": {"message", "toolCall", "toolResult", "tool_call", "tool_result", "text", "session", "compaction", "custom", "bashExecution", "model_change", "thinking_level_change"},
    "runtime": {"pi", "codex", "claude", "headless", "interactive", "e2e", "e2e_interactive", "agent_sdk", "app_server", "rpc"},
    "status": {"running", "online", "offline", "busy", "idle", "completed", "failed", "success", "failure", "starting", "stopped", "ready", "error"},
    "phase": {"restoring", "serving", "reconciled", "starting", "stopped", "ready"},
    "code": {"INVALID_ARGUMENT", "ATTACHMENT_PATH_REFUSED", "HARNESS_START_FAILED", "AGENT_HOME_ACCESS_FAILED", "AGENT_NOT_FOUND", "METHOD_NOT_FOUND", "STALE_SESSION", "SESSION_SUPERSEDED", ipc_errors.DAEMON_UNAVAILABLE, "PERMISSION_DENIED", "invalid_api_key"},
}
TOKEN = re.compile(r"(?i)(?:\bsk[-_]|\bbearer\b|[a-f0-9]{32,}|[a-z0-9+/=_-]{40,})")
EVENT = re.compile(r"(?:worker|daemon|inbox|session|pi|codex|claude|harness|tool|agent|ipc|message|turn|transport|process)\.[a-z0-9_.]+")
METHOD = re.compile(r"(?:message|session|harness|agent|identity)\.[a-z_]+|(?:tools|thread|turn|config|account|skills)/(?:call|list|start|read|resume)|initialize|initialized")


def safe_scalar(key, value, payload):
    if isinstance(value, (bool, int, float)):
        return value if key in {"ok", "code", "status"} and abs(value) < 2**63 and math.isfinite(value) else None
    if not isinstance(value, str) or len(value) > 256:
        return None
    if key in ID_FIELDS:
        try:
            if str(UUID(value)) == value.lower():
                return value
        except ValueError:
            pass
        if re.fullmatch(r"[a-f0-9]{32}:\d+:[a-f0-9-]{36}", value):
            return value
        if TOKEN.search(value):
            return None
        if key == "conversationId" and re.fullmatch(r"resident-(?:pi|codex|claude)(?:-[a-z]+)*-\d+", value):
            return value
        if key == "toolCallId" and re.fullmatch(r"(?:call|toolu)_[a-zA-Z0-9_-]{1,30}", value):
            return value
        return None
    if payload or TOKEN.search(value):
        return None
    if key in ENUMS:
        return value if value in ENUMS[key] else None
    if key in {"name", "toolName"}:
        return value if value in TOOLS else None
    if key == "event":
        return value if EVENT.fullmatch(value) or value in {"tool_execution_start", "tool_execution_end", "turn_start", "turn_end", "message_update", "agent_start", "agent_end"} else None
    if key == "method":
        return value if METHOD.fullmatch(value) else None
    if key == "ts":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value
    return None


def protocol_metadata(value, depth=0, payload=False):
    if depth > 8:
        return None
    if isinstance(value, list):
        return [item for child in value[:MAX_LIST_ITEMS] if (item := protocol_metadata(child, depth + 1, payload))]
    if not isinstance(value, dict):
        return None
    result = {}
    for key, child in value.items():
        if key in CONTAINERS:
            kept = protocol_metadata(child, depth + 1, payload or key in PAYLOAD_CONTAINERS)
            if kept:
                result[key] = kept
        elif (kept := safe_scalar(key, child, payload)) is not None:
            result[key] = kept
    return result


def retain_protocol_trace(root: Path, destination: Path) -> dict:
    root = root.resolve()
    records, encoded = [], []
    used, read_bytes, truncated = 64, 0, False
    for path in sorted(root.rglob("*.jsonl")):
        relative = path.relative_to(root)
        if "secrets" in relative.parts or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        if not (relative.parts[:2] == ("state", "logs") or (relative.parts[:2] == ("home", "agents") and "state" in relative.parts)):
            continue
        with path.open("rb") as stream:
            while line := stream.readline(MAX_LINE_BYTES + 1):
                read_bytes += len(line)
                if read_bytes > MAX_INPUT_BYTES or len(records) >= MAX_RECORDS:
                    truncated = True
                    break
                if len(line) > MAX_LINE_BYTES:
                    truncated = True
                    while not line.endswith(b"\n") and read_bytes <= MAX_INPUT_BYTES:
                        line = stream.readline(MAX_LINE_BYTES + 1)
                        read_bytes += len(line)
                        if not line:
                            break
                    continue
                try:
                    kept = protocol_metadata(json.loads(line))
                except (ValueError, RecursionError):
                    continue
                if not kept:
                    continue
                record = {"source": str(relative), "record": kept}
                body = json.dumps(record, separators=(",", ":")).encode()
                if used + len(body) + 1 > MAX_OUTPUT_BYTES:
                    truncated = True
                    break
                records.append(record)
                encoded.append(body)
                used += len(body) + 1
        if read_bytes > MAX_INPUT_BYTES or len(records) >= MAX_RECORDS or truncated and used > MAX_OUTPUT_BYTES - MAX_LINE_BYTES:
            break
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b'{"records":[' + b','.join(encoded) + b'],"truncated":' + (b'true' if truncated else b'false') + b'}\n')
    return {"records": records, "truncated": truncated}


@contextmanager
def failure_evidence(root: Path, failed):
    raised = False
    try:
        yield
    except BaseException:
        raised = True
        raise
    finally:
        if raised or failed():
            try:
                destination = artifacts_root() / f"e2e006-protocol-{uuid4().hex}.json"
                document = retain_protocol_trace(root, destination)
                print(f"RESULT NOTE E2E-006 sanitized-protocol-trace={destination} records={len(document['records'])}", flush=True)
            except Exception as error:  # noqa: BLE001 - evidence must never mask the original failure
                print(f"RESULT NOTE E2E-006 trace-preservation-failed={type(error).__name__}", flush=True)


@contextmanager
def resident_root(failed):
    def terminated(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, terminated)
    try:
        with tempfile.TemporaryDirectory(prefix="hyprial-e2e-006-") as temporary:
            root = Path(temporary)
            with failure_evidence(root, failed):
                yield root
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
