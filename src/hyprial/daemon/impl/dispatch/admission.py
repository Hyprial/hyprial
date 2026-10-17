"""One dispatch admission policy shared by legacy workflow and PAC paths.

Also holds the send-link admission slot (WP4): ``link_governed`` decides which
messages the link mechanism governs, and ``LinkAdmission`` carries the optional
``LinkGate`` every dispatch and ingress boundary asks before acting.  The gate
itself is implemented in ``links/service.py`` (WP2) and injected at
composition; this module never imports the links implementation.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from hyprial.kernel import (
    RESERVED_AGENT_ACTOR_NAMES,
    ipc_errors,
    parse_agent_uri,
)

from hyprial.daemon.impl.dispatch.identity import DISPATCH_SERVICE_ACTOR_NAME
from hyprial.daemon.impl.links.ports import LinkGate

DISPATCH_ROLES = frozenset({"plan", "dispatch", "review", "execute"})

_RHYTHM = re.compile(
    r"明早|明天|上班后|下周|\btomorrow\b|\bnext morning\b", re.IGNORECASE
)
_ACCEPTED = re.compile(r"\bACCEPTED\b", re.IGNORECASE)
_HUMAN_GATES_FIELD = re.compile(r"\bhuman_gates\s*:")


def dispatch_gate(
    *,
    target: str,
    capabilities: Mapping[str, Any],
    role: str | None,
    first_output_eta: str | None = None,
    accepted_text: str = "",
    human_gates_declared: bool = False,
    emit: Callable[..., None],
    source: str,
) -> tuple[str, ...]:
    """Reject only an execute dispatch to a known interactive entity.

    ``role=None`` denotes ordinary messaging/receipt observation, not a PAC
    target (whose schema defaults to execute). Unknown facts are observable,
    never treated as headless or as interactive. Rhythm language warns only.
    """

    if role is not None and (not isinstance(role, str) or role not in DISPATCH_ROLES):
        raise ipc_errors.DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "role must be plan, dispatch, review, or execute",
        )
    if role is not None and not isinstance(capabilities.get("interactive"), bool):
        emit(
            "warn",
            "daemon",
            "dispatch.capabilities_unknown",
            target=target,
            role=role,
            source=source,
        )
    if role == "execute" and capabilities.get("interactive") is True:
        detail = "交互会话只做 plan/dispatch/review；role=execute 必须派给 headless 实体"
        emit(
            "warn",
            "daemon",
            "dispatch.role_mismatch",
            target=target,
            role=role,
            source=source,
        )
        raise ipc_errors.DaemonRequestError(
            ipc_errors.DISPATCH_ROLE_MISMATCH,
            detail,
            {"target": target, "role": role},
        )
    receipt = accepted_text if _ACCEPTED.search(accepted_text) else ""
    if human_gates_declared or (receipt and _HUMAN_GATES_FIELD.search(receipt)):
        return ()
    fields = (("first_output_eta", first_output_eta or ""), ("ACCEPTED", receipt))
    warnings = []
    for field, text in fields:
        match = _RHYTHM.search(text)
        if match is not None:
            warning = (
                f"dispatch.rhythm_lint target={target} field={field} "
                f"word={match.group(0)} (无 human_gates；先警不拒)"
            )
            emit(
                "warn",
                "daemon",
                "dispatch.rhythm_lint",
                target=target,
                field=field,
                word=match.group(0),
                source=source,
                detail=warning,
            )
            warnings.append(warning)
    return tuple(warnings)


LINK_GATE_UNBOUND_EVENT = "link.gate_unbound"
LINK_GATE_ERROR_EVENT = "link.gate_error"
LINK_HELD_EVENT = "dispatch.link_held"
LINK_ADMITTED_EVENT = "dispatch.link_admitted"
#: Bound on the in-memory held/admitted tables (§2.1): they are observability
#: plus the inbound-waiting-injection count, never a durable store.
_LINK_HELD_LIMIT = 4096
#: One ``link.gate_unbound`` log per recipient per minute (§2.1).
_LINK_GATE_UNBOUND_LOG_INTERVAL_MS = 60_000


def link_governed(
    sender: str,
    recipient: str,
    *,
    internal_actors: frozenset[str] = RESERVED_AGENT_ACTOR_NAMES,
) -> bool:
    """Does the link mechanism govern this message? (§4.6, rulings 25/45/51)

    Only canonical ``agent:`` sender and recipient URIs are governed.  That one
    predicate exempts ``user:`` (owner and other people), ``route:``/
    ``adapter:``, ``channel:``, ``system:`` and bare node names, which have no
    link file.  Self-sends need no link.

    ``internal_actors`` is the set of reserved daemon actor *names*, not URIs:
    the actor segment is read with ``parse_agent_uri`` and compared exactly, so
    a peer node's ``dispatch-coordinator`` is exempt as well (README rulings
    45/51) and an intent string, a ``turnHookRequest`` flag or a name that
    merely starts with ``_`` never widens the exemption (review B2).
    """

    if sender == recipient:
        return False
    sender_actor = _agent_actor(sender)
    recipient_actor = _agent_actor(recipient)
    # README ruling 51: a daemon internal actor is exempt as sender OR as
    # recipient (a PAC worker reply or turn-hook reply addressed to
    # ``dispatch-coordinator`` has no link file either).
    if sender_actor is not None and sender_actor in internal_actors:
        return False
    if recipient_actor is not None and recipient_actor in internal_actors:
        return False
    if sender_actor is None or recipient_actor is None:
        return False
    return True


def _agent_actor(uri: str) -> str | None:
    """The actor segment of a canonical agent URI, or None for any other shape."""

    parsed = parse_agent_uri(uri)
    return None if parsed is None else parsed[2]


class LinkAdmission:
    """The one slot holding the optional ``LinkGate`` (WP4 §2.1).

    Composition creates exactly one instance and hands the same object to the
    inbox service, the runtime bridge and the application, so WP2's single
    ``bind`` is visible to every boundary.  An unbound slot is a real state,
    not a bypass: ingress still accepts (the local wiring may self-heal) and
    dispatch holds (no link proof means no injection).

    ``ingress_refusal`` and ``dispatch_admitted`` are called on the inbox actor
    thread, partly inside a SQLite transaction.  They answer from the gate's
    in-memory copy and never do I/O themselves.

    ``internal_actors`` holds reserved daemon actor *names* (README rulings
    45/51/63); ``link_governed`` compares the parsed actor segment, so the
    exemption is independent of owner and machine.  WP2 reuses the set through
    the read-only :attr:`internal_actors` property.
    """

    def __init__(
        self,
        *,
        internal_actors: frozenset[str] = RESERVED_AGENT_ACTOR_NAMES,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self._internal_actors = frozenset(internal_actors)
        self._logger = logger
        self._gate: LinkGate | None = None
        #: message id -> (recipient, held_at_ms); insertion order is the
        #: eviction order past the cap.
        self._held: OrderedDict[str, tuple[str, int]] = OrderedDict()
        #: message id -> recipient for rows the gate admitted but that are not
        #: yet fetched/consumed.  Read-only admission state for the inbound
        #: waiting count (review B6); never consulted to grant admission.
        self._admitted: OrderedDict[str, str] = OrderedDict()
        self._unbound_logged_at_ms: dict[str, int] = {}

    @property
    def bound(self) -> bool:
        return self._gate is not None

    @property
    def internal_actors(self) -> frozenset[str]:
        """Reserved daemon actor names exempt from link governance (rulings 45/63)."""

        return self._internal_actors

    @property
    def gate(self) -> LinkGate | None:
        """The bound gate, or None while the slot is unbound (WP2 wiring point)."""

        return self._gate

    def current_gate(self) -> LinkGate | None:
        """Read-only accessor for callers that must not take the one slot."""

        return self._gate

    def governs(self, sender: str, recipient: str) -> bool:
        """Read-only governance predicate (no gate call, no W reservation)."""

        return link_governed(
            sender, recipient, internal_actors=self._internal_actors
        )

    def bind(self, gate: LinkGate) -> None:
        """Bind the gate once.  A second bind is a wiring defect, not a race."""

        if not isinstance(gate, LinkGate):
            raise TypeError("link gate must implement the LinkGate protocol")
        if self._gate is not None:
            raise RuntimeError("link gate is already bound")
        self._gate = gate

    def ingress_refusal(
        self, *, sender: str, recipient: str, intent: str
    ) -> str | None:
        """The refusal code for ingress, or None to accept (§4.6).

        Unbound and gate failures accept: refusing is a terminal statement
        that this sender never joined, and a local wiring gap must not mint it
        (review B4 / §2.2).
        """

        del intent
        if not link_governed(
            sender, recipient, internal_actors=self._internal_actors
        ):
            return None
        gate = self._gate
        if gate is None:
            self._log_gate_unbound(recipient)
            return None
        try:
            known = gate.sender_known(recipient=recipient, sender=sender)
        except Exception as error:  # noqa: BLE001 - fail open at ingress
            self._log_gate_error(
                stage="ingress",
                sender=sender,
                recipient=recipient,
                error=error,
            )
            return None
        return None if known else ipc_errors.LINK_UNKNOWN

    def dispatch_admitted(
        self,
        *,
        sender: str,
        recipient: str,
        intent: str,
        message_id: str,
        now_ms: int,
        reply_to: str | None,
    ) -> bool:
        """May this message enter the recipient's context? (§4.6, §5.3)

        Governed replies go through the gate exactly like requests (review B1):
        whether the correlation names a request this node still holds open, and
        whether the link is still effective, is policy's call, not the call
        site's.  An unbound or failing gate holds (fail closed).
        """

        if not link_governed(
            sender, recipient, internal_actors=self._internal_actors
        ):
            return True
        gate = self._gate
        if gate is None:
            self._log_gate_unbound(recipient)
            self._record_held(recipient, message_id, now_ms)
            return False
        try:
            admitted = gate.admit_dispatch(
                recipient=recipient,
                sender=sender,
                message_id=message_id,
                now_ms=now_ms,
                intent=intent,
                reply_to=reply_to,
            )
        except Exception as error:  # noqa: BLE001 - fail closed at dispatch
            self._log_gate_error(
                stage="dispatch",
                sender=sender,
                recipient=recipient,
                error=error,
            )
            self._record_held(recipient, message_id, now_ms)
            return False
        if admitted:
            self._record_admitted(recipient, message_id, now_ms)
            return True
        self._record_held(recipient, message_id, now_ms)
        return False

    def forget(self, message_id: str) -> None:
        """Read-only-adjacent: drop both admission memories for one id.

        The inbox projection calls this once a row is consumed or expired so
        the B6 count cannot be pinned by stale in-memory state.
        """

        self._held.pop(message_id, None)
        self._admitted.pop(message_id, None)

    def held_message_ids(self, recipient: str) -> frozenset[str]:
        """Snapshot of ids currently held for one recipient (B6, read-only)."""

        return frozenset(
            message_id
            for message_id, (held_recipient, _) in tuple(self._held.items())
            if held_recipient == recipient
        )

    def admitted_message_ids(self, recipient: str) -> frozenset[str]:
        """Read-only snapshot of admitted-but-not-yet-consumed ids (B6)."""

        return frozenset(
            message_id
            for message_id, admitted_recipient in tuple(self._admitted.items())
            if admitted_recipient == recipient
        )

    def _record_held(self, recipient: str, message_id: str, now_ms: int) -> None:
        self._admitted.pop(message_id, None)
        existing = self._held.get(message_id)
        if existing is not None:
            return
        self._held[message_id] = (recipient, now_ms)
        while len(self._held) > _LINK_HELD_LIMIT:
            self._held.popitem(last=False)
        self._log("info", LINK_HELD_EVENT, recipient=recipient, messageId=message_id)

    def _record_admitted(self, recipient: str, message_id: str, now_ms: int) -> None:
        if self._admitted.get(message_id) != recipient:
            self._admitted[message_id] = recipient
            while len(self._admitted) > _LINK_HELD_LIMIT:
                self._admitted.popitem(last=False)
        existing = self._held.pop(message_id, None)
        if existing is None:
            return
        self._log(
            "info",
            LINK_ADMITTED_EVENT,
            recipient=recipient,
            messageId=message_id,
            heldMs=max(0, now_ms - existing[1]),
        )

    def _log_gate_unbound(self, recipient: str) -> None:
        now_mono = time.monotonic_ns() // 1_000_000
        last = self._unbound_logged_at_ms.get(recipient)
        if last is not None and now_mono - last < _LINK_GATE_UNBOUND_LOG_INTERVAL_MS:
            return
        self._unbound_logged_at_ms[recipient] = now_mono
        self._log("warn", LINK_GATE_UNBOUND_EVENT, recipient=recipient)

    def _log_gate_error(
        self,
        *,
        stage: str,
        sender: str,
        recipient: str,
        error: BaseException,
    ) -> None:
        self._log(
            "warn",
            LINK_GATE_ERROR_EVENT,
            stage=stage,
            sender=sender,
            recipient=recipient,
            errorType=type(error).__name__,
        )

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, "daemon", event, **fields)
        except Exception:  # noqa: BLE001 - logging cannot break admission
            pass


__all__ = [
    "DISPATCH_ROLES",
    "DISPATCH_SERVICE_ACTOR_NAME",
    "LINK_ADMITTED_EVENT",
    "LINK_GATE_ERROR_EVENT",
    "LINK_GATE_UNBOUND_EVENT",
    "LINK_HELD_EVENT",
    "LinkAdmission",
    "dispatch_gate",
    "link_governed",
]
