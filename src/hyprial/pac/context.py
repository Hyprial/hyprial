"""Node work context from one public snapshot, never inbox or reference bodies."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

from .errors import PAC_NODE_NOT_FOUND, PacError
from .principal import principal_matches_local_actor
from .subscription import snapshot


def _local_identity() -> tuple[str | None, str]:
    """(local owner, local machine) for execution-binding association.

    An unresolvable owner is not an error here: association simply stays
    empty (fail closed) rather than guessing a binding.
    """

    owner: str | None = None
    try:
        from hyprial.daemon.identity import node_owner_or_none
        from hyprial.home import configured_hyprial_home

        home, _source = configured_hyprial_home()
        owner = node_owner_or_none(hyprial_home=home)
    except Exception:  # noqa: BLE001 - association degrades to none, never guesses
        owner = None
    machine = os.environ.get("HYPRIAL_NODE_ID", socket.gethostname()).strip()
    return owner, machine


def node_context(database: Path, graph_id: str, node_id: str) -> dict[str, Any]:
    """Separate completed facts from the current recorded request/activation.

    All predecessors, references, assignments and the cursor come from the SAME
    snapshot. No follow-up lookup is allowed to advance an individual field.
    """
    state = snapshot(database, graph_id)
    nodes = {node["nodeId"]: node for node in state["structure"]["nodes"]}
    if node_id not in nodes:
        raise PacError(PAC_NODE_NOT_FOUND, f"node {node_id!r} not found on {graph_id!r}")
    node = nodes[node_id]
    flag = state["flags"][node_id]
    predecessors = sorted(edge["from"] for edge in state["structure"]["edges"]
                          if edge["to"] == node_id and edge["kind"] == "forward")
    activation = next((item for item in state["assignments"] if item["nodeId"] == node_id), None)
    # Actor association requires the FROZEN execution binding (design §5.3):
    # the node owner must be exactly agent:<local owner>:<local machine>:
    # <actorName> -- never a bare-name equality (a foreign same-named agent
    # must not associate with this runtime), and never a same-owner
    # different-actor URI.  Without a resolvable local owner there is no
    # binding to match against, so association stays empty.
    local_owner, local_machine = _local_identity()
    actor = next(
        (
            item
            for item in state["actors"]
            if item["nodeId"] == node_id
            or (
                local_owner is not None
                and principal_matches_local_actor(
                    node["owner"],
                    owner=local_owner,
                    machine=local_machine,
                    actor_name=item["actorName"],
                )
            )
        ),
        None,
    )
    return {
        "ok": True, "schemaVersion": 1, "type": "context",
        "graphId": graph_id, "nodeId": node_id, "version": state["version"],
        "journalId": state["journalId"], "cursor": state["cursor"],
        "owner": node["owner"], "kind": node["kind"], "briefRef": node["briefRef"],
        "requires": node.get("requires"),
        "blockedOn": state["blockedOn"][node_id],
        **flag,
        "currentActivation": activation,
        "predecessors": [
            {"nodeId": pred, "owner": nodes[pred]["owner"], "briefRef": nodes[pred]["briefRef"],
             **{key: value for key, value in state["flags"][pred].items() if key != "completedCount"}}
            for pred in predecessors
        ],
        "active": state["active"], "closed": state["closed"],
        **(
            {
                "deadlineMs": node["deadlineMs"],
                **(
                    {"guardedByNodeId": node["guardedByNodeId"]}
                    if "guardedByNodeId" in node
                    else {}
                ),
            }
            if node["kind"] == "clock"
            else {}
        ),
        **(
            {
                "actor": {
                    **actor,
                    "assignments": [
                        item for item in state["assignments"]
                        if local_owner is not None
                        and principal_matches_local_actor(
                            item["owner"],
                            owner=local_owner,
                            machine=local_machine,
                            actor_name=actor["actorName"],
                        )
                    ],
                }
            }
            if actor is not None
            else {}
        ),
    }
