"""Reserved daemon-internal agent actor names.

These names identify daemon-owned actors used by ordinary message delivery.
The check is keyed on the actor segment, never on the owner or machine
segment, so the same reserved name is protected on every node.  Agent
registration and interactive-session admission reject the names for ordinary
agents.  The constant lives in kernel contracts so the daemon and identity
layers can share one definition without identity importing daemon.
"""

from __future__ import annotations

#: Actor name of the daemon-owned dispatch IO identity (PAC notices and
#: cross-machine dispatch).  Renamed from the retired ``mfu-coordinator``
#: without an alias: durable records and peers still carrying the old sender
#: are not recognised.
DISPATCH_SERVICE_ACTOR_NAME = "dispatch-coordinator"

#: Actor name daemon turn-hook exchanges are sent from and replied to.
TURN_HOOKS_ACTOR_NAME = "_turn-hooks"

RESERVED_AGENT_ACTOR_NAMES = frozenset(
    {DISPATCH_SERVICE_ACTOR_NAME, TURN_HOOKS_ACTOR_NAME}
)

__all__ = [
    "DISPATCH_SERVICE_ACTOR_NAME",
    "RESERVED_AGENT_ACTOR_NAMES",
    "TURN_HOOKS_ACTOR_NAME",
]
