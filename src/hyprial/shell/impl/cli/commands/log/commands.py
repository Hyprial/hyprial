"""``hyprial query/log/trajectory`` log readers."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import PRE_TRAJECTORY_ARCHIVE
from collections.abc import Sequence
from datetime import UTC, datetime
from hyprial.kernel import ipc_errors
import json
from hyprial.kernel import parse_agent_uri
import re
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _local_operator_identity
RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _parse_boundary(flag: str, value: str | None) -> datetime | None:
    services = get_services()
    if value is None:
        return None
    if not RFC3339.fullmatch(value):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"{flag} requires an RFC 3339 timestamp with a timezone",
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, f"{flag} requires a valid RFC 3339 timestamp"
        ) from error


def _entry_timestamp(entry: JsonObject) -> datetime | None:
    raw = entry.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_log_entry(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _entry_timestamp(value) is not None
        and value.get("level") in {"debug", "info", "warn", "error"}
        and isinstance(value.get("component"), str)
        and isinstance(value.get("event"), str)
    )


def _read_log_history() -> tuple[list[JsonObject], int]:
    services = get_services()
    entries: list[JsonObject] = []
    skipped = 0
    logs_dir = services._state_dir() / "logs"
    files = sorted(logs_dir.rglob("*")) if logs_dir.is_dir() else []
    for path in files:
        if logs_dir / PRE_TRAJECTORY_ARCHIVE in path.parents:
            continue
        if not path.is_file() or re.search(r"\.jsonl(?:\.\d+)?$", path.name) is None:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            skipped += 1
            continue
        for line in lines:
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not _is_log_entry(entry):
                skipped += 1
                continue
            entries.append(entry)
    entries.sort(
        key=lambda entry: _entry_timestamp(entry) or datetime.min.replace(tzinfo=UTC)
    )
    return entries, skipped


def _log_result(
    *,
    component: str | None,
    name: str | None,
    level: str | None,
    actor: str | None,
    conversation: str | None,
    since: str | None,
    until: str | None,
    correlation_id: str | None,
) -> JsonObject:
    services = get_services()
    if level is not None and level not in {"debug", "info", "warn", "error"}:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, "--level must be debug, info, warn, or error"
        )
    since_at = _parse_boundary("--since", since)
    until_at = _parse_boundary("--until", until)
    if since_at is not None and until_at is not None and since_at > until_at:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, "--since must not be later than --until"
        )

    history, skipped = _read_log_history()
    entries: list[JsonObject] = []
    for entry in history:
        entry_at = _entry_timestamp(entry)
        assert entry_at is not None
        actor_matches = (
            actor is None
            or any(
                entry.get(field) == actor
                for field in ("sender", "target", "recipient", "actorId", "actor")
            )
            or entry.get("route") == f"delivered:{actor}"
        )
        if (
            (component is None or entry.get("component") == component)
            and (name is None or entry.get("name") == name)
            and (level is None or entry.get("level") == level)
            and actor_matches
            and (conversation is None or entry.get("conversationId") == conversation)
            and (correlation_id is None or entry.get("correlationId") == correlation_id)
            and (since_at is None or entry_at >= since_at)
            and (until_at is None or entry_at <= until_at)
        ):
            entries.append(entry)
    return {"ok": True, "entries": entries, "skippedLines": skipped}


_AGENT_TIMELINE_EVENTS = frozenset(
    {
        "agent.harness.handover",
        "agent.binding.superseded",
        "worker.started",
        "worker.ready",
        "worker.exited",
        "worker.stopped",
        "session.registered",
        "session.unregistered",
    }
)


_TRAJECTORY_LOG_EVENTS = frozenset(
    {
        "adapter.inbound",
        "send.received",
        "send.target_unresolved",
        "wake.signalled",
        "wake.busy",
        "wake.offline",
        "wake.failed",
        "worker.turn.started",
        "worker.turn.completed",
        "worker.turn.failed",
        "worker.turn.interrupted",
        "outbox.pruned",
        "inbox.pruned",
    }
)


def _is_trajectory_log_entry(entry: JsonObject) -> bool:
    """Validate the minimum persisted message-path contract before projection.

    ``_is_log_entry`` intentionally validates only the shared logger envelope
    so ``hyprial log`` can inspect heterogeneous component history.  This stricter
    check is data-corruption defense for trajectory projection, not a legacy
    format adapter.
    """

    event = entry.get("event")
    if event not in _TRAJECTORY_LOG_EVENTS:
        return False
    required = ("name", "messageId", "correlationId", "node")
    if not all(
        isinstance(entry.get(field), str) and entry[field] for field in required
    ):
        return False
    return entry["messageId"] == entry["correlationId"]


def _trajectory_ordering() -> JsonObject:
    return {
        "key": "ts",
        "authoritative": False,
        "detail": "cross-node timestamps are display order only",
    }


def _trajectory_event_state(
    entry: JsonObject, trajectory_entries: Sequence[JsonObject]
) -> str:
    event = str(entry["event"])
    if event == "worker.turn.started":
        terminal = {
            "worker.turn.completed",
            "worker.turn.failed",
            "worker.turn.interrupted",
        }
        entry_at = _entry_timestamp(entry)
        for candidate in trajectory_entries:
            if candidate.get("event") not in terminal:
                continue
            if candidate.get("name") != entry.get("name"):
                continue
            candidate_at = _entry_timestamp(candidate)
            if entry_at is None or candidate_at is None or candidate_at >= entry_at:
                return "completed"
        return "running"
    return "completed"


def _trajectory_node(
    entry: JsonObject, trajectory_entries: Sequence[JsonObject]
) -> JsonObject:
    event = str(entry["event"])
    return {
        "ts": entry["ts"],
        "node": entry["node"],
        "component": entry["component"],
        "name": entry["name"],
        "event": event,
        "state": _trajectory_event_state(entry, trajectory_entries),
        "messageId": entry["messageId"],
        "correlationId": entry["correlationId"],
        "source": "log",
        "details": {
            key: value
            for key, value in entry.items()
            if key
            not in {
                "ts",
                "level",
                "component",
                "name",
                "event",
                "messageId",
                "correlationId",
                "node",
            }
        },
    }


def _status_timestamp(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("recordedAtMs")
    if not isinstance(raw, int):
        return None
    return (
        datetime.fromtimestamp(raw / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _trajectory_result(message_id: str) -> JsonObject:
    services = get_services()
    history, skipped = _read_log_history()
    trajectory_entries: list[JsonObject] = []
    for entry in history:
        if entry.get("event") not in _TRAJECTORY_LOG_EVENTS:
            continue
        if not _is_trajectory_log_entry(entry):
            skipped += 1
            continue
        if entry.get("correlationId") == message_id:
            trajectory_entries.append(entry)
    base: JsonObject = {
        "schemaVersion": 1,
        "ok": True,
        "found": bool(trajectory_entries),
        "messageId": message_id,
        "nodes": [],
        "agentTimeline": [],
        "skippedLines": skipped,
        "ordering": _trajectory_ordering(),
    }
    if not trajectory_entries:
        base["message"] = "本机日志无此消息"
        return base

    actors = {
        value
        for entry in trajectory_entries
        for field in ("sender", "target", "recipient", "actorId", "actor")
        if isinstance((value := entry.get(field)), str) and value
    }
    sender = next(
        (
            value
            for entry in trajectory_entries
            if isinstance((value := entry.get("sender")), str) and value
        ),
        None,
    )
    nodes = [
        _trajectory_node(entry, trajectory_entries) for entry in trajectory_entries
    ]

    status: JsonObject | None = None
    status_error: str | None = None
    if sender is None:
        status_error = "sender unavailable in local trajectory events"
    else:
        try:
            # Only an agent claim needs the operator form (it has no session
            # here); channel, adapter and user senders keep their own path.
            status_params: JsonObject = (
                {"from": _local_operator_identity(), "onBehalfOf": sender}
                if parse_agent_uri(sender) is not None
                else {"from": sender}
            )
            value = services._daemon_request(
                "message.status",
                {**status_params, "messageId": message_id},
                restore_wait=0.0,
            )
            if isinstance(value, dict):
                status = value
            else:
                status_error = "message.status returned a non-object"
        except (
            services.CliError,
            ConnectionError,
            OSError,
            RuntimeError,
            TimeoutError,
        ) as error:
            status_error = str(error)

    if status is not None and status.get("messageId") not in {None, message_id}:
        status_error = (
            "message.status returned a different messageId: "
            f"{status.get('messageId')!r}"
        )
        status = None
    status_value = status.get("state") if status is not None else "unknown"
    if status_value not in {
        "fetched",
        "expired",
        "pending",
        "unknown",
    }:
        status_error = f"unsupported message.status state: {status_value!r}"
        status_value = "unknown"
    status_state = (
        "completed"
        if status_value in {"fetched", "expired"}
        else "running"
        if status_value == "pending"
        else "unknown"
    )
    raw_records = status.get("records", []) if status is not None else []
    records = raw_records if isinstance(raw_records, list) else []
    selected_record = next(
        (
            record
            for record in records
            if isinstance(record, dict) and record.get("messageId") == message_id
        ),
        None,
    )
    if records and selected_record is None:
        status_error = "message.status records did not contain the requested message"
        status_value = "unknown"
        status_state = "unknown"
    nodes.append(
        {
            "ts": _status_timestamp(selected_record),
            "node": "delivery-terminal",
            "component": "message-status",
            "name": (
                selected_record.get("holder", "query")
                if isinstance(selected_record, dict)
                else "query"
            ),
            "event": f"message.status.{status_value}",
            "state": status_state,
            "messageId": message_id,
            "correlationId": message_id,
            "source": "message-status",
            "details": {
                **({"record": selected_record} if selected_record is not None else {}),
                **(
                    {"diagnostics": status.get("diagnostics")}
                    if status is not None and "diagnostics" in status
                    else {}
                ),
                **({"error": status_error} if status_error is not None else {}),
            },
        }
    )
    nodes.sort(
        key=lambda node: (
            node["ts"] is None,
            _entry_timestamp({"ts": node["ts"]})
            if node["ts"] is not None
            else datetime.max.replace(tzinfo=UTC),
            str(node["event"]),
        )
    )
    base["nodes"] = nodes

    dated_nodes = [
        parsed
        for node in nodes
        if isinstance(node.get("ts"), str)
        and (parsed := _entry_timestamp({"ts": node["ts"]})) is not None
    ]
    if dated_nodes:
        started_at = min(dated_nodes)
        ended_at = datetime.now(UTC) if status_state == "running" else max(dated_nodes)
        from hyprial.kernel import short_actor_name

        actor_names = {short_actor_name(actor) for actor in actors}
        base["agentTimeline"] = [
            entry
            for entry in history
            if entry.get("event") in _AGENT_TIMELINE_EVENTS
            and (entry_at := _entry_timestamp(entry)) is not None
            and started_at <= entry_at <= ended_at
            and (
                any(entry.get(field) in actors for field in ("actor", "actorId"))
                or entry.get("name") in actor_names
            )
        ]
    return base


def _format_trajectory(result: JsonObject) -> str:
    skipped = result.get("skippedLines", 0)
    skipped = skipped if isinstance(skipped, int) and skipped >= 0 else 0
    if result.get("found") is not True:
        lines = [f"本机日志无此消息：{result.get('messageId', 'unknown')}"]
        lines.append(f"skipped {skipped} malformed lines")
        return "\n".join(lines)
    lines = [f"Trajectory {result.get('messageId', 'unknown')}"]
    nodes = result.get("nodes", [])
    if not isinstance(nodes, list):
        nodes = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        timestamp = node.get("ts") or "—"
        lines.append(
            f"{timestamp} [{node.get('state', 'unknown')}] "
            f"{node.get('node', 'unknown')} {node.get('event', 'unknown')} "
            f"({node.get('component', 'unknown')})"
        )
    timeline = result.get("agentTimeline", [])
    if isinstance(timeline, list) and timeline:
        lines.append("Agent timeline:")
        for event in timeline:
            if not isinstance(event, dict):
                continue
            lines.append(
                f"{event.get('ts', '—')} [annotation] "
                f"{event.get('event', 'unknown')} ({event.get('component', 'unknown')})"
            )
    lines.append("Note: cross-node timestamps are display order only.")
    lines.append(f"skipped {skipped} malformed lines")
    return "\n".join(lines)


@app.command("query")
def query_command(
    actor: str = typer.Argument(..., help="Local actor name or agent URI."),
    view: str = typer.Argument(..., help="inbox or outbox."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read one local actor's inbox or outbox, changing nothing.

    ``inbox`` lists the messages and system notices still waiting for the
    actor, with their text; ``outbox`` lists what the actor sent that is still
    queued for delivery.  Read-only: nothing is fetched, acknowledged or
    drained, so the agent still receives exactly what it would have.
    """
    services = get_services()

    if view not in {"inbox", "outbox"}:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, "view must be inbox or outbox")

    def operation() -> Any:
        services = get_services()
        return services._daemon_request("message.query", {"from": actor, "view": view})

    services._execute(operation, json_output=json_output)


@app.command("log")
def log_command(
    component: str | None = typer.Option(
        None, "--component", help="Exact component scope (for example daemon)."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Exact daemon, adapter, or worker name scope."
    ),
    level: str | None = typer.Option(
        None, "--level", help="Exact level: debug, info, warn, or error."
    ),
    actor: str | None = typer.Option(None, "--actor"),
    conversation: str | None = typer.Option(None, "--conversation"),
    since: str | None = typer.Option(None, "--since"),
    until: str | None = typer.Option(None, "--until"),
    correlation_id: str | None = typer.Option(None, "--correlation-id"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Query all structured daemon, adapter, and worker JSONL history."""
    services = get_services()

    services._execute(
        lambda: _log_result(
            component=component,
            name=name,
            level=level,
            actor=actor,
            conversation=conversation,
            since=since,
            until=until,
            correlation_id=correlation_id,
        ),
        json_output=json_output,
    )


@app.command("trajectory")
def trajectory_command(
    message_id: str = typer.Argument(..., help="Message ID to trace."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Merge local structured events with the pullable message status."""
    services = get_services()

    def operation() -> Any:
        return CliResult(_trajectory_result(message_id), render=_format_trajectory)

    services._execute(operation, json_output=json_output)
