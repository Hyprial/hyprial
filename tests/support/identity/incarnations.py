# Decision record (UUID addressing, ruling X; integrator rule for tests about
# a same-name successor incarnation, round 2):
# - After the id-form flip a forward destroy freezes the name, so a test can
#   no longer destroy and re-create a name to get "the same name, another
#   incarnation".  AgentRegistry.rotate_secret_authority gives exactly that:
#   the same actor with a new entity token (and, with the switch on, a new
#   a.<id> address), and it is the only same-name successor these tests use.
# - The daemon reads agents through the Agent actor's in-memory projection,
#   which only commands update; the registry rotation is not a command, so
#   the helper refreshes the projection the way the actor does after a
#   registry-level change.
"""Same-name successor incarnations for daemon-application tests."""

from __future__ import annotations

from typing import Any


def rotate_incarnation(app: Any, name: str) -> Any:
    """Give ``name`` a new incarnation and return the rotated record."""

    from hyprial.identity.impl.agents.actor._base import _refresh_projection

    actor = app._agent_session_domains.agent
    rotated = actor._registry.rotate_secret_authority(name)
    _refresh_projection(
        actor._projection, actor._registry, actor._liveness, actor._version.bump()
    )
    return rotated


__all__ = ["rotate_incarnation"]
