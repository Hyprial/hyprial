"""The ``hyprial outbox`` delivery-observation command group."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, tables

from typing import Any
from collections.abc import Mapping
import time
import typer

outbox_app = typer.Typer(help="Inspect and prune the durable outbox.")

DASH = "\u2014"


def _now_ms() -> int:
    """The render clock; a seam so the wait column is testable."""

    return int(time.time() * 1000)


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _text(value: object) -> str:
    return value if isinstance(value, str) and value else DASH


def _wait_ms(row: Mapping[str, Any], *, now_ms: int) -> int | None:
    """The daemon's wait, else ``now - createdAtMs``; never a fabricated 0."""

    supplied = row.get("waitMs")
    if type(supplied) is int:
        return max(0, supplied)
    created = row.get("createdAtMs")
    if type(created) is not int:
        return None
    return max(0, now_ms - created)


def _state(row: Mapping[str, Any], *, now_ms: int) -> str:
    expires = row.get("expiresAtMs")
    if type(expires) is not int:
        return DASH
    return "expired" if expires <= now_ms else "queued"


def _render_outbox_list(data: Mapping[str, Any]) -> str:
    """Queue wait reason and wait duration, one row per outbox entry."""

    from hyprial.daemon import HOLD_REASON_KEY

    now_ms = _now_ms()
    cells: list[list[str]] = []
    for row in _rows(data.get("entries")):
        wait = _wait_ms(row, now_ms=now_ms)
        reason = row.get(HOLD_REASON_KEY)
        if not isinstance(reason, str) or not reason:
            reason = row.get("reason")
        cells.append(
            [
                _text(row.get("messageId")),
                _text(row.get("recipient")),
                _text(reason),
                DASH if wait is None else _format_wait(wait),
                _state(row, now_ms=now_ms),
            ]
        )
    return tables.render_table(
        None,
        ["MESSAGE", "TARGET", "REASON", "WAIT", "STATE"],
        cells,
        truncate_column=0,
    )


def _format_wait(wait_ms: int) -> str:
    seconds = max(0, wait_ms // 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h{minutes % 60:02d}m"
    return f"{hours // 24}d{hours % 24:02d}h"


@outbox_app.command("list")
def outbox_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every queued outbox entry with its wait reason and wait duration."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        result = services._daemon_request("outbox.list")
        if isinstance(result, Mapping):
            return CliResult(result, render=_render_outbox_list)
        return result

    services._execute(operation, json_output=json_output)


@outbox_app.command("prune")
def outbox_prune(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be pruned without deleting."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Move dead outbox entries (undeliverable scheme or expired TTL) to the DLQ."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("outbox.prune", {"dryRun": dry_run}),
        json_output=json_output,
    )
