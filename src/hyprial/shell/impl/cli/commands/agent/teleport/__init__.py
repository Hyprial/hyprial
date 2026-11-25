# Decision record (file header; no inline comments by repository rule):
# - The command wraps exactly one ``teleport.run`` request and chooses no
#   step of the move; the daemon owns the sequence (docs/design/
#   agent-teleport-protocol.md §4).  It never imports the daemon's teleport
#   package (architecture test ``test_teleport_import_fence``).
# - ``--to`` is the target node id (``HYPRIAL_NODE_ID``); there is no host
#   resolver.  There is no peer authentication beyond the transport (KP-047).
# - Confirmation mirrors ``agent destroy``: ``--yes`` skips it, ``--json`` or a
#   non-terminal stdin without ``--yes`` is ``CONFIRMATION_REQUIRED`` and sends
#   nothing, declining is ``CANCELLED``.  ``--dry-run`` changes nothing, needs
#   no confirmation and cannot be combined with ``--yes``.
# - The IPC wait must stay strictly above the daemon's ``teleport.run``
#   deadline (``TELEPORT_BUDGET.ipc_seconds`` in the kernel,
#   560 s: step budgets 535 s plus 25 s), so a legal slow move is answered by
#   the daemon with a ``TELEPORT_*`` verdict and not cut short here.
# - The registered ``INVALID_ARGUMENT`` code comes from ``output.errors``,
#   which already owns the CLI's kernel import: the import ratchet forbids a
#   new CLI->kernel edge.
"""``hyprial agent teleport``: move a local agent to another node."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.agent.admin import agent_app
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, confirm
from hyprial.shell.impl.cli.output.errors import TELEPORT_INVALID_ARGUMENT

TELEPORT_IPC_TIMEOUT_SECONDS = 600.0


def _render(result: Mapping[str, Any]) -> str:
    source = result["from"]
    target = result["to"]
    agent = result["agent"]
    if result["dryRun"]:
        lines = [
            f"Dry run: agent {agent} on {source['node']} can move to "
            f"{target['node']}. Nothing was changed."
        ]
    else:
        lines = [
            f"Teleported agent {agent}: {source['node']} -> {target['node']}",
            f"  now at {target['uri']}",
            f"  {result['bytes']} bytes sent",
        ]
    routines = result["routines"]
    if routines:
        lines.append(
            "  routines: "
            + ", ".join(
                f"{item['name']} ({'enabled' if item['enabled'] else 'disabled'})"
                for item in routines
            )
        )
    commit = result.get("commit")
    if commit is not None and not commit["ok"]:
        paused = ", ".join(commit["pausedRoutines"]) or "none listed"
        lines.append(
            f"  warning: the target did not confirm the final step; routines "
            f"still paused on {target['node']}: {paused}"
        )
    return "\n".join(lines)


@agent_app.command("teleport")
def agent_teleport(
    agent: str = typer.Argument(..., help="The local agent to move."),
    to: str = typer.Option(
        ..., "--to", help="Target node id (HYPRIAL_NODE_ID of the other machine)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Check the move and show the plan; change nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the confirmation and move the agent."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Move an agent, its home and its session to another node.

    The agent is stopped here, its home is sent to the target daemon, resumed
    there on the same session, and then removed from this node.  The target
    must be a node id reachable through the daemon network.  If the outcome on
    the target cannot be confirmed the command fails with
    TELEPORT_UNRESOLVED, the source agent stays stopped, and the error lists
    the manual check.  The peer is not authenticated beyond the transport.
    """
    services = get_services()

    def operation() -> CliResult:
        if dry_run and yes:
            raise services.CliError(
                TELEPORT_INVALID_ARGUMENT, "--dry-run cannot be combined with --yes"
            )
        if not dry_run and not yes:
            if json_output or not services._stdin_isatty():
                raise services.CliError(
                    "CONFIRMATION_REQUIRED",
                    f"teleporting agent {agent!r} from a non-interactive command "
                    "requires --yes",
                )
            question = (
                f"Stop agent {agent!r}, move it to node {to!r} and remove it "
                "from this node. Continue?"
            )
            if not confirm(question):
                raise services.CliError("CANCELLED", "agent teleport cancelled")
        result = services.runtime.request(
            "teleport.run",
            {"agent": agent, "to": to, "dryRun": dry_run},
            timeout=TELEPORT_IPC_TIMEOUT_SECONDS,
        )
        if not isinstance(result, dict) or "from" not in result or "to" not in result:
            raise services.CliError(
                "INVALID_RESPONSE", "daemon teleport.run result is malformed"
            )
        return CliResult(result, render=_render)

    services.runtime.execute(operation, json_output=json_output)
