"""Reserved daemon-internal agent actor names (send-links README rulings 45/51/63).

The send-link mechanism exempts daemon-internal mechanism identities as sender
or recipient.  The exemption is keyed on the **actor segment**, never on the
owner or machine segment: a peer node's ``dispatch-coordinator`` has no link
file either, so the same reserved name exempts anywhere.

Because the exemption is a name check, agent registration must refuse these
names for ordinary agents.  Otherwise any user could create an agent called
``dispatch-coordinator`` and bypass every link constraint.  The constant lives
in the kernel contracts so both the daemon (``link_governed``) and identity
(registration guard) can share one definition without identity importing
daemon.
"""

from __future__ import annotations

#: Actor name of the daemon-owned dispatch IO identity (PAC notices and
#: cross-machine dispatch).  Renamed from the retired ``mfu-coordinator``
#: without an alias: durable records and peers still carrying the old sender
#: are not recognised.
DISPATCH_SERVICE_ACTOR_NAME = "dispatch-coordinator"

#: Actor name daemon turn-hook exchanges are sent from and replied to.
TURN_HOOKS_ACTOR_NAME = "_turn-hooks"

#: Every actor name exempt from link governance as sender or recipient.
RESERVED_AGENT_ACTOR_NAMES = frozenset(
    {DISPATCH_SERVICE_ACTOR_NAME, TURN_HOOKS_ACTOR_NAME}
)

__all__ = [
    "DISPATCH_SERVICE_ACTOR_NAME",
    "RESERVED_AGENT_ACTOR_NAMES",
    "TURN_HOOKS_ACTOR_NAME",
]
