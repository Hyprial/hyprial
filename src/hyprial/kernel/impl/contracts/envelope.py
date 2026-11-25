"""Envelope vocabulary shared by every domain (cell model phase A, slice S2).

Decisions (executor decisions, 2026-11-03):

* ``MessageClass`` is the storage class of one durable message.
  ``INTERACTION`` is a human or agent exchange whose body will be recorded in
  a conversation cell; ``SYSTEM`` is local run information (workflow notices,
  PAC dispatch, watchdog, TTL expiry) that never leaves the daemon.  Class
  decides storage only, never wake (Q6).
* Phase A keeps the class local (Q1): it is never put on
  ``hyprial-delivery-frame/v1``.  Each daemon derives it from signals it
  minted itself; the single rulebook is ``daemon.impl.inbox.envelope``.
* ``DISPATCH_MESSAGE_PREFIX`` is the prefix of every daemon-minted dispatch
  message id (PAC notifications, workflow requests, actor alerts and routine
  tasks).  It lives here so the id minting and the class rule share one
  constant.
"""

from __future__ import annotations

from enum import StrEnum

DISPATCH_MESSAGE_PREFIX = "workflow-"


class MessageClass(StrEnum):
    """Storage class of one durable message."""

    INTERACTION = "interaction"
    SYSTEM = "system"


__all__ = ["DISPATCH_MESSAGE_PREFIX", "MessageClass"]
