# Decision record (UA1g, 2026-11-18; PR 2941 review):
# - The borrowed-actor admission a peer asks this daemon for (``admit`` over
#   the wire, answered by ``RemoteWorkflow.admit_local``) lives here, moved
#   out of the package module so it stays under the line limit without
#   compressed statements.  It owns no state and makes no network call; the
#   wire stays in ``RemoteWorkflow``.
# - Order: the actor must be a local agent address of this daemon's owner
#   and node, then the owner's user agent is refused (it has no harness and
#   never takes work, UA1c), then the record must exist, then the ordinary
#   ``dispatch_gate`` runs.
"""Admit a borrowed actor on this daemon for a peer's workflow node."""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.user_agent.naming import refuse_user_agent_work
from hyprial.identity import PacError
from hyprial.kernel import parse_agent_uri

__all__ = ["admit_local_actor"]


def admit_local_actor(app: Any, data: Any) -> dict[str, Any]:
    """The admission answer for the borrowed actor ``data["owner"]``."""

    actor = data.get("owner")
    principal = parse_agent_uri(actor) if isinstance(actor, str) else None
    if principal is None or principal[:2] != (app.owner, app.node_id):
        raise PacError("WORKFLOW_NOT_OWNER", "actor is not local to this daemon")
    refuse_user_agent_work(app.agents, actor, error=PacError)
    recipient = app._resolve_send_sender(actor)
    entity = app.agents.get(recipient)
    if entity is None:
        raise PacError(
            "WORKFLOW_REMOTE_ACTOR_NOT_FOUND", "borrowed actor does not exist"
        )
    dispatch_gate(
        target=actor,
        capabilities=entity.capabilities,
        role=data.get("role"),
        first_output_eta=data.get("firstOutputEta"),
        human_gates_declared=data.get("humanGatesDeclared") is True,
        emit=app._log,
        source="pac.remote.admission",
    )
    return {
        "ok": True,
        "owner": actor,
        "features": ["pac-expansion-v1"],
    }
