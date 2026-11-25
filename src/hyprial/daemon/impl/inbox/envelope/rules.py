"""The single message-class rulebook (cell model phase A, slice S2).

Decisions (merged plan §2.0, executor decisions Q1 and Q4):

* ``message_class_of`` is the only class accessor.  Every consumer (delivery
  retention, conversation recording, the wake gate) asks it; nobody keeps a
  second rule list.
* An explicit stamp on the local envelope wins.  Without one the class is
  derived from signals this daemon minted itself, never from a value a caller
  chooses: ``conversation_id``, the recipient name, the sender or ``intent``
  on its own and free-form idempotency keys are caller-chosen and are ignored.
* SYSTEM signals:

  - sender ``system:hyprial:<node>`` together with ``intent == "system"``:
    every daemon-authored notice, alarm and reply reminder.  The sender alone
    is not sound: ``message.send`` accepts a caller-chosen ``from`` of that
    shape.  ``message.send`` and ``message.reply`` only ever produce
    ``request`` or ``reply``, and every daemon producer of that sender sets
    ``intent="system"``, so the pair is minted by this daemon;
  - message id with the kernel ``DISPATCH_MESSAGE_PREFIX``: the dispatch IO
    mints these for PAC notifications, workflow requests, actor alerts and
    routine tasks (``dispatch.identity.dispatch_message_id``);
  - idempotency key exactly ``reply:workflow-<uuid>``: ``message.reply`` and
    the mechanism answer build ``reply:<inbound id>``, so this is a reply to a
    PAC dispatch (Q4: PAC worker replies are SYSTEM; one rule, revertible).

* Everything else is INTERACTION, the safe direction: an interaction body is
  only dropped after it is recorded.
* ``inherit_envelope`` runs on the replying node only.  It copies the
  original's explicit thread, conversation cell and class stamps onto a reply
  that has none of its own; a derived class is not copied because the reply
  derives its own from the same local signals.
"""

from __future__ import annotations

import re

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX, MessageClass

SYSTEM_SENDER_PREFIX = "system:hyprial:"
SYSTEM_INTENT = "system"

_DISPATCH_REPLY_KEY = re.compile(
    "reply:"
    + re.escape(DISPATCH_MESSAGE_PREFIX)
    + r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def derive_message_class(message: InboxMessage) -> MessageClass:
    """The class sound local signals give *message*, ignoring any stamp."""

    if message.intent == SYSTEM_INTENT and message.sender.startswith(
        SYSTEM_SENDER_PREFIX
    ):
        return MessageClass.SYSTEM
    if message.message_id.startswith(DISPATCH_MESSAGE_PREFIX):
        return MessageClass.SYSTEM
    key = message.idempotency_key
    if isinstance(key, str) and _DISPATCH_REPLY_KEY.fullmatch(key) is not None:
        return MessageClass.SYSTEM
    return MessageClass.INTERACTION


def message_class_of(message: InboxMessage) -> MessageClass:
    """The class of *message*: its explicit stamp, else the derived class."""

    if message.message_class is not None:
        return message.message_class
    return derive_message_class(message)


def inherit_envelope(reply: InboxMessage, original: InboxMessage) -> InboxMessage:
    """*reply* with the original's explicit envelope where it has none."""

    return reply.with_envelope(
        thread_id=reply.thread_id or original.thread_id,
        conversation_cell_id=(
            reply.conversation_cell_id or original.conversation_cell_id
        ),
        message_class=reply.message_class or original.message_class,
    )


__all__ = [
    "SYSTEM_INTENT",
    "SYSTEM_SENDER_PREFIX",
    "derive_message_class",
    "inherit_envelope",
    "message_class_of",
]
