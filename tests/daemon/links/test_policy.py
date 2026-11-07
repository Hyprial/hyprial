"""WP1 policy tests: admission, eviction, replies, residue, degradation.

Cases P1-P19 and D7-D12 of docs/design/send-links-impl/wp1.md §3.3, plus the
regression cases the review rulings B1-B12 add.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.controller import (
    DEFAULTS,
    ControllerConstants,
    ControllerOutput,
    WindowStats,
)
from hyprial.daemon.impl.links.model import Link, LinkEntry
from hyprial.daemon.impl.links.ports import Allow, HOLD_REASONS, Hold, Mint
from hyprial.kernel import DaemonRequestError, ipc_errors

A = "agent:o:m:a"
B = "agent:o:m:b"
C = "agent:o:m:c"
D = "agent:o:m:d"
OWNER = "user:o"
X = "agent:p:m:x"
U = "user:p"


def _limits(
    *,
    k: int = 2,
    w: int = 4,
    cwnd: dict[str, int] | None = None,
    period_ms: int | None = 10000,
    t_max_ms: int | None = None,
    w_target: int | None = None,
) -> ControllerOutput:
    node = WindowStats(
        n=10,
        t_min_ms=1000,
        t_max_ms=t_max_ms,
        t_bar_ms=1000.0,
        mu_max_per_ms=None,
        w_target=w_target,
        window_ms=10000,
    )
    return ControllerOutput(
        n_active=23,
        k=k,
        w=w,
        cwnd=MappingProxyType(dict(cwnd or {})),
        period_ms=period_ms,
        node=node,
        links=MappingProxyType({}),
        diff=0.0,
        grad=0,
    )


def _link(
    actor: str,
    peer: str,
    *,
    state: str = "established",
    mine: bool = True,
    theirs: bool = True,
    lifecycle: str = "regular",
    task_id: str | None = None,
    minted_at_ms: int = 0,
    last_used_ms: int = 0,
) -> Link:
    entry = None
    if mine:
        entry = LinkEntry(
            peer=peer,
            minted_at_ms=minted_at_ms,
            revision=1,
            lifecycle=lifecycle,  # type: ignore[arg-type]
            task_id=task_id,
            last_used_ms=last_used_ms,
        )
    return Link(
        actor=actor,
        peer=peer,
        state=state,  # type: ignore[arg-type]
        mine=mine,
        theirs=theirs,
        entry=entry,
    )


def _view(
    *,
    actor: str = A,
    links: tuple[Link, ...] = (),
    pending_inbound: tuple[policy.PendingRequest, ...] = (),
    drained: tuple[str, ...] = (),
    outbound: tuple[policy.OutboundRequest, ...] = (),
    limits: ControllerOutput | None = None,
    recent_reclaims_ms: tuple[int, ...] = (),
    rtt_max_ms: dict[str, int] | None = None,
    org_available: bool = True,
    admitted_ids: tuple[str, ...] = (),
    peer_full: tuple[str, ...] = (),
    constants: ControllerConstants = DEFAULTS,
    window_mode: str = "enforce",
) -> policy.NodeView:
    # The H1 switch defaults to ``observe`` in production (``NodeView``); these
    # cases pin the legacy decision table, so the harness asks for ``enforce``
    # explicitly.  The observe cases below pass ``window_mode="observe"``.
    return policy.NodeView(
        actor=actor,
        links={link.peer: link for link in links},
        pending_inbound={request.message_id: request for request in pending_inbound},
        drained=set(drained),
        outbound=outbound,
        limits=limits or _limits(),
        recent_reclaims_ms=recent_reclaims_ms,
        rtt_max_ms=rtt_max_ms or {},
        org_available=org_available,
        constants=constants,
        admitted_ids=set(admitted_ids),
        peer_full=set(peer_full),
        window_mode=window_mode,
    )


def _admit(view: policy.NodeView, *, target: str, intent: str = "request", reply_to: str | None = None, now_ms: int = 0):
    return policy.admit(
        view, sender=view.actor, target=target, intent=intent, reply_to=reply_to, now_ms=now_ms
    )


def test_p1_owner_and_route_targets_bypass_every_budget() -> None:
    view = _view(
        links=(_link(A, B), _link(A, C)),
        limits=_limits(k=2, w=0, cwnd={B: 0, C: 0}),
    )
    assert _admit(view, target=OWNER) == Allow(peer=None)
    assert _admit(view, target="route:lark:x") == Allow(peer=None)
    owner_view = _view(actor=OWNER, limits=_limits(k=0, w=0))
    assert (
        policy.admit(
            owner_view, sender=OWNER, target=A, intent="request", reply_to=None, now_ms=0
        )
        == Allow(peer=None)
    )


def test_p2_owner_links_do_not_consume_k() -> None:
    view = _view(links=(_link(A, B), _link(A, OWNER)), limits=_limits(k=2))
    assert policy.degree(view) == 1
    assert _admit(view, target=C) == Mint(peer=C)
    assert _admit(view, target=OWNER) == Allow(peer=None)


def test_p1_non_owner_user_targets_bypass_the_link_check() -> None:
    """Ruling 37/44: every ``user:`` target is exempt from link governance."""

    assert policy.governed(A, U) is False
    assert _admit(_view(limits=_limits(k=2)), target=U) == Allow(peer=None)
    assert _admit(_view(limits=_limits(k=2), org_available=False), target=U) == Allow(
        peer=None
    )


def test_p4_an_effective_link_with_headroom_allows() -> None:
    view = _view(links=(_link(A, B),), limits=_limits(k=2, w=4, cwnd={B: 2}))
    assert _admit(view, target=B) == Allow(peer=B)


def test_p5_window_bounds_hold_with_ports_reasons() -> None:
    cwnd_full = _view(
        links=(_link(A, B),),
        outbound=(policy.OutboundRequest("m1", B, 0), policy.OutboundRequest("m2", B, 0)),
        limits=_limits(k=2, w=4, cwnd={B: 2}),
    )
    assert _admit(cwnd_full, target=B) == Hold(
        ipc_errors.LINK_WINDOW_FULL, B, "cwnd"
    )
    w_full = _view(
        links=(_link(A, B),),
        outbound=tuple(policy.OutboundRequest(f"m{i}", C, i) for i in range(4)),
        limits=_limits(k=2, w=4, cwnd={B: 2}),
    )
    assert _admit(w_full, target=B) == Hold(ipc_errors.LINK_WINDOW_FULL, B, "W")
    with pytest.raises(ValueError):
        Hold(reason=ipc_errors.LINK_UNKNOWN, peer=B)
    assert {hold.reason for hold in (_admit(cwnd_full, target=B), _admit(w_full, target=B))} <= HOLD_REASONS


def test_p6b_reply_to_an_unsynced_link_holds_instead_of_refusing() -> None:
    request = policy.PendingRequest("m1", B, 0)
    unsynced = _view(
        links=(_link(A, B, state="pending", theirs=False),),
        pending_inbound=(request,),
        limits=_limits(k=2),
    )
    assert _admit(unsynced, target=B, intent="reply", reply_to="m1") == Hold(
        ipc_errors.LINK_MINTING, B, "awaiting-peer"
    )
    missing_copy = _view(pending_inbound=(request,), limits=_limits(k=2))
    assert _admit(missing_copy, target=B, intent="reply", reply_to="m1") == Hold(
        ipc_errors.LINK_MINTING, B, "awaiting-peer"
    )
    restored = _view(
        links=(_link(A, B),), pending_inbound=(request,), limits=_limits(k=2)
    )
    assert _admit(restored, target=B, intent="reply", reply_to="m1") == Allow(peer=B)


def test_p6_a_one_sided_reference_waits_without_reminting() -> None:
    pending = _view(
        links=(_link(A, B, state="pending", theirs=False),), limits=_limits(k=2)
    )
    assert _admit(pending, target=B) == Hold(
        ipc_errors.LINK_MINTING, B, "awaiting-peer"
    )
    known_full = _view(
        links=(_link(A, B, state="pending", theirs=False),),
        limits=_limits(k=2),
        peer_full=(B,),
    )
    assert _admit(known_full, target=B) == Hold(
        ipc_errors.LINK_PEER_FULL, B, "awaiting-peer"
    )


def test_p7_no_link_with_room_mints_including_a_peer_sided_pending() -> None:
    assert _admit(_view(limits=_limits(k=2)), target=C) == Mint(peer=C)
    peer_only = _view(
        links=(_link(A, C, state="pending", mine=False, theirs=True),),
        limits=_limits(k=2),
    )
    assert policy.degree(peer_only) == 0
    assert _admit(peer_only, target=C) == Mint(peer=C)


def test_p8_full_degree_evicts_the_lru_or_holds() -> None:
    old = _link(A, B, last_used_ms=0)
    recent = _link(A, C, last_used_ms=5000)
    view = _view(links=(old, recent), limits=_limits(k=2))
    assert policy.degree(view) == 2
    assert policy.select_victim(view, now_ms=20000) == B
    assert _admit(view, target=D, now_ms=20000) == Mint(peer=D, evict=B)
    busy = _view(
        links=(_link(A, B), _link(A, C)),
        outbound=(policy.OutboundRequest("m1", B, 0), policy.OutboundRequest("m2", C, 0)),
        limits=_limits(k=2),
    )
    assert policy.select_victim(busy, now_ms=20000) is None
    assert _admit(busy, target=D, now_ms=20000) == Hold(
        ipc_errors.LINK_SELF_FULL, D, "k"
    )


def test_p8b_full_degree_evicts_an_idle_link_without_a_baseline() -> None:
    """A full node with an idle regular link must still evict it, even when
    the controller has not produced a period baseline (the production cold
    start).  The idle link is the LRU by minting time.
    """

    idle = _link(A, B, minted_at_ms=1000, last_used_ms=0)
    view = _view(links=(idle,), limits=_limits(k=1, period_ms=None))

    assert policy.select_victim(view, now_ms=20000) == B
    assert _admit(view, target=C, now_ms=20000) == Mint(peer=C, evict=B)


def test_p9b_owner_exemption_does_not_skip_reply_validation() -> None:
    owner_view = _view(
        pending_inbound=(policy.PendingRequest("m1", OWNER, 0),), limits=_limits()
    )
    assert (
        _admit(owner_view, target=OWNER, intent="reply", reply_to="m1")
        == Allow(peer=None)
    )
    with pytest.raises(DaemonRequestError) as unknown:
        _admit(owner_view, target=OWNER, intent="reply", reply_to="ghost")
    assert unknown.value.code == ipc_errors.MESSAGE_REPLY_UNAVAILABLE
    with pytest.raises(DaemonRequestError) as wrong_target:
        _admit(owner_view, target=B, intent="reply", reply_to="m1")
    assert wrong_target.value.code == ipc_errors.INVALID_ARGUMENT
    settled_view = _view(limits=_limits())
    with pytest.raises(DaemonRequestError) as settled:
        _admit(settled_view, target=OWNER, intent="reply", reply_to="m1")
    assert settled.value.code == ipc_errors.MESSAGE_REPLY_UNAVAILABLE


def test_p9_replies_pass_a_full_cwnd_and_a_full_w() -> None:
    request = policy.PendingRequest("m1", B, 0)
    view = _view(
        links=(_link(A, B),),
        pending_inbound=(request,),
        outbound=tuple(policy.OutboundRequest(f"o{i}", C, 0) for i in range(4)),
        limits=_limits(k=2, w=1, cwnd={B: 0}),
    )
    assert _admit(view, target=B, intent="reply", reply_to="m1") == Allow(peer=B)
    owner_request = policy.PendingRequest("m2", OWNER, 0)
    owner_view = _view(
        pending_inbound=(owner_request,), limits=_limits(k=0, w=0)
    )
    assert (
        _admit(owner_view, target=OWNER, intent="reply", reply_to="m2")
        == Allow(peer=None)
    )


def test_p10_only_reply_to_makes_a_reply() -> None:
    request = policy.PendingRequest("m1", B, 0)
    view = _view(
        links=(_link(A, B),),
        pending_inbound=(request,),
        outbound=(policy.OutboundRequest("o1", B, 0), policy.OutboundRequest("o2", B, 0)),
        limits=_limits(k=2, w=4, cwnd={B: 2}),
    )
    assert _admit(view, target=B, intent="reply", reply_to=None) == Hold(
        ipc_errors.LINK_WINDOW_FULL, B, "cwnd"
    )
    assert _admit(view, target=B, intent="request", reply_to="m1") == Allow(peer=B)


def test_p11_reclaimed_and_drained_replies_refuse() -> None:
    request = policy.PendingRequest("m1", B, 0)
    reclaimed = _view(
        links=(_link(A, B, state="reclaimed"),),
        pending_inbound=(request,),
        limits=_limits(),
    )
    with pytest.raises(DaemonRequestError) as reclaimed_error:
        _admit(reclaimed, target=B, intent="reply", reply_to="m1")
    assert reclaimed_error.value.code == ipc_errors.LINK_RECLAIMED
    assert reclaimed_error.value.data["message_id"] == "m1"

    drained = _view(drained=("m1",), limits=_limits())
    with pytest.raises(DaemonRequestError) as drained_error:
        _admit(drained, target=B, intent="reply", reply_to="m1")
    assert drained_error.value.code == ipc_errors.LINK_RECLAIMED
    assert drained_error.value.data["message_id"] == "m1"

    regrown = _view(links=(_link(A, B),), drained=("m1",), limits=_limits())
    with pytest.raises(DaemonRequestError) as regrown_error:
        _admit(regrown, target=B, intent="reply", reply_to="m1")
    assert regrown_error.value.code == ipc_errors.LINK_RECLAIMED


def test_p12_unknown_or_mismatched_replies_refuse() -> None:
    with pytest.raises(DaemonRequestError) as unavailable:
        _admit(_view(limits=_limits()), target=B, intent="reply", reply_to="ghost")
    assert unavailable.value.code == ipc_errors.MESSAGE_REPLY_UNAVAILABLE

    mismatch = _view(
        pending_inbound=(policy.PendingRequest("m1", C, 0),), limits=_limits()
    )
    with pytest.raises(DaemonRequestError) as invalid:
        _admit(mismatch, target=B, intent="reply", reply_to="m1")
    assert invalid.value.code == ipc_errors.INVALID_ARGUMENT


def test_p13_admit_rejects_foreign_senders_and_returns_seam_decisions() -> None:
    view = _view(limits=_limits())
    with pytest.raises(ValueError):
        policy.admit(
            view, sender=B, target=C, intent="request", reply_to=None, now_ms=0
        )
    decisions = [
        _admit(view, target=C),
        _admit(_view(links=(_link(A, B),), limits=_limits(cwnd={B: 1})), target=B),
        _admit(view, target=OWNER),
    ]
    assert all(isinstance(decision, (Allow, Mint, Hold)) for decision in decisions)


def test_p14_admit_dispatch_is_idempotent_and_reply_aware() -> None:
    full = _view(
        pending_inbound=tuple(policy.PendingRequest(f"i{n}", C, 0) for n in range(4)),
        limits=_limits(k=2, w=4),
    )
    assert policy.admit_dispatch(full, sender=OWNER, message_id="m1", now_ms=0) is True

    pending = _view(
        links=(_link(A, B, state="pending", theirs=False),), limits=_limits(k=2, w=4)
    )
    assert policy.admit_dispatch(pending, sender=B, message_id="m2", now_ms=0) is False

    effective_full = _view(
        links=(_link(A, B),),
        pending_inbound=tuple(policy.PendingRequest(f"i{n}", C, 0) for n in range(4)),
        limits=_limits(k=2, w=4),
    )
    assert (
        policy.admit_dispatch(effective_full, sender=B, message_id="m3", now_ms=0)
        is False
    )

    admitted = _view(
        links=(_link(A, B, state="pending", theirs=False),),
        admitted_ids=("m4",),
        limits=_limits(k=2, w=4),
    )
    assert policy.admit_dispatch(admitted, sender=B, message_id="m4", now_ms=0) is True


def test_p14b_dispatch_counts_outbound_and_exempts_matching_replies() -> None:
    outbound_full = _view(
        links=(_link(A, B),),
        outbound=tuple(policy.OutboundRequest(f"o{n}", B, 0) for n in range(4)),
        limits=_limits(k=2, w=4),
    )
    assert (
        policy.admit_dispatch(outbound_full, sender=B, message_id="new", now_ms=0)
        is False
    )
    assert (
        policy.admit_dispatch(
            outbound_full,
            sender=B,
            message_id="r1",
            now_ms=0,
            intent="reply",
            reply_to="o0",
        )
        is True
    )
    assert (
        policy.admit_dispatch(
            outbound_full,
            sender=B,
            message_id="r2",
            now_ms=0,
            intent="reply",
            reply_to="ghost",
        )
        is False
    )
    assert (
        policy.admit_dispatch(
            outbound_full, sender=B, message_id="r3", now_ms=0, intent="reply"
        )
        is False
    )
    assert (
        policy.admit_dispatch(
            outbound_full,
            sender=C,
            message_id="r4",
            now_ms=0,
            intent="reply",
            reply_to="o0",
        )
        is False
    )
    last_slot = _view(
        links=(_link(A, B),),
        pending_inbound=tuple(policy.PendingRequest(f"i{n}", C, 0) for n in range(3)),
        limits=_limits(k=2, w=4),
    )
    assert policy.admit_dispatch(last_slot, sender=B, message_id="a", now_ms=0) is True
    taken = _view(
        links=(_link(A, B),),
        pending_inbound=tuple(policy.PendingRequest(f"i{n}", C, 0) for n in range(4)),
        limits=_limits(k=2, w=4),
    )
    assert policy.admit_dispatch(taken, sender=B, message_id="b", now_ms=0) is False


def test_p15_settle_hint_reports_pending_versus_drained() -> None:
    view = _view(
        pending_inbound=(policy.PendingRequest("m1", B, 0),),
        drained=("m2",),
        limits=_limits(),
    )
    assert policy.settle_hint(view, message_id="m1") == "keep"
    assert policy.settle_hint(view, message_id="m2") == "ack"
    assert policy.settle_hint(view, message_id="ghost") == "ack"


def test_p16_accept_reference_never_evicts_for_a_new_reference() -> None:
    space = _view(links=(_link(A, B),), limits=_limits(k=2))
    assert policy.accept_reference(space, requester=C, now_ms=0) is True
    full = _view(links=(_link(A, B), _link(A, C)), limits=_limits(k=2))
    assert policy.accept_reference(full, requester=D, now_ms=0) is False
    assert policy.accept_reference(full, requester=B, now_ms=0) is True
    churn = _view(
        links=(_link(A, B), _link(A, C)),
        limits=_limits(k=2),
        recent_reclaims_ms=(0, 100, 200, 300),
    )
    assert policy.churning(churn, now_ms=300) is True
    assert policy.accept_reference(churn, requester=D, now_ms=300) is False


def test_p17_residue_drains_inbound_and_releases_outbound() -> None:
    view = _view(
        pending_inbound=(
            policy.PendingRequest("m2", B, 20),
            policy.PendingRequest("m1", B, 10),
            policy.PendingRequest("m3", C, 5),
        ),
        outbound=(
            policy.OutboundRequest("o1", B, 1),
            policy.OutboundRequest("o2", C, 2),
            policy.OutboundRequest("o3", B, 3),
        ),
        limits=_limits(),
    )
    result = policy.residue(view, peer=B)
    assert result.drain_ack == ("m1", "m2")
    assert result.released_outbound == ("o1", "o3")


def test_p18_shrink_to_k_reclaims_even_with_in_flight_links() -> None:
    idle = _link(A, B, last_used_ms=1)
    busy = _link(A, C, last_used_ms=2)
    young = _link(A, D, last_used_ms=99, minted_at_ms=19999)
    view = _view(
        links=(idle, busy, young),
        outbound=(policy.OutboundRequest("m1", C, 0),),
        limits=_limits(k=2),
    )
    assert policy.shrink_to_k(view, now_ms=20000) == (B,)
    all_busy = _view(
        links=(_link(A, B), _link(A, C)),
        outbound=(policy.OutboundRequest("m1", B, 0), policy.OutboundRequest("m2", C, 0)),
        limits=_limits(k=1),
    )
    assert policy.shrink_to_k(all_busy, now_ms=20000) == (B,)
    pac_and_regular = _view(
        links=(
            _link(A, D, lifecycle="pac_task", task_id="t1"),
            _link(A, B, last_used_ms=50),
        ),
        limits=_limits(k=0),
    )
    assert policy.shrink_to_k(pac_and_regular, now_ms=20000) == (B,)


def test_p19_unclosed_counts_an_owner_request() -> None:
    view = _view(pending_inbound=(policy.PendingRequest("m1", OWNER, 0),))
    assert policy.unclosed(view) == 1


def test_d7_abandoned_releases_head_of_line_without_resending() -> None:
    view = _view(
        links=(_link(A, B),),
        outbound=(policy.OutboundRequest("m1", B, 0),),
        rtt_max_ms={B: 100},
        limits=_limits(k=2, w=4, cwnd={B: 1}),
    )
    assert policy.abandoned(view, now_ms=399) == ()
    assert policy.abandoned(view, now_ms=400) == ("m1",)
    assert _admit(view, target=B, now_ms=400) == Hold(
        ipc_errors.LINK_WINDOW_FULL, B, "cwnd"
    )
    released = _view(
        links=(_link(A, B),),
        rtt_max_ms={B: 100},
        limits=_limits(k=2, w=4, cwnd={B: 1}),
    )
    assert _admit(released, target=B, now_ms=400) == Allow(peer=B)
    assert all(isinstance(item, str) for item in policy.abandoned(view, now_ms=400))
    assert not any("resend" in name or "retry" in name for name in policy.__all__)


def test_d8_minimum_link_lifetime_blocks_automatic_eviction() -> None:
    young = _view(
        links=(_link(A, B, minted_at_ms=15000, last_used_ms=0),), limits=_limits(k=1)
    )
    assert policy.select_victim(young, now_ms=20000) is None
    assert _admit(young, target=C, now_ms=20000) == Hold(
        ipc_errors.LINK_SELF_FULL, C, "k"
    )
    old = _view(
        links=(_link(A, B, minted_at_ms=5000, last_used_ms=0),), limits=_limits(k=1)
    )
    assert policy.select_victim(old, now_ms=20000) == B


def test_d9_churn_holds_instead_of_minting_even_with_a_free_slot() -> None:
    hot = _view(
        limits=_limits(k=3), recent_reclaims_ms=(0, 100, 200, 300)
    )
    assert policy.degree(hot) == 0
    assert policy.churning(hot, now_ms=300) is True
    assert _admit(hot, target=C, now_ms=300) == Hold(
        ipc_errors.LINK_SELF_FULL, C, "churn"
    )
    boundary = _view(limits=_limits(k=3), recent_reclaims_ms=(0, 100, 200))
    assert policy.churning(boundary, now_ms=300) is False
    assert _admit(boundary, target=C, now_ms=300) == Mint(peer=C)
    cooled = _view(limits=_limits(k=3), recent_reclaims_ms=(0, 100, 200, 300))
    assert policy.churning(cooled, now_ms=50000) is False
    assert _admit(cooled, target=C, now_ms=50000) == Mint(peer=C)
    linked = _view(
        links=(_link(A, B),), limits=_limits(k=3), recent_reclaims_ms=(0, 100, 200, 300)
    )
    assert _admit(linked, target=B, now_ms=300) == Allow(peer=B)


def test_d10_lru_prefers_least_recent_and_skips_pac_links() -> None:
    first = _link(A, B, last_used_ms=10, minted_at_ms=0)
    second = _link(A, C, last_used_ms=10, minted_at_ms=0)
    assert policy.select_victim(_view(links=(first, second), limits=_limits(k=1)), now_ms=20000) == B
    pac = _link(A, B, lifecycle="pac_task", task_id="t1", last_used_ms=0, minted_at_ms=0)
    regular = _link(A, C, last_used_ms=99, minted_at_ms=0)
    assert policy.select_victim(_view(links=(pac, regular), limits=_limits(k=1)), now_ms=20000) == C


def test_d11_missing_peer_copy_degrades_to_pending_and_holds() -> None:
    stale = _view(
        links=(_link(A, B, state="pending", theirs=False),), limits=_limits(k=2, w=4)
    )
    decision = _admit(stale, target=B)
    assert decision == Hold(ipc_errors.LINK_MINTING, B, "awaiting-peer")
    assert not isinstance(decision, Mint)
    assert not isinstance(stale.links[B].state, type(None))
    assert stale.links[B].mine and not stale.links[B].theirs
    assert policy.admit_dispatch(stale, sender=B, message_id="m1", now_ms=0) is False


def test_d12_owner_messages_pass_when_every_budget_is_spent() -> None:
    view = _view(
        links=(_link(A, B), _link(A, C)),
        pending_inbound=tuple(policy.PendingRequest(f"i{n}", C, 0) for n in range(8)),
        outbound=tuple(policy.OutboundRequest(f"o{n}", C, 0) for n in range(4)),
        limits=_limits(k=2, w=4),
    )
    assert _admit(view, target=OWNER) == Allow(peer=None)
    assert policy.admit_dispatch(view, sender=OWNER, message_id="m1", now_ms=0) is True


def test_p20_pac_bound_links_do_not_consume_the_regular_k_budget() -> None:
    pac_one = _link(A, B, lifecycle="pac_task", task_id="t1")
    pac_two = _link(A, C, lifecycle="pac_task", task_id="t1")
    pac_three = _link(A, D, lifecycle="pac_task", task_id="t2")
    view = _view(links=(pac_one, pac_two, pac_three), limits=_limits(k=2))
    assert policy.degree(view) == 0
    assert _admit(view, target=X) == Mint(peer=X)
    assert policy.accept_reference(view, requester=X, now_ms=0) is True
    busy = _view(
        links=(_link(A, B, lifecycle="pac_task", task_id="t1"),),
        outbound=(policy.OutboundRequest("m1", B, 0),),
        limits=_limits(k=2, w=1, cwnd={B: 1}),
    )
    assert _admit(busy, target=B) == Hold(ipc_errors.LINK_WINDOW_FULL, B, "cwnd")
    ending = _view(
        links=(pac_one,),
        pending_inbound=(policy.PendingRequest("m1", B, 5),),
        outbound=(policy.OutboundRequest("o1", B, 1),),
    )
    result = policy.residue(ending, peer=B)
    assert result.drain_ack == ("m1",) and result.released_outbound == ("o1",)


def test_p24_unclosed_w_is_the_only_global_cap() -> None:
    def view_with(pending: tuple[policy.PendingRequest, ...]) -> policy.NodeView:
        return _view(
            links=(_link(A, B), _link(A, C)),
            pending_inbound=pending,
            limits=_limits(k=2, w=2, cwnd={B: 5, C: 5}),
        )

    assert _admit(view_with(()), target=B) == Allow(peer=B)
    assert _admit(
        view_with((policy.PendingRequest("m1", B, 0),)), target=C
    ) == Allow(peer=C)
    assert _admit(
        view_with(
            (policy.PendingRequest("m1", B, 0), policy.PendingRequest("m2", C, 0))
        ),
        target=B,
    ) == Hold(ipc_errors.LINK_WINDOW_FULL, B, "W")


def test_p25_pac_reference_is_accepted_even_when_regular_k_is_full() -> None:
    full = _view(links=(_link(A, B), _link(A, C)), limits=_limits(k=2))
    assert policy.degree(full) == 2
    assert policy.accept_reference(full, requester=D, now_ms=0) is False
    assert (
        policy.accept_reference(full, requester=D, now_ms=0, lifecycle="pac_task")
        is True
    )


def test_p21_org_unavailable_blocks_linking_but_not_owner_or_route() -> None:
    view = _view(limits=_limits(k=2), org_available=False)
    assert _admit(view, target=B) == Hold(
        ipc_errors.LINK_SELF_FULL, B, "org-unavailable"
    )
    assert _admit(view, target=U) == Allow(peer=None)
    assert _admit(view, target=OWNER) == Allow(peer=None)
    assert _admit(view, target="route:lark:x") == Allow(peer=None)
    assert policy.accept_reference(view, requester=B, now_ms=0) is False
    assert _admit(view, target=A) == Allow(peer=None)
    assert policy.admit_dispatch(view, sender=U, message_id="m1", now_ms=0) is True
    assert policy.admit_dispatch(view, sender=B, message_id="m2", now_ms=0) is False
    joined = _view(limits=_limits(k=2), org_available=True)
    assert _admit(joined, target=B) == Mint(peer=B)


def test_p22_thresholds_fall_back_to_the_node_baseline() -> None:
    link_based = _view(limits=_limits(t_max_ms=1000), rtt_max_ms={B: 100})
    assert policy.request_threshold_ms(link_based, B) == 400
    node_based = _view(limits=_limits(t_max_ms=1000))
    assert policy.request_threshold_ms(node_based, B) == 4000
    no_baseline = _view(limits=_limits(t_max_ms=None))
    assert policy.request_threshold_ms(no_baseline, B) is None
    assert dict(policy.censored_inbound(no_baseline, now_ms=10**9)) == {}

    pending = _view(
        pending_inbound=(
            policy.PendingRequest("m1", B, 0),
            policy.PendingRequest("m2", OWNER, 0),
        ),
        limits=_limits(t_max_ms=1000),
    )
    assert dict(policy.censored_inbound(pending, now_ms=3999)) == {}
    assert dict(policy.censored_inbound(pending, now_ms=4000)) == {
        "m1": 4000,
        "m2": 4000,
    }
    assert dict(policy.censored_inbound(pending, now_ms=4000)) == dict(
        policy.censored_inbound(pending, now_ms=4000)
    )


def test_p26_abandoned_falls_back_to_four_times_node_t_max() -> None:
    view = _view(
        links=(_link(A, B),),
        outbound=(policy.OutboundRequest("m1", B, 0),),
        limits=_limits(t_max_ms=1000),
    )
    assert B not in view.rtt_max_ms
    assert policy.abandoned(view, now_ms=3999) == ()
    assert policy.abandoned(view, now_ms=4000) == ("m1",)
    unmeasured = _view(
        links=(_link(A, B),),
        outbound=(policy.OutboundRequest("m1", B, 0),),
        limits=_limits(t_max_ms=None),
    )
    assert policy.abandoned(unmeasured, now_ms=10**9) == ()


def test_p27_degree_counts_two_regular_links_and_ignores_three_pac_links() -> None:
    view = _view(
        links=(
            _link(A, B),
            _link(A, C),
            _link(A, D, lifecycle="pac_task", task_id="t1"),
            _link(A, X, lifecycle="pac_task", task_id="t1"),
            _link(A, "agent:p:m:y", lifecycle="pac_task", task_id="t2"),
        ),
        limits=_limits(k=2),
    )
    assert policy.degree(view) == 2


def test_p28_degree_is_within_k_after_shrink_to_k_reclaims_with_residue() -> None:
    peers = (B, C, D, X)
    links = tuple(_link(A, peer, last_used_ms=i) for i, peer in enumerate(peers))
    view = _view(
        links=links,
        pending_inbound=(policy.PendingRequest("m1", B, 5),),
        outbound=(policy.OutboundRequest("o1", C, 1),),
        limits=_limits(k=2),
    )
    assert policy.degree(view) == 4
    reclaimed = policy.shrink_to_k(view, now_ms=0)
    assert len(reclaimed) == 2
    for peer in reclaimed:
        policy.residue(view, peer=peer)
    after = _view(
        links=tuple(link for link in links if link.peer not in reclaimed),
        limits=_limits(k=2),
    )
    assert policy.degree(after) <= 2


# -- H1: the window switch counts under observe and holds under enforce ----


def test_h1_observe_allows_a_full_cwnd_and_reports_the_bound() -> None:
    """Under observe cwnd never holds; the bound is reported on the Allow."""

    view = _view(
        links=(_link(A, B),),
        outbound=(
            policy.OutboundRequest("m1", B, 0),
            policy.OutboundRequest("m2", B, 0),
        ),
        limits=_limits(k=2, w=4, cwnd={B: 2}),
        window_mode="observe",
    )
    assert _admit(view, target=B) == Allow(peer=B, window_observed="cwnd")


def test_h1_observe_allows_a_full_w_and_reports_the_bound() -> None:
    view = _view(
        links=(_link(A, B),),
        outbound=tuple(policy.OutboundRequest(f"m{i}", C, i) for i in range(4)),
        limits=_limits(k=2, w=4, cwnd={B: 2}),
        window_mode="observe",
    )
    assert _admit(view, target=B) == Allow(peer=B, window_observed="W")


def test_h1_enforce_keeps_the_legacy_window_holds() -> None:
    cwnd_full = _view(
        links=(_link(A, B),),
        outbound=(
            policy.OutboundRequest("m1", B, 0),
            policy.OutboundRequest("m2", B, 0),
        ),
        limits=_limits(k=2, w=4, cwnd={B: 2}),
        window_mode="enforce",
    )
    assert _admit(cwnd_full, target=B) == Hold(
        ipc_errors.LINK_WINDOW_FULL, B, "cwnd"
    )
    w_full = _view(
        links=(_link(A, B),),
        outbound=tuple(policy.OutboundRequest(f"m{i}", C, i) for i in range(4)),
        limits=_limits(k=2, w=4, cwnd={B: 2}),
        window_mode="enforce",
    )
    assert _admit(w_full, target=B) == Hold(ipc_errors.LINK_WINDOW_FULL, B, "W")


def test_h1_observe_never_self_locks_on_its_own_in_flight_row() -> None:
    """E2E-001 phase2: a cwnd=1 sender held because it counted its own row."""

    self_counted = _view(
        links=(_link(A, B),),
        outbound=(policy.OutboundRequest("held-1", B, 0),),
        limits=_limits(k=2, w=4, cwnd={B: 1}),
        window_mode="observe",
    )
    assert _admit(self_counted, target=B) == Allow(peer=B, window_observed="cwnd")


def test_h1_observe_admits_a_w_full_dispatch_and_flags_the_bound() -> None:
    view = _view(
        links=(_link(A, B),),
        pending_inbound=tuple(
            policy.PendingRequest(f"i{n}", C, 0) for n in range(4)
        ),
        limits=_limits(k=2, w=4),
        window_mode="observe",
    )
    assert policy.admit_dispatch(view, sender=B, message_id="m1", now_ms=0) is True
    assert policy.dispatch_window_observed(
        view, sender=B, message_id="m1", intent="request", reply_to=None
    ) is True


def test_h1_enforce_refuses_and_never_flags_the_same_dispatch() -> None:
    view = _view(
        links=(_link(A, B),),
        pending_inbound=tuple(
            policy.PendingRequest(f"i{n}", C, 0) for n in range(4)
        ),
        limits=_limits(k=2, w=4),
        window_mode="enforce",
    )
    assert policy.admit_dispatch(view, sender=B, message_id="m1", now_ms=0) is False
    assert policy.dispatch_window_observed(
        view, sender=B, message_id="m1", intent="request", reply_to=None
    ) is False


def test_h1_dispatch_observation_skips_exempt_and_reply_paths() -> None:
    owner = _view(limits=_limits(k=2, w=0), window_mode="observe")
    assert policy.dispatch_window_observed(
        owner, sender=OWNER, message_id="m1", intent="request", reply_to=None
    ) is False
    reply = _view(
        links=(_link(A, B),),
        pending_inbound=tuple(
            policy.PendingRequest(f"i{n}", C, 0) for n in range(4)
        ),
        outbound=(policy.OutboundRequest("o1", B, 0),),
        limits=_limits(k=2, w=4),
        window_mode="observe",
    )
    assert policy.admit_dispatch(
        reply, sender=B, message_id="r1", now_ms=0, intent="reply", reply_to="o1"
    ) is True
    assert policy.dispatch_window_observed(
        reply, sender=B, message_id="r1", intent="reply", reply_to="o1"
    ) is False
