"""The ``hyprial log`` structured-history query command."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from datetime import datetime
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.log.history import (
    RFC3339,
    _entry_timestamp,
    _read_log_history,
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
