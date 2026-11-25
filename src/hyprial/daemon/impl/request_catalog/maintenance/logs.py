"""Runtime method declaration and read adapter migrated from settings #2328."""

from __future__ import annotations

from hyprial.kernel import HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec, ipc_errors
from collections.abc import Mapping
from typing import Any
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
import threading
from hyprial.kernel import DaemonRequestError, LogCursorInvalid, LogPager


_COMPONENTS = ["daemon", "adapter", "worker", "other"]

_EVENTS = ["daemon.started", "daemon.stopped", "worker.exited", "dead-letter", "other"]

_REASONS = ["logs-unavailable", "discovery-limit", "unsafe-path", "unreadable-file", "invalid-record", "scan-limit"]

_ROW = {"type": "object", "properties": {
    "timestamp": {"type": "string", "format": "date-time"},
    "level": {"type": "string", "enum": ["debug", "info", "warn", "error"]},
    "component": {"type": "string", "enum": _COMPONENTS},
    "event": {"type": "string", "enum": _EVENTS},
}, "required": ["timestamp", "level", "component", "event"], "additionalProperties": False}

_RESULT = {"type": "object", "properties": {
    "entries": {"type": "array", "items": _ROW, "maxItems": 1000},
    "nextCursor": {"type": ["string", "null"], "maxLength": 256},
    "partial": {"type": "boolean"},
    "partialReasons": {"type": "array", "items": {"type": "string", "enum": _REASONS}},
    "skippedLines": {"type": "integer", "minimum": 0},
    "order": {"const": "file-offset"},
    "snapshotExpiresInSeconds": {"type": "integer", "minimum": 0, "maximum": 300},
}, "required": ["entries", "nextCursor", "partial", "partialReasons", "skippedLines", "order", "snapshotExpiresInSeconds"], "additionalProperties": False}

LOG_PAGE_SPEC = MethodSpec(
    name="log.page", owner="daemon.status",
    params_schema={"type": "object", "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 256},
    }, "additionalProperties": False},
    result_schema=_RESULT,
    errors=(MethodError(ipc_errors.INVALID_ARGUMENT), MethodError("LOG_CURSOR_INVALID"), MethodError("LOG_ACCESS_DENIED"),
            MethodError(ipc_errors.DAEMON_ERROR), MethodError(ipc_errors.DAEMON_RESTORING),
            MethodError("HTTP_NOT_READY", {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False})),
    mode="unary", mutates=False, retry="safe", audience=("local-operator",), http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")), confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

_EMPTY = {"entries": [], "nextCursor": None, "partial": False, "partialReasons": [], "skippedLines": 0, "order": "file-offset", "snapshotExpiresInSeconds": 299}

LOG_EXAMPLES = (MethodExampleSet(method="log.page", cases=(
    MethodExample(name="empty", kind="empty", reason="The existing log directory contains no records", description="No events in an existing empty logs root", params={}, result=_EMPTY),
    MethodExample(name="safe-event", kind="success", description="Only allowlisted log metadata", params={}, result={**_EMPTY, "entries": [{"timestamp": "2026-11-03T00:00:00+00:00", "level": "info", "component": "daemon", "event": "daemon.started"}]}),
    MethodExample(name="missing-logs", kind="success", description="Unavailable logs are explicitly incomplete", params={}, result={**_EMPTY, "partial": True, "partialReasons": ["logs-unavailable"]}),
    MethodExample(name="invalid-path", kind="invalid-params", description="Browser cannot choose filesystem roots", params={"path": "/tmp"}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Unexpected path parameter")),
    MethodExample(name="expired", kind="domain-error", description="Rotated or expired snapshot cannot be resumed", params={"cursor": "expired"}, error=MethodExampleError("LOG_CURSOR_INVALID", "Log snapshot expired or changed; start a new query")),
)),)


_PAGERS: OrderedDict[tuple[str, str], LogPager] = OrderedDict()


_LOCK = threading.Lock()


def log_page(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> dict[str, Any]:
    """Scope to this daemon; the HTTP host authenticates the local operator."""
    if trusted_message_origin not in (None, "ipc"):
        raise DaemonRequestError("LOG_ACCESS_DENIED", "Log summaries require a local operator")
    key = (str(Path(app.state_dir).absolute()), str(app.epoch))
    with _LOCK:
        pager = _PAGERS.get(key)
        if pager is None:
            if len(_PAGERS) >= 8:
                _PAGERS.popitem(last=False)
            pager = LogPager(Path(app.state_dir) / "logs")
            _PAGERS[key] = pager
        _PAGERS.move_to_end(key)
    try:
        page = pager.read(limit=params.get("limit", 200), cursor=params.get("cursor"))
    except LogCursorInvalid:
        raise DaemonRequestError("LOG_CURSOR_INVALID", "Log snapshot expired or changed; start a new query") from None
    rows = [{"timestamp": datetime.fromisoformat(row["ts"]).isoformat(), "level": row["level"],
             "component": row["component"] if row["component"] in _COMPONENTS else "other",
             "event": row["event"] if row["event"] in _EVENTS else "other"} for row in page["entries"]]
    return {**page, "entries": rows}


LOG_BINDINGS = ((LOG_PAGE_SPEC, log_page),)
