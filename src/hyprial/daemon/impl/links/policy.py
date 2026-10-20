"""Send admission, eviction, replies and reclaim residue (WP1).

Design: docs/design/design-send-links-2026-10-14.md §4; implementation spec:
docs/design/send-links-impl/wp1.md §1.3 with the review rulings B1–B12.  Pure
decision functions over a :class:`NodeView` snapshot that WP2 assembles from
its in-memory state; nothing here reads files, orgfs or the inbox.

Refusals stay values only where the seam has values (``ports.SendDecision``);
a refused reply raises the kernel's ``DaemonRequestError`` exactly like the
existing IPC rejection channel.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from hyprial.daemon.impl.links.controller import (
    DEFAULTS,
    ControllerConstants,
    ControllerOutput,
    abandon_threshold_ms,
)
from hyprial.daemon.impl.links.model import (
    Link,
    Lifecycle,
    effective,
    is_link_endpoint,
    is_owner_pair,
)
from hyprial.daemon.impl.links.ports import (
    Allow,
    Hold,
    Mint,
    SendDecision,
    SettleHint,
)
from hyprial.kernel import DaemonRequestError, ipc_errors, uri_scheme

#: H1 window switch: ``observe`` (the daemon default) counts a full W/cwnd and
#: lets the message through, ``enforce`` restores the holding table.
WindowMode = Literal["observe", "enforce"]
#: ``wouldHoldReason`` of a recipient-side dispatch the bypass admitted: the
#: sender's link is not effective yet (pending, minting or absent).
DISPATCH_UNLINKED = ipc_errors.LINK_MINTING

__all__ = [
    "NodeView",
    "OutboundRequest",
    "PendingRequest",
    "Residue",
    "WindowMode",
    "abandoned",
    "accept_reference",
    "admit",
    "admit_dispatch",
    "bypass_send",
    "dispatch_bypassed",
    "censored_inbound",
    "churning",
    "degree",
    "dispatch_window_observed",
    "governed",
    "request_threshold_ms",
    "residue",
    "select_victim",
    "settle_hint",
    "shrink_to_k",
    "unclosed",
]


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """An inbound request injected into context and not yet replied to."""

    message_id: str
    requester: str
    received_at_ms: int


@dataclass(frozen=True, slots=True)
class OutboundRequest:
    """An outbound request with no received reply and not yet abandoned."""

    message_id: str
    peer: str
    sent_at_ms: int


@dataclass(frozen=True, slots=True)
class Residue:
    """What reclaiming one link leaves behind (§4.4)."""

    peer: str
    drain_ack: tuple[str, ...]
    released_outbound: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NodeView:
    """One actor's local snapshot, assembled by WP2 before every decision.

    ``admitted_ids`` (B1) is the set of message ids this actor already
    admitted and has not settled; it is not derivable from
    ``pending_inbound`` alone because replies are admitted too.  ``peer_full``
    (B3) and ``org_available`` (B12) are facts WP2 supplies; policy never
    queries another node or orgfs.
    """

    actor: str
    links: Mapping[str, Link]
    pending_inbound: Mapping[str, PendingRequest]
    drained: AbstractSet[str]
    outbound: Sequence[OutboundRequest]
    limits: ControllerOutput
    recent_reclaims_ms: Sequence[int]
    rtt_max_ms: Mapping[str, int]
    org_available: bool
    constants: ControllerConstants = DEFAULTS
    admitted_ids: AbstractSet[str] = frozenset()
    peer_full: AbstractSet[str] = frozenset()
    #: H1: ``observe`` (the daemon default) counts the W/cwnd bounds and lets
    #: the message through; ``enforce`` restores the holding decision table.
    window_mode: WindowMode = "observe"
    #: Emergency switch ``HYPRIAL_LINKS_BYPASS``: every link-state hold or
    #: dispatch refusal admits instead (see :func:`bypass_send`).
    bypass: bool = False

    def __post_init__(self) -> None:
        if not is_link_endpoint(self.actor):
            raise ValueError(f"node view actor is not an endpoint: {self.actor!r}")
        object.__setattr__(self, "links", MappingProxyType(dict(self.links)))
        object.__setattr__(
            self, "pending_inbound", MappingProxyType(dict(self.pending_inbound))
        )
        object.__setattr__(self, "rtt_max_ms", MappingProxyType(dict(self.rtt_max_ms)))
        object.__setattr__(self, "drained", frozenset(self.drained))
        object.__setattr__(self, "admitted_ids", frozenset(self.admitted_ids))
        object.__setattr__(self, "peer_full", frozenset(self.peer_full))
        object.__setattr__(self, "outbound", tuple(self.outbound))
        object.__setattr__(self, "recent_reclaims_ms", tuple(self.recent_reclaims_ms))


def governed(sender: str, target: str) -> bool:
    """Outbound rule: is this (sender, target) pair subject to links?

    Owner pairs, route targets, self-sends, non-address targets and the
    system origin bypass the mechanism (§3.2, §4.1).  This is the outbound
    classification only; the inbound direction is decided separately in
    :func:`admit_dispatch` (review B12).
    """

    if sender == target:
        return False
    if uri_scheme(sender) == "system":
        return False
    # Ruling 37/44: every ``user:`` target (owner and other people) bypasses
    # the link check.  Delivery goes through squire/user-proxy or the adapter;
    # the open request still counts against the sender's W.
    if uri_scheme(target) == "user":
        return False
    if not is_link_endpoint(sender) or not is_link_endpoint(target):
        return False
    return not is_owner_pair(sender, target)


def _inbound_governed(sender: str, actor: str) -> bool:
    """Inbound rule (README #25): only agent senders need a link.

    Non-owner ``user:``, ``adapter:``, ``channel:``, ``system:``, daemon
    internal and self sends are exempt: a person has no link file to write.
    """

    if sender == actor:
        return False
    return uri_scheme(sender) == "agent"


def degree(view: NodeView) -> int:
    """Regular links this actor referenced: what counts against k (B4).

    Only my own references count, only regular lifecycle; PAC-bound links
    carry a task's authorization instead of the regular k budget.  A one-sided
    reference I wrote is included (it still occupies a slot until reclaimed).
    """

    total = 0
    for peer, link in view.links.items():
        if is_owner_pair(view.actor, peer) or not link.mine:
            continue
        if link.state == "reclaimed" or link.entry is None:
            continue
        if link.entry.lifecycle == "regular":
            total += 1
    return total


def unclosed(view: NodeView) -> int:
    """Open items the node carries: inbound awaiting reply + outbound (§2)."""

    return len(view.pending_inbound) + len(view.outbound)


def _inflight(view: NodeView, peer: str) -> int:
    return sum(1 for request in view.outbound if request.peer == peer)


def _has_pending_from(view: NodeView, peer: str) -> bool:
    return any(request.requester == peer for request in view.pending_inbound.values())


def _waiting_reason(view: NodeView, target: str) -> str:
    """Why a one-sided reference is still waiting (B3): never a guess.

    Only a known peer-capacity fact says ``LINK_PEER_FULL``; an unknown peer
    or a missing file copy stays ``LINK_MINTING``.
    """

    if target in view.peer_full:
        return ipc_errors.LINK_PEER_FULL
    return ipc_errors.LINK_MINTING


def churning(view: NodeView, *, now_ms: int) -> bool:
    """True while reclaims in the last period exceed the churn budget (B10)."""

    period = view.limits.period_ms
    if period is None:
        return False
    limit = view.constants.churn_limit_per_period or view.limits.k
    count = sum(1 for at in view.recent_reclaims_ms if now_ms - period <= at <= now_ms)
    return count > limit


def select_victim(view: NodeView, *, now_ms: int) -> str | None:
    """The LRU regular link an automatic mint may evict (§4.2).

    Only idle, past-minimum-lifetime links qualify; PAC-bound links never do.
    ``period_ms is None`` means the controller has no baseline, and then no
    link is evicted at all (A4).
    """

    period = view.limits.period_ms
    # With no controller baseline (the production cold start), the minimum
    # lifetime cannot be computed; an idle link is still evictable, and its
    # effective last-use is at least its minting time.
    lifetime = 0 if period is None else view.constants.min_link_lifetime_periods * period
    candidates: list[tuple[int, str]] = []
    for peer, link in view.links.items():
        if is_owner_pair(view.actor, peer) or not link.mine:
            continue
        entry = link.entry
        if entry is None or entry.lifecycle != "regular":
            continue
        if link.state == "reclaimed":
            continue
        if now_ms - entry.minted_at_ms < lifetime:
            continue
        if _inflight(view, peer) or _has_pending_from(view, peer):
            continue
        candidates.append((entry.last_used_ms or entry.minted_at_ms, peer))
    if not candidates:
        return None
    return min(candidates)[1]


def shrink_to_k(view: NodeView, *, now_ms: int) -> tuple[str, ...]:
    """Reclaim down to the topology budget after k dropped (B11).

    Unlike :func:`select_victim`, a hard-cap reclaim ignores the minimum
    lifetime and prefers idle links before ones with in-flight requests; a
    link can therefore be reclaimed young, with the residue released by
    :func:`residue`.  PAC-bound links are never candidates.
    """

    del now_ms
    regular: list[tuple[str, int]] = []
    for peer, link in view.links.items():
        if is_owner_pair(view.actor, peer) or not link.mine:
            continue
        entry = link.entry
        if entry is None or entry.lifecycle != "regular":
            continue
        if link.state == "reclaimed":
            continue
        regular.append((peer, entry.last_used_ms))
    surplus = len(regular) - view.limits.k
    if surplus <= 0:
        return ()
    idle: list[tuple[int, str]] = []
    busy: list[tuple[int, str]] = []
    for peer, last_used_ms in regular:
        key = (last_used_ms, peer)
        if _inflight(view, peer) or _has_pending_from(view, peer):
            busy.append(key)
        else:
            idle.append(key)
    ordered = sorted(idle) + sorted(busy)
    return tuple(peer for _, peer in ordered[:surplus])


def request_threshold_ms(view: NodeView, peer: str) -> int | None:
    """Abandon threshold for one peer, with the node-T_max fallback (B9).

    The link's own RTT_max first; otherwise the node's window T_max, so a
    request on a link this node never measured can still expire.  With no
    baseline at all there is no threshold and nothing is fabricated.
    """

    rtt_max = view.rtt_max_ms.get(peer)
    if rtt_max is not None:
        return abandon_threshold_ms(rtt_max, view.constants)
    node_t_max = view.limits.node.t_max_ms
    if node_t_max is not None:
        return abandon_threshold_ms(node_t_max, view.constants)
    return None


def abandoned(view: NodeView, *, now_ms: int) -> tuple[str, ...]:
    """Outbound request ids past their threshold.  Release only, never resend."""

    released: list[str] = []
    for request in view.outbound:
        threshold = request_threshold_ms(view, request.peer)
        if threshold is None:
            continue
        if now_ms - request.sent_at_ms >= threshold:
            released.append(request.message_id)
    return tuple(released)


def censored_inbound(view: NodeView, *, now_ms: int) -> Mapping[str, int]:
    """Pending inbound ids past their threshold, valued at the threshold (B9)."""

    censored: dict[str, int] = {}
    for message_id, request in view.pending_inbound.items():
        threshold = request_threshold_ms(view, request.requester)
        if threshold is None:
            continue
        if now_ms - request.received_at_ms >= threshold:
            censored[message_id] = threshold
    return MappingProxyType(censored)


def residue(view: NodeView, *, peer: str) -> Residue:
    """What reclaiming ``peer`` drains and releases (§4.4)."""

    drain_ack = tuple(
        message_id
        for _, message_id in sorted(
            (request.received_at_ms, message_id)
            for message_id, request in view.pending_inbound.items()
            if request.requester == peer
        )
    )
    released = tuple(
        request.message_id
        for request in sorted(
            (item for item in view.outbound if item.peer == peer),
            key=lambda item: (item.sent_at_ms, item.message_id),
        )
    )
    return Residue(peer=peer, drain_ack=drain_ack, released_outbound=released)


def accept_reference(
    view: NodeView,
    *,
    requester: str,
    now_ms: int,
    lifecycle: Lifecycle = "regular",
) -> bool:
    """May the minted side write its half of the reference back? (§4.2, B4/B10)

    An existing own reference is idempotently accepted.  A PAC-bound
    reference is authorized by its task end, not by the regular k budget.
    Only a new regular reference checks churn and the k headroom, and the
    minted side never evicts a link to accept (§4.2).
    """

    link = view.links.get(requester)
    if link is not None and link.mine and link.state != "reclaimed":
        return True
    if lifecycle == "pac_task":
        return True
    if not view.org_available:
        return False
    if churning(view, now_ms=now_ms):
        return False
    return degree(view) < view.limits.k


def settle_hint(view: NodeView, *, message_id: str) -> SettleHint:
    """The carrier's turn-end answer: ``keep`` only while still pending (§8.1)."""

    if message_id in view.drained:
        return "ack"
    if message_id in view.pending_inbound:
        return "keep"
    return "ack"


def _reclaimed(message_id: str, peer: str) -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.LINK_RECLAIMED,
        "the link for this reply has been reclaimed; the residue is drained",
        data={"message_id": message_id, "peer": peer},
    )


