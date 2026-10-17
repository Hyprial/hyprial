"""Send-link seams: the names every other package codes against (WP0).

Design: docs/design/design-send-links-2026-10-14.md §14.  This module fixes
the protocols, decision types, IPC method names, hook event names and the
summary budget so WP1 (model/controller/policy), WP2 (store/service), WP3
(inbox), WP4 (dispatch), WP5 (carriers), WP6 (CLI/MCP) and WP7 (reminder
hook) can be built in parallel against one vocabulary.  It holds no
behaviour: a ``LinkGate`` implementation lives in ``service.py`` (WP2) and
is injected at composition; ``inbox`` and ``dispatch`` only ever import
this module, never the service, so there is no inbox <-> links cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from hyprial.kernel import ipc_errors

__all__ = [
    "Allow",
    "EVENT_LINK_MINTED",
    "EVENT_LINK_RECLAIMED",
    "HOLD_REASONS",
    "Hold",
    "IPC_LINK_ADD",
    "IPC_LINK_DROP",
    "IPC_LINK_LIST",
    "IPC_LINK_STATS",
    "IPC_MESSAGE_READ_BODY",
    "IPC_MESSAGE_SETTLE_HINT",
    "LINK_DIR",
    "HOLD_REASON_KEY",
    "LinkAdmin",
    "LinkChangeSource",
    "LinkGate",
    "LinkQueries",
    "LinkSendObserver",
    "SUMMARY_REQUIRED_DATA_KEYS",
    "TOP_FIELD_CAPACITY_W",
    "TOP_FIELD_INBOUND_AWAITING_INJECTION",
    "TOP_FIELD_OUTBOUND_IN_FLIGHT",
    "LinkState",
    "Mint",
    "SUMMARY_MAX_BYTES",
    "SendDecision",
    "SettleHint",
]

#: orgfs directory under the org space holding one file per agent (§6).
LINK_DIR = "link"
#: Hard cap on ``summary`` in UTF-8 bytes (§7).  Over it: SUMMARY_REQUIRED
#: with the measured size in ``data``; never truncated silently.
SUMMARY_MAX_BYTES = 512

LinkState = Literal["pending", "established", "reclaimed"]
#: What the daemon answers a carrier at turn end (§8.1): ``ack`` when the
#: injected message is no longer pending (replied, acked or drained), or when
#: the row is not a request (a reply or event owes nothing back), else
#: ``keep``.  A carrier never replies on its own (ruling 56).
SettleHint = Literal["ack", "keep"]

#: The three hold reasons.  A hold is ``ok: true, queued: true`` (§4.3).
HOLD_REASONS: frozenset[str] = frozenset(
    {
        ipc_errors.LINK_MINTING,
        ipc_errors.LINK_PEER_FULL,
        ipc_errors.LINK_SELF_FULL,
        ipc_errors.LINK_WINDOW_FULL,
    }
)
#: IPC result key carrying a hold code.  Deliberately not ``reason``: that
#: key already carries free-text strings such as "target-not-visible".
HOLD_REASON_KEY = "holdReason"
#: ``data`` shape of SUMMARY_REQUIRED when the summary is present but over
#: budget: {"summaryBytes": <measured>, "maxBytes": SUMMARY_MAX_BYTES}.
SUMMARY_REQUIRED_DATA_KEYS = ("summaryBytes", "maxBytes")
#: ``top`` snapshot fields (§9): WP2 provides the first two, WP4 the third.
TOP_FIELD_CAPACITY_W = "capacityW"
TOP_FIELD_OUTBOUND_IN_FLIGHT = "outboundInFlight"
TOP_FIELD_INBOUND_AWAITING_INJECTION = "inboundAwaitingInjection"

EVENT_LINK_MINTED = "link.minted"
EVENT_LINK_RECLAIMED = "link.reclaimed"

# IPC method names.  WP3 registers message.* handlers, WP6 registers link.*;
# the router's allow-list and contract/ pins must use these constants.
IPC_LINK_LIST = "link.list"
IPC_LINK_STATS = "link.stats"
IPC_LINK_ADD = "link.add"
IPC_LINK_DROP = "link.drop"
IPC_MESSAGE_READ_BODY = "message.read_body"
IPC_MESSAGE_SETTLE_HINT = "message.settle_hint"


@dataclass(frozen=True, slots=True)
class Allow:
    """Deliver now.  ``peer`` is the canonical counterpart, or None for an
    owner or route target that the link mechanism does not govern.

    ``window_observed`` names the W/cwnd bound the decision would have held on
    under ``enforce`` (H1).  It is empty under ``enforce`` and for every
    ungoverned or in-window send: observe mode delivers and only counts."""

    peer: str | None = None
    window_observed: str = ""


@dataclass(frozen=True, slots=True)
class Mint:
    """No effective link yet: the daemon writes the sender's one-sided
    reference and holds the message until the peer references back (§4.2).
    ``evict`` names the least-recently-used link the sender must drop to make
    room (policy picked it; the store must not pick again), or None."""

    peer: str
    evict: str | None = None


@dataclass(frozen=True, slots=True)
class Hold:
    """Keep the message in the sender's outbox; ``reason`` is one of
    HOLD_REASONS and ``detail`` names the bound that was hit."""

    reason: str
    peer: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason not in HOLD_REASONS:
            raise ValueError(f"not a hold reason: {self.reason!r}")


SendDecision = Allow | Mint | Hold


@runtime_checkable
class LinkGate(Protocol):
    """What the send boundary, ingress and dispatch ask before acting.

    Threading contract: ``sender_known`` and ``admit_dispatch`` are called on
    the inbox actor thread, partly inside a SQLite transaction.  They must
    answer from memory and never block, do I/O or take locks the inbox
    actor could be waiting on.  Only canonical agent-to-agent messages are
    governed; owner ``user:`` traffic, adapter/route inbound, system and
    daemon-internal actors and self-sends bypass the gate at the call site.
    """

    def admit_send(
        self,
        *,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        now_ms: int,
    ) -> SendDecision: ...

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        """Ingress (§4.6): does the local orgfs copy hold any link file for
        ``sender``?  False means the sender never joined: LINK_UNKNOWN.
        Never asked for ``user:`` senders (people have no link file)."""
        ...

    def admit_dispatch(
        self,
        *,
        recipient: str,
        sender: str,
        message_id: str,
        now_ms: int,
        intent: str = "request",
        reply_to: str | None = None,
    ) -> bool:
        """Dispatch (§4.6, §5.3): the mutual reference is visible locally and
        ``recipient`` has W headroom.  False keeps the row in the inbox.

        A reply (``intent == "reply"`` with a ``reply_to`` naming a request
        this node still has open) is admitted regardless of W (§4.5): it
        closes an open item, and gating it could deadlock a node whose W is
        full of outbound requests waiting for exactly these replies.  The
        exemption lives in policy, not at the call site.

        Idempotent per ``(recipient, message_id)``: once True it stays True
        until the message settles and never counts against W twice.  It is
        re-asked on every runtime tick and on every pending list/wait."""
        ...

    def settle_hint(self, *, actor: str, message_id: str) -> SettleHint:
        """Only ever asked about a message the carrier injected and that may
        still be pending; ``ack`` when it is no longer pending or is not a
        request (reply/event rows owe no answer), else ``keep``.  Never
        changes state."""
        ...


@runtime_checkable
class LinkSendObserver(Protocol):
    """Optional: the send boundary tells the link service what it did with
    an admitted or held message, so open-request counts and hold tracking
    use the real message id (``admit_send`` is asked before the id exists).
    ``hold_reason`` is None for a delivered send, else one of HOLD_REASONS."""

    def submitted(
        self,
        *,
        sender: str,
        target: str,
        message_id: str,
        reply_to: str | None,
        hold_reason: str | None,
        now_ms: int,
    ) -> None: ...


@runtime_checkable
class LinkChangeSource(Protocol):
    """Optional wake-up: carriers waiting on ``message.pending.wait`` may
    subscribe so a link or window change is seen at once instead of within
    the next poll.  The callback must be cheap and thread-safe."""

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]: ...


@runtime_checkable
class LinkQueries(Protocol):
    """Read-only views behind ``link.list`` / ``link.stats`` (§9)."""

    def list_links(self, actor: str) -> Mapping[str, Any]: ...

    def stats(self, actor: str) -> Mapping[str, Any]: ...


@runtime_checkable
class LinkAdmin(Protocol):
    """Operator mutations behind ``link.add`` / ``link.drop`` (§9).

    ``add`` writes the one-sided reference without sending; the result says
    whether the link is already effective.  ``drop`` reclaims (§4.4)."""

    def add(self, *, actor: str, peer: str, now_ms: int) -> Mapping[str, Any]: ...

    def drop(self, *, actor: str, peer: str, now_ms: int) -> Mapping[str, Any]: ...
