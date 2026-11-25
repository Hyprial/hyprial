# Decision record (UA2, 2026-11-18; rulings R27 and the UA2 brief):
# - A message to the owner is an ordinary queued message.  The envelope keeps
#   ``user:<owner>`` as the target and sender string: that string is the
#   session principal the HTTP layer injects, so conversation ids, record
#   ends, history filters and ``peer`` stay exactly what they were.  The
#   machine-local user agent address never enters an envelope.
# - ``user:<owner>`` is delivered on this node only while this node holds the
#   owner's user agent.  The daemon then registers ``user:<owner>`` with its
#   local-first delivery transport as a local actor, so the durable outbox
#   hands rows to this node's inbox and never to the network.  Registration
#   is local only: nothing is announced.
# - Registration happens at composition and again after an ensure that found
#   or created the user agent (it is created on a thread after
#   ``daemon.ready``), and once more on demand when a send asks.  It is
#   idempotent per delivery transport: a new transport is registered anew.
# - A target or sender that is a user agent address is rewritten to
#   ``user:<owner>`` of that address's owner before conversation ids are
#   derived, so a reply addressed to another machine's instance lands with
#   the local actor and in the same conversation.  Recognition (UA4): any
#   machine's name form by its text; this machine's instance in every
#   spelling by its registry marker when the caller passes its registry
#   (the send and reply paths do; the pure group fold does not).  Another
#   machine's id form is an ordinary agent address until the merge step.
# - UA2b item 4: ``USER_TARGET_FACTS`` are the capability facts dispatch
#   admission sees for any ``user:`` target, on every node: a person is never
#   a worker, so a role is refused.
# - UA2b item 3: a send queues to ``user:<owner>`` only while a conversation
#   recorder is composed and open and the cell runtime is neither closed nor
#   failing to compose (the application attributes ``wiring.conversations``
#   and ``wiring.cells`` keep); without one nothing would ever acknowledge
#   the row, so the send keeps the push-only path.  The local registration
#   stays, so a reply addressed to the owner is still delivered here.
# - UA2b item 3: a push that went out is remembered in memory (bounded,
#   ``PUSHED_CAPACITY``), so the recorder's observer can acknowledge a
#   pushed message even when it was recorded only as a placeholder: the
#   owner already has the text.  A restart forgets it; such a placeholder
#   then stays queued until the hold TTL, as an unpushed one does.  A
#   placeholder recorded before its push went out (the first push raised and
#   the same-key retry pushed, or the recorder ran during the receipt wait)
#   is held (``hold_unpushed_placeholder``, bounded) and acknowledged through
#   ``message.ack`` when that push goes out; an ack failure there is logged
#   and the row is left to the TTL.
"""Bind ``user:<owner>`` to this node's user agent, and canonicalise its addresses."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from hyprial.kernel import TARGET_KIND_USER, canonical_user_uri, is_own_user_uri, parse_agent_uri

from .ensure import local_user_agent
from .lookup import local_user_agent_address
from .naming import USER_AGENT_TEMPLATE, is_user_agent, is_user_agent_address

__all__ = [
    "USER_TARGET_FACTS",
    "bind_local_user_target",
    "canonical_user_end",
    "local_user_target",
    "hold_unpushed_placeholder",
    "note_user_pushed",
    "own_user_target_rows",
    "recording_available",
    "user_target_capabilities",
]

USER_TARGET_FACTS: Mapping[str, Any] = MappingProxyType({"template": USER_AGENT_TEMPLATE})

PUSHED_CAPACITY = 4096

_REGISTRATION = "_user_target_registration"
_PUSHED = "_user_pushed_messages"
_HELD = "_user_held_placeholders"
_DELIVERY = "_local_delivery"
_LOCK = threading.Lock()


def canonical_user_end(address: str, agents: Any = None) -> str:
    """``user:<owner>`` for a user agent address; other addresses unchanged.

    Any machine's name form is recognised by its text; with *agents* (this
    machine's registry) the local instance is recognised in every spelling.
    """

    if is_user_agent_address(address):
        parsed = parse_agent_uri(address)
        assert parsed is not None
        return canonical_user_uri(parsed[0])
    if agents is None or not isinstance(address, str) or not address:
        return address
    try:
        agent = agents.get(address)
    except Exception:  # noqa: BLE001 - an unresolvable address is not the user agent
        return address
    return canonical_user_uri(str(agent.owner)) if is_user_agent(agent) else address


def bind_local_user_target(app: Any) -> bool:
    """Register ``user:<owner>`` as a local actor once this node holds the user agent.

    Returns whether it is registered.  Needs the application's local-first
    delivery transport (``app._local_delivery``); without one, or without a
    local user agent, nothing is registered.
    """

    delivery = getattr(app, _DELIVERY, None)
    held = getattr(app, _REGISTRATION, None)
    if held is not None and held[0] is delivery:
        return True
    owner = getattr(app, "owner", None)
    if delivery is None or not owner:
        return False
    try:
        if local_user_agent_address(app.agents) is None:
            return False
    except Exception:  # noqa: BLE001 - an unreadable registry keeps today's path
        return False
    with _LOCK:
        held = getattr(app, _REGISTRATION, None)
        if held is None or held[0] is not delivery:
            registration = delivery.register_actor(canonical_user_uri(owner))
            setattr(app, _REGISTRATION, (delivery, registration))
    return True


def local_user_target(app: Any, target: str) -> bool:
    """Whether a message to *target* is queued to this node's own user agent.

    True only for ``user:<this owner>`` while this node holds the owner's user
    agent and ``user:<owner>`` is registered for local delivery.  Every other
    ``user:`` target keeps the receiver push path.
    """

    if not is_own_user_uri(target, getattr(app, "owner", None)):
        return False
    return recording_available(app) and bind_local_user_target(app)


def recording_available(app: Any) -> bool:
    """Whether a conversation recorder will record (and so acknowledge) a row."""

    if getattr(app, "_conversation_recorder", None) is None:
        return False
    if getattr(app, "_conversation_recorder_closed", False):
        return False
    if getattr(app, "_cell_runtime_closed", False):
        return False
    return not (
        getattr(app, "_cell_runtime", None) is None
        and getattr(app, "_cell_runtime_failed_at", None) is not None
    )


def _remember(app: Any, attribute: str, message_id: str) -> None:
    seen = getattr(app, attribute, None)
    if seen is None:
        seen = OrderedDict()
        setattr(app, attribute, seen)
    seen[message_id] = None
    seen.move_to_end(message_id)
    while len(seen) > PUSHED_CAPACITY:
        seen.popitem(last=False)


def note_user_pushed(app: Any, message_id: str) -> None:
    """Remember that the owner push of *message_id* went out.

    A placeholder of it that was held unpushed is acknowledged now.
    """

    with _LOCK:
        _remember(app, _PUSHED, message_id)
        held = getattr(app, _HELD, None)
        release = held is not None and held.pop(message_id, False) is None
    if not release:
        return
    try:
        result = app.handle(
            "message.ack",
            {"from": canonical_user_uri(app.owner), "messageId": message_id},
            _trusted_message_origin="daemon",
        )
        acknowledged = isinstance(result, dict) and bool(result.get("acknowledged"))
    except Exception:  # noqa: BLE001 - the push stands; the row meets the TTL
        acknowledged = False
    app._log("info" if acknowledged else "warn", "conversations",
             "conversation.user_row_acknowledged", messageId=message_id,
             acknowledged=acknowledged, placeholder=True)


def hold_unpushed_placeholder(app: Any, message_id: str) -> bool:
    """Hold a placeholder until its push goes out; True when it already did."""

    with _LOCK:
        if message_id in (getattr(app, _PUSHED, None) or ()):
            return True
        _remember(app, _HELD, message_id)
        return False


def user_target_capabilities(app: Any) -> dict[str, Any]:
    """The capability facts dispatch admission sees for ``user:<owner>``.

    The local user agent's record facts, so a role-carrying message to the
    owner is refused like work aimed at the user agent; the template fact
    alone when the record cannot be read.
    """

    try:
        agent = local_user_agent(app.agents.list(), str(app.agents.owner))
    except Exception:  # noqa: BLE001 - admission still knows it is the user
        agent = None
    facts = getattr(agent, "capabilities", None)
    return dict(facts) if hasattr(facts, "items") else dict(USER_TARGET_FACTS)


def own_user_target_rows(app: Any) -> dict[str, dict[str, Any]]:
    """The ``targets`` row of ``user:<owner>`` while it is queued to this node.

    Status ``user-agent``: a message to the owner is queued and recorded on
    this node.  Empty otherwise.
    """

    try:
        own = canonical_user_uri(str(getattr(app, "owner", "") or ""))
    except ValueError:
        return {}
    if not local_user_target(app, own):
        return {}
    return {own: {"targetKind": TARGET_KIND_USER, "targetUri": own, "actor": own,
                  "status": "user-agent", "deliverable": True}}