def _admit_reply(view: NodeView, target: str, reply_to: str) -> SendDecision:
    """Replies: existence and target first, then the link (B2)."""

    if reply_to in view.drained:
        raise _reclaimed(reply_to, target)
    request = view.pending_inbound.get(reply_to)
    if request is None:
        raise DaemonRequestError(
            ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
            f"no open request {reply_to!r} to reply to",
        )
    if request.requester != target:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"request {reply_to!r} came from {request.requester!r}, not {target!r}",
        )
    if not governed(view.actor, target):
        return Allow(peer=None)
    link = view.links.get(target)
    if link is not None and link.state == "reclaimed":
        raise _reclaimed(reply_to, target)
    if effective(link):
        return Allow(peer=target)
    return Hold(_waiting_reason(view, target), target, "awaiting-peer")


def admit(
    view: NodeView,
    *,
    sender: str,
    target: str,
    intent: str,
    reply_to: str | None,
    now_ms: int,
) -> SendDecision:
    """The one send decision table (§4.1–§4.3, B1/B2/B3/B10/B12).

    ``intent`` is accepted for seam parity but never inspected: only an
    explicit ``reply_to`` makes a message a reply (R17).
    """

    del intent
    if sender != view.actor:
        raise ValueError(f"sender {sender!r} is not the view actor {view.actor!r}")
    decision = _admit_table(view, target=target, reply_to=reply_to, now_ms=now_ms)
    return bypass_send(view, decision, target) if view.bypass else decision


