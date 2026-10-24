"""The ``hyprial message batch`` view: one daemon-rendered coalesced batch.

Thin on purpose.  The daemon owns ordering, truncation and the spill file, so
this command never renders a batch itself: human output is ``delivery.text``
byte for byte and ``--json`` is exactly one daemon response through
``CliResult`` / ``shell.impl.cli.output``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import _message_claim_params
from hyprial.shell.impl.cli.commands.message import message_app
from hyprial.shell.impl.cli.commands.message.send import _validate_from_identity
from hyprial.shell.impl.cli.output import CliResult


def _render_batch(data: Mapping[str, Any]) -> str:
    """The canonical ``inbox-batch-v1`` text exactly as the daemon emitted it."""

    delivery = data.get("delivery")
    if isinstance(delivery, Mapping):
        text = delivery.get("text")
        if isinstance(text, str):
            return text
    return ""


@message_app.command("batch")
def message_batch(
    source: str = typer.Option(
        ...,
        "--from",
        help="Local actor whose inbox to read.",
        callback=_validate_from_identity,
    ),
    cursor: str | None = typer.Option(
        None, "--cursor", help="Replay one exact frozen snapshot."
    ),
    after_cursor: str | None = typer.Option(
        None,
        "--after-cursor",
        help="Capture the successor of a previously consumed snapshot.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Print one complete inbox batch (full bodies, or the one spill file path).

    This is a consuming read: the daemon stamps the returned rows fetched but
    acknowledges nothing.  Reply or ack every listed message id separately.
    """

    if cursor is not None and after_cursor is not None:
        raise typer.BadParameter("--cursor and --after-cursor are mutually exclusive")
    services = get_services()
    params: dict[str, Any] = _message_claim_params(source)
    if cursor is not None:
        params["cursor"] = cursor
    if after_cursor is not None:
        params["afterCursor"] = after_cursor

    def operation() -> Any:
        result = services._daemon_request("message.pending.batch", params)
        if isinstance(result, Mapping):
            return CliResult(result, render=_render_batch)
        return result

    # The daemon owns the inbox: a missing local home must not refuse to ask
    # it.  The request itself reports an unreachable daemon honestly.
    services._execute(operation, json_output=json_output, allow_missing_home=True)


__all__ = ["message_batch"]
