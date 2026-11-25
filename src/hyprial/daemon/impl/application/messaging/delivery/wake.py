"""Observe-mode human-wake gate on the squire/user wake paths (phase A, I1).

Decision notes (phase A, integration I1; executor Q6, S5 open issue):

- The squire/user wake paths in ``sends.py`` deliver text, not an
  ``InboxMessage``, so ``observe_user_wake`` builds a local ``EnvelopeLike``
  from what the path is about to send and asks S5's ``ObserveWakeGate``,
  which classes it through ``message_class_of``.
- Class decision (I1, not S2's rulebook): the owner watchdog alerts and the
  autoupdate restart notice are daemon-authored notices, and the plan's Q6
  framing names the watchdog as the tension case.  ``daemon_notice=True``
  therefore builds their envelope with this node's ``system:hyprial:<node>``
  sender and intent ``system`` so ``message_class_of`` reads SYSTEM; their
  real sender (``quota-watchdog``, the squire agent) would read INTERACTION.
  This is a per-site choice made here, recorded for the Q6 enforce decision.
  Every other path keeps its real sender, message id and idempotency key
  with intent ``request``, so PAC dispatch ids and dispatch reply keys
  classify as they do in the inbox.
- One observation per wake per process: the first site to observe a
  message id wins and later hops skip it (bounded, last ``SEEN_CAPACITY``
  ids).  I7a removed the user-proxy hand-off and with it the ``proxy``
  site: the owner's copy is pushed by the daemon without a second hop, and
  across nodes the ``messageId`` field joins the logs.
- Observe mode never suppresses: the gate's answer is not consulted, and a
  failure building or classifying the envelope never blocks the wake.  Such
  a failure is logged as ``delivery.wake.observe_failed`` (warn, at most
  once per ``FAILURE_LOG_INTERVAL_S`` per process), so a broken gate is
  distinguishable from an absent ``would_suppress``.
- Paths are ``user.<site>`` (S3b, 2026-11-23: the ``squire.`` prefix is
  retired with the squire special cases); the gate logs ``delivery.wake.
  would_suppress`` under component ``delivery`` through ``app._log``.
- Phase A final integration (acceptance open issue for S5/wave 3): the
  remaining ``UserDeliveryRequest`` sites observe too, through
  ``observe_user_request``, which reads the request about to be delivered
  (recipient ``user:<request.owner>``).  The operator alarm owner DM
  (``operator-alarm``, observed on the alarm lane right before delivery)
  and the OrgFS resident-replica owner notice (``orgfs-owner-notice``) are
  daemon notices like the watchdogs, so ``daemon_notice=True``.  A
  ``message.send`` to a ``user:`` target (``send``) keeps its real sender;
  its intent (``request`` or ``reply``) never makes a message SYSTEM, so
  the envelope's ``request`` intent gives the same class.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from hyprial.daemon.impl.delivery import ObserveWakeGate
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.kernel import canonical_user_uri
from hyprial.daemon.impl.inbox.envelope import (
    SYSTEM_INTENT,
    SYSTEM_SENDER_PREFIX,
    message_class_of,
)

WAKE_PATH_PREFIX = "user."
OBSERVE_FAILED_EVENT = "delivery.wake.observe_failed"
SEEN_CAPACITY = 1024
FAILURE_LOG_INTERVAL_S = 300.0

_STATE_ATTRIBUTE = "_user_wake_observations"
_STATE_LOCK = threading.Lock()

__all__ = [
    "FAILURE_LOG_INTERVAL_S",
    "OBSERVE_FAILED_EVENT",
    "SEEN_CAPACITY",
    "WAKE_PATH_PREFIX",
    "daemon_notice_sender",
    "observe_user_request",
    "observe_user_wake",
]


class _Observations:
    """Message ids already observed and the last failure log time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._failure_logged_at: float | None = None

    def first(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self._seen:
                self._seen.move_to_end(message_id)
                return False
            self._seen[message_id] = None
            while len(self._seen) > SEEN_CAPACITY:
                self._seen.popitem(last=False)
            return True

    def may_log_failure(self) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._failure_logged_at
            if last is not None and now - last < FAILURE_LOG_INTERVAL_S:
                return False
            self._failure_logged_at = now
            return True


def _observations(app: Any) -> _Observations:
    with _STATE_LOCK:
        state = getattr(app, _STATE_ATTRIBUTE, None)
        if isinstance(state, _Observations):
            return state
        state = _Observations()
        try:
            setattr(app, _STATE_ATTRIBUTE, state)
        except Exception:  # noqa: BLE001 - an app without attributes is not deduplicated
            pass
        return state


def daemon_notice_sender(app: Any) -> str:
    """This node's daemon notice sender, ``system:hyprial:<node>``."""

    return f"{SYSTEM_SENDER_PREFIX}{app.node_id}"


def _gate_log(app: Any):
    def log(level: str, event: str, **fields: Any) -> None:
        app._log(level, "delivery", event, **fields)

    return log


def _log_failure(app: Any, state: _Observations, path: str, error: BaseException) -> None:
    if not state.may_log_failure():
        return
    try:
        app._log(
            "warn", "delivery", OBSERVE_FAILED_EVENT,
            path=path, errorType=type(error).__name__,
        )
    except Exception:  # noqa: BLE001 - logging never blocks a wake
        pass


def observe_user_wake(
    app: Any,
    site: str,
    *,
    message_id: str,
    sender: str,
    recipient: str,
    text: str,
    conversation_id: str,
    idempotency_key: str | None,
    daemon_notice: bool = False,
) -> None:
    """Ask the observe-mode gate about one human wake; never raises, never blocks."""

    path = f"{WAKE_PATH_PREFIX}{site}"
    state = _observations(app)
    failures: list[BaseException] = []

    def class_of(message: Any) -> Any:
        try:
            return message_class_of(message)
        except Exception as error:
            failures.append(error)
            raise

    try:
        if not state.first(message_id):
            return
        envelope = InboxMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            sender=daemon_notice_sender(app) if daemon_notice else sender,
            recipient=recipient,
            payload=text.encode("utf-8"),
            intent=SYSTEM_INTENT if daemon_notice else "request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=time.time_ns() // 1_000_000,
            idempotency_key=idempotency_key,
        )
        ObserveWakeGate(class_of, log=_gate_log(app)).observe(envelope, path=path)
    except Exception as error:  # noqa: BLE001 - observation never blocks a wake
        failures.append(error)
    if failures:
        _log_failure(app, state, path, failures[0])


def observe_user_request(
    app: Any, site: str, request: Any, *, daemon_notice: bool = False
) -> None:
    """``observe_user_wake`` for one ``UserDeliveryRequest``; never raises."""

    try:
        recipient = canonical_user_uri(request.owner)
        fields = {
            "message_id": request.message_id,
            "sender": request.sender,
            "text": request.message,
            "conversation_id": request.conversation_id,
            "idempotency_key": request.idempotency_key,
        }
    except Exception as error:  # noqa: BLE001 - observation never blocks a wake
        _log_failure(app, _observations(app), f"{WAKE_PATH_PREFIX}{site}", error)
        return
    observe_user_wake(app, site, recipient=recipient, daemon_notice=daemon_notice, **fields)