def bypass_send(view: NodeView, decision: SendDecision, target: str) -> SendDecision:
    """Turn a link-state hold into a delivery (emergency switch).

    A ``Hold`` (minting, peer full, self full, org unavailable, churn, k,
    window) becomes ``Allow(peer=target, bypassed=<reason>)``.  A ``Mint``
    delivers without bookkeeping (``mint=False``): with the bypass on the
    store freezes link-file writes, so no reference is staged or committed.
    Refusals that are not link holds (reply validation, reclaimed residue)
    are raised before this point and stay refusals.
    """

    del view
    if isinstance(decision, Hold):
        return Allow(peer=target, bypassed=decision.reason)
    if isinstance(decision, Mint):
        if decision.evict is not None:
            return Allow(peer=target, bypassed=ipc_errors.LINK_SELF_FULL)
        return Allow(peer=target, bypassed=ipc_errors.LINK_MINTING)
    return decision


def _admit_table(
    view: NodeView, *, target: str, reply_to: str | None, now_ms: int
) -> SendDecision:
    sender = view.actor
    if reply_to:
        return _admit_reply(view, target, reply_to)
    if not governed(sender, target):
        return Allow(peer=None)
    if not view.org_available:
        return Hold(ipc_errors.LINK_SELF_FULL, target, "org-unavailable")
    link = view.links.get(target)
    if effective(link):
        cwnd = view.limits.cwnd.get(target, 1)
        if _inflight(view, target) >= cwnd:
            if view.window_mode == "enforce":
                return Hold(ipc_errors.LINK_WINDOW_FULL, target, "cwnd")
            return Allow(peer=target, window_observed="cwnd")
        if unclosed(view) >= view.limits.w:
            if view.window_mode == "enforce":
                return Hold(ipc_errors.LINK_WINDOW_FULL, target, "W")
            return Allow(peer=target, window_observed="W")
        return Allow(peer=target)
    if link is not None and link.mine and link.state == "pending":
        return Hold(_waiting_reason(view, target), target, "awaiting-peer")
    if churning(view, now_ms=now_ms):
        return Hold(ipc_errors.LINK_SELF_FULL, target, "churn")
    if degree(view) < view.limits.k:
        return Mint(peer=target)
    victim = select_victim(view, now_ms=now_ms)
    if victim is not None:
        return Mint(peer=target, evict=victim)
    return Hold(ipc_errors.LINK_SELF_FULL, target, "k")


