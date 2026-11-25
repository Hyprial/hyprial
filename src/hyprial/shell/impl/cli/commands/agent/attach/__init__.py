"""``hyprial agent attach`` / ``hyprial agent detach``: externally supervised harnesses.

Decision notes (phase B, S8):

- Thin on purpose: each command is one ``agent.attach``/``agent.detach``
  daemon request.  The daemon owns the identity, the custody facts and the
  cell; the shell neither checks names nor retries a write.
- ``--json`` is the daemon result through ``CliResult`` (one object with
  ``ok``); the text render states the facts the operator acts on: the agent
  URI, custody, whether anything changed, and where the cell change stands.
- The help and the render state the sender-auth limit plainly (S8 review):
  the ``--from <name>`` read path of a session-less harness depends on
  observe mode; under enforce only the operator form of reply/ack works.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.agent.admin import agent_app
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult

_CELL_TEXT = {
    "recorded": "cell recorded",
    "deferred": "cell deferred; reconciled when the cell runtime is reachable",
    "skipped": "no agent cell: the agent owner is not the OrgFS author",
    "refused": "cell refused by the cell runtime (for example retired); not retried",
}


def render_attachment(data: Mapping[str, Any]) -> str:
    """One line per fact: who, custody, the change and the cell state."""

    verb = "attached" if data.get("attached") is True else "detached"
    if data.get("created") is True:
        change = "new agent identity registered"
    elif data.get("changed") is True:
        change = verb
    else:
        change = f"already {verb}; nothing changed"
    cell = data.get("cell")
    state = cell.get("state") if isinstance(cell, Mapping) else None
    lines = [
        f"{verb} {data.get('actor')}   custody {data.get('custody')}   {change}",
        f"agent {data.get('agentId')}   cell {data.get('cellId')}   "
        f"{_CELL_TEXT.get(str(state), str(state))}",
    ]
    if data.get("attached") is True:
        name = data.get("name")
        lines.extend((
            "the daemon never starts, restores or supervises this harness "
            "('hyprial agent start' refuses it)",
            f"read with 'hyprial message batch --from {name}' (needs observe sender "
            "auth or a registered session); reply and ack also take "
            f"'--from user:<owner> --on-behalf-of {name}'",
        ))
    return "\n".join(lines)


def _result(result: Any) -> Any:
    if isinstance(result, Mapping):
        return CliResult(dict(result), render=render_attachment)
    return result


@agent_app.command("attach")
def agent_attach(
    name: str = typer.Argument(..., help="Bare agent name to attach (created if new)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Attach an externally supervised harness as an agent (custody=external).

    Registers the agent identity, or reuses it when it is already an
    external-custody agent, and makes the harness its single cell member;
    the cell membership follows the agent record, so members added to it by
    hand are removed.  The daemon never starts, restores or stops that
    harness ('hyprial agent start' refuses an attached agent): messages to
    the agent wait in its inbox and the harness reads them with the message
    commands.  Without a registered session, reading with --from <name>
    works only while sender authentication is in observe mode (the default);
    under enforce, reply and ack use --from user:<owner> --on-behalf-of
    <name> and reading has no operator form yet.  Repeating the command
    changes nothing.  A name held by a daemon-managed agent is refused.
    """
    services = get_services()

    services.runtime.execute(
        lambda: _result(services.runtime.request("agent.attach", {"name": name})),
        json_output=json_output,
    )


@agent_app.command("detach")
def agent_detach(
    name: str = typer.Argument(..., help="Attached agent name or canonical agent URI."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Detach the external harness of an attached agent.

    Removes the harness from the agent's cell.  The agent identity, its ids,
    its inbox and its history stay; messages keep queueing until a harness
    attaches again with 'hyprial agent attach'.  This is neither stop nor
    destroy, and it refuses a daemon-managed agent.
    """
    services = get_services()

    services.runtime.execute(
        lambda: _result(services.runtime.request("agent.detach", {"name": name})),
        json_output=json_output,
    )


__all__ = ["agent_attach", "agent_detach", "render_attachment"]