def admit_dispatch(
    view: NodeView,
    *,
    sender: str,
    message_id: str,
    now_ms: int,
    intent: str = "request",
    reply_to: str | None = None,
) -> bool:
    """Dispatch admission: link visible, W headroom, replies exempt (B1).

    Idempotent per message id: once admitted it stays admitted (and keeps
    counting once) until WP2 settles it.  Exempt inbound origins (owner,
    non-owner ``user:``, adapter/channel/system, self) pass at once.
    """

    del now_ms
    if message_id in view.admitted_ids:
        return True
    if not _inbound_governed(sender, view.actor):
        return True
    if not effective(view.links.get(sender)):
        return view.bypass
    if _reply_exempt(view, sender=sender, intent=intent, reply_to=reply_to):
        return True
    if unclosed(view) >= view.limits.w:
        # H1: observe admits and only counts the W bound; enforce holds.
        return view.window_mode != "enforce" or view.bypass
    return True


def dispatch_bypassed(
    view: NodeView,
    *,
    sender: str,
    message_id: str,
    intent: str = "request",
    reply_to: str | None = None,
) -> str:
    """The hold reason the bypass saved this dispatch from, or ``""``.

    Classifies only, like :func:`dispatch_window_observed`;
    ``admit_dispatch`` decides.
    """

    if not view.bypass or message_id in view.admitted_ids:
        return ""
    if not _inbound_governed(sender, view.actor):
        return ""
    if not effective(view.links.get(sender)):
        return DISPATCH_UNLINKED
    if _reply_exempt(view, sender=sender, intent=intent, reply_to=reply_to):
        return ""
    if view.window_mode == "enforce" and unclosed(view) >= view.limits.w:
        return ipc_errors.LINK_WINDOW_FULL
    return ""


def dispatch_window_observed(
    view: NodeView,
    *,
    sender: str,
    message_id: str,
    intent: str = "request",
    reply_to: str | None = None,
) -> bool:
    """True when observe mode saved admission from the W bound (H1).

    False under ``enforce``, for the exempt/unlinked/reply paths, and while W
    has headroom, so the caller can count a window observation and nothing
    else.  ``admit_dispatch`` itself decides; this only classifies.
    """

    if view.window_mode != "observe":
        return False
    if message_id in view.admitted_ids:
        return False
    if not _inbound_governed(sender, view.actor):
        return False
    if not effective(view.links.get(sender)):
        return False
    if _reply_exempt(view, sender=sender, intent=intent, reply_to=reply_to):
        return False
    return unclosed(view) >= view.limits.w


def _reply_exempt(
    view: NodeView, *, sender: str, intent: str, reply_to: str | None
) -> bool:
    """Ruling B1: a reply closing an open outbound request never counts to W."""

    if intent != "reply" or reply_to is None:
        return False
    return any(
        request.message_id == reply_to and request.peer == sender
        for request in view.outbound
    )
