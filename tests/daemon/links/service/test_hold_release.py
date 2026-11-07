"""WP2 hold tests (spec §4.3): minting, re-evaluation, release and expiry.

Drives the real ``LinkService.tick`` with fake clocks and durable doubles.
"""

from __future__ import annotations


from dataclasses import replace

from hyprial.daemon.impl.links.controller import (
    ControllerOutput,
    ControllerState,
    LoopState,
    WindowStats,
    compute_k,
)
from hyprial.daemon.impl.links.model import LinkEntry
from hyprial.daemon.impl.links.ports import (
    EVENT_LINK_MINTED,
    EVENT_LINK_RECLAIMED,
    Allow,
    Hold,
    Mint,
)
from hyprial.kernel import ipc_errors

from tests.support.links.org import (
    FakeOrgFs,
    LogRecorder,
    RecordingPublisher,
    establish,
    establish_many,
    make_service,
    write_reference,
)

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"
D = "agent:o:n1:d"

MINTING = ipc_errors.LINK_MINTING
PEER_FULL = ipc_errors.LINK_PEER_FULL
SELF_FULL = ipc_errors.LINK_SELF_FULL
WINDOW_FULL = ipc_errors.LINK_WINDOW_FULL


def harness(
    *,
    spaces: tuple[str, ...] = ("s1",),
    publisher=None,
    local_names: tuple[str, ...] = ("a",),
    **kwargs,
):
    fs = FakeOrgFs()
    for space in spaces:
        fs.add_space(space)
    return make_service(
        fs=fs, publisher=publisher, local_names=local_names, **kwargs
    )


def pin(h, actor: str, *, w: int = 8, cwnd: dict[str, int] | None = None) -> None:
    """Pin the controller loops the next pass evaluates (test-only, no stubbing)."""

    with h.service._mem:
        state = h.service._state(actor)

        def loop(value: int) -> LoopState:
            return LoopState(
                value=value,
                prev_t_min_ms=None,
                grad_pos_streak=0,
                next_eval_ms=None,
                last_diff=None,
                last_grad=None,
            )

        state.controller = ControllerState(
            w=loop(w), cwnd={peer: loop(value) for peer, value in (cwnd or {}).items()}
        )
        state.limits = ControllerOutput(
            n_active=0,
            k=compute_k(0, w),
            w=w,
            cwnd=dict(cwnd or {}),
            period_ms=None,
            node=WindowStats(0, None, None, None, None, None, None),
            links={},
            diff=None,
            grad=None,
        )


def craft(
    h,
    actor: str,
    *,
    k: int | None = None,
    w: int | None = None,
    cwnd: dict[str, int] | None = None,
    period_ms: int | None = None,
) -> None:
    """Pin the controller output the next decision reads (test-only)."""

    with h.service._mem:
        state = h.service._state(actor)
        base = state.limits
        assert base is not None
        state.limits = ControllerOutput(
            n_active=base.n_active,
            k=base.k if k is None else k,
            w=base.w if w is None else w,
            cwnd=dict(base.cwnd) if cwnd is None else dict(cwnd),
            period_ms=base.period_ms if period_ms is None else period_ms,
            node=base.node,
            links=dict(base.links),
            diff=base.diff,
            grad=base.grad,
        )


def hold_row(h, message_id, *, sender=A, target=B, reason=MINTING, at, reply_to=None,
             ttl=600_000):
    h.inbox.hold(
        message_id, sender=sender, target=target, reason=reason,
        reply_to=reply_to, created_at_ms=at, expires_at_ms=at + ttl,
    )
    h.service.submitted(
        sender=sender, target=target, message_id=message_id, reply_to=reply_to,
        hold_reason=reason, now_ms=at,
    )


# -- H1 ------------------------------------------------------------------


def test_h1_minting_writes_an_intent_then_a_committed_file_only() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=B)
    # The mint is staged for the service thread, not written from the IPC
    # thread (P-3); ``_view`` already shows it as a pending local reference.
    with h.service._mem:
        staged = h.service._states[A].pending_file
    assert staged is not None and B in staged.entries
    assert B not in h.store.index().files[A].entries
    hold_row(h, "m1", reason=MINTING, at=now)
    h.tick()
    assert A in h.store.index().files
    assert B in h.store.index().files[A].entries
    assert any(node.endswith(f"{A}.json") for _space, node, _v in h.fs.write_versions)
    with h.service._mem:
        held = h.service._states[A].held["m1"]
    assert held.hold_reason == MINTING
    assert held.expires_at_ms == now + 600_000


# -- H2 ------------------------------------------------------------------


def test_h2_one_sided_reference_holds_until_the_peer_references_back() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=B)
    hold_row(h, "m1", reason=MINTING, at=now)
    h.tick()
    with h.service._mem:
        assert "m1" in h.service._states[A].held
        assert h.service._states[A].outbound == []
    assert all(not update.release for update in h.inbox.applies)
    peer = make_service(fs=h.fs)
    peer.tick()
    assert peer.service.sender_known(recipient=B, sender=A) is True
    assert (
        peer.service.admit_dispatch(
            recipient=B, sender=A, message_id="m9", now_ms=now
        )
        is False
    )


# -- H3 ------------------------------------------------------------------


def test_h3_mutual_reference_releases_the_hold_and_emits_both_sides() -> None:
    publisher = RecordingPublisher()
    peer_publisher = RecordingPublisher()
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(fs=fs, publisher=publisher, local_names=("a",))
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=B)
    hold_row(h, "m1", reason=MINTING, at=now)
    h.tick()
    peer = make_service(fs=fs, publisher=peer_publisher, local_names=("b",))
    peer.tick()
    peer.tick()
    h.tick()
    with h.service._mem:
        assert "m1" not in h.service._states[A].held
        assert [item.message_id for item in h.service._states[A].outbound] == ["m1"]
    minted = publisher.payloads(EVENT_LINK_MINTED)
    assert {payload["side"] for payload in minted} == {"minter"}
    assert {
        payload["side"] for payload in peer_publisher.payloads(EVENT_LINK_MINTED)
    } == {"acceptor"}
    # C23/P1: the minted payload is the minted link's own shape; a regular
    # link carries no task binding and never folds in the reclaim keys.
    assert set(minted[0]) == {
        "actor", "atMs", "lifecycle", "mintedAtMs", "peer", "revision", "side",
        "taskId",
    }
    assert minted[0]["peer"] == B
    assert minted[0]["lifecycle"] == "regular"
    assert minted[0]["taskId"] is None
    assert minted[0]["revision"] >= 1


# -- H4 ------------------------------------------------------------------


def test_h4_peer_full_tracks_the_peers_regular_reference_count() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    now = 1_000_000
    establish_many(fs, [(B, C), (B, D)], now_ms=now - 10_000)
    h = make_service(fs=fs, local_names=("a",))
    h.tick()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=B)
    hold_row(h, "m1", reason=MINTING, at=now)
    h.tick()
    with h.service._mem:
        assert h.service._states[A].held["m1"].hold_reason == PEER_FULL
    # B drops one regular reference: the estimate falls below k again.
    write_reference(fs, B, {C: LinkEntry(peer=C, minted_at_ms=now - 9_000, revision=2)},
                    revision=2)
    h.tick()
    with h.service._mem:
        assert h.service._states[A].held["m1"].hold_reason == MINTING
    # B references A back: the link is established and the hold is released.
    write_reference(
        fs,
        B,
        {
            C: LinkEntry(peer=C, minted_at_ms=now - 9_000, revision=2),
            A: LinkEntry(peer=A, minted_at_ms=now, revision=3),
        },
        revision=3,
    )
    h.tick()
    with h.service._mem:
        assert "m1" not in h.service._states[A].held
        assert [item.message_id for item in h.service._states[A].outbound] == ["m1"]


# -- H5 ------------------------------------------------------------------


def test_h5_cwnd_full_hold_releases_after_the_reply_closes_it() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=B)
    h.service.submitted(
        sender=A, target=B, message_id="m1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(WINDOW_FULL, B, "cwnd")
    hold_row(h, "m2", reason=WINDOW_FULL, at=now)
    h.tick()
    with h.service._mem:
        assert h.service._states[A].held["m2"].hold_reason == WINDOW_FULL
        assert h.service._states[A].held_detail["m2"] == "cwnd"
    h.service.reply_received(
        actor=A, reply_message_id="r1", reply_to="m1", sender=B,
        received_at_ms=now + 10,
    )
    h.tick()
    with h.service._mem:
        assert "m2" not in h.service._states[A].held
        assert "m2" in [item.message_id for item in h.service._states[A].outbound]


# -- H6 ------------------------------------------------------------------


def test_h6_w_full_hold_releases_when_an_inbound_settles() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    for message_id in ("i1", "i2"):
        h.service.injected(
            actor=A, message_id=message_id, sender=B, intent="request",
            reply_to=None, received_at_ms=now,
        )
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(WINDOW_FULL, B, "W")
    hold_row(h, "h1", reason=WINDOW_FULL, at=now)
    h.tick()
    with h.service._mem:
        assert h.service._states[A].held_detail["h1"] == "W"
    h.service.submitted(
        sender=A, target=B, message_id="reply", reply_to="i1", hold_reason=None,
        now_ms=now,
    )
    h.tick()
    with h.service._mem:
        assert "h1" not in h.service._states[A].held


def test_h6_w_full_hold_releases_when_the_ledger_shows_an_ack() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.inbox.put_in(A, "i1", peer=B, state="open", opened_at_ms=now)
    h.inbox.put_in(A, "i2", peer=B, state="open", opened_at_ms=now)
    h.tick()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(WINDOW_FULL, B, "W")
    hold_row(h, "h1", reason=WINDOW_FULL, at=now)
    h.tick()
    h.inbox.rows[(A, "i1")] = replace(h.inbox.rows[(A, "i1")], state="acked")
    h.tick()
    with h.service._mem:
        assert "h1" not in h.service._states[A].held


# -- H7 ------------------------------------------------------------------


def test_h7_self_full_evicts_the_lru_link_and_reclaims_it() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    publisher = RecordingPublisher()
    old = 1_000_000 - 10_000
    establish_many(fs, [(A, B), (A, C)], now_ms=old)
    h = make_service(fs=fs, publisher=publisher, local_names=("a",))
    h.tick()
    now = h.clock.now_ms()
    craft(h, A, k=2, w=8, cwnd={B: 5, C: 5}, period_ms=1_000)
    for index, peer in ((1, B), (2, C)):
        h.service.submitted(
            sender=A, target=peer, message_id=f"o{index}", reply_to=None,
            hold_reason=None, now_ms=now,
        )
    with h.service._mem:
        assert h.service._view(A, now).limits.k == 2
    assert h.service.add(actor=A, peer=D, now_ms=now) == {
        "actor": A,
        "peer": D,
        "state": "absent",
        "written": False,
        "queued": False,
        "holdReason": SELF_FULL,
        "detail": "k",
    }
    # One link closes and is past its minimum lifetime: it becomes the victim.
    with h.service._mem:
        state = h.service._states[A]
        state.outbound = [item for item in state.outbound if item.peer != C]
        state.last_used[C] = 0
        state.last_used[B] = now
    outcome = h.service.add(actor=A, peer=D, now_ms=now)
    assert outcome["state"] == "pending"
    assert outcome["written"] is False
    # ``add`` only stages the write and queues the eviction; the pass applies
    # both, then the committed file reflects them (P-3).
    h.tick(now + 1_000)
    assert D in h.store.index().files[A].entries
    assert C not in h.store.index().files[A].entries
    reclaimed = publisher.payloads(EVENT_LINK_RECLAIMED)
    assert [payload["peer"] for payload in reclaimed] == [C]
    assert reclaimed[0]["reason"] == "evicted"


# -- H8 ------------------------------------------------------------------


def test_h8_churn_blocks_minting_until_the_period_passes() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    craft(h, A, k=2, w=8, cwnd={}, period_ms=1_000)
    # Two reclaims in one period exceed the churn budget of k=2?  Use k=1.
    craft(h, A, k=1, w=8, cwnd={}, period_ms=1_000)
    for _ in range(2):
        with h.service._mem:
            h.service._states[A].recent_reclaims_ms.append(now)
    outcome = h.service.add(actor=A, peer=B, now_ms=now)
    assert outcome["holdReason"] == SELF_FULL
    assert outcome["detail"] == "churn"
    later = now + 1_001
    craft(h, A, k=1, w=8, cwnd={}, period_ms=1_000)
    outcome = h.service.add(actor=A, peer=B, now_ms=later)
    assert outcome["state"] == "pending"


# -- H9 ------------------------------------------------------------------


def test_h9_joining_an_org_releases_org_unavailable_holds() -> None:
    fs = FakeOrgFs()
    h = make_service(fs=fs, local_names=("a", "b", "c"))
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(SELF_FULL, B, "org-unavailable")
    hold_row(h, "m1", reason=SELF_FULL, at=now)
    h.tick()
    assert h.store.index().org_available is False
    fs.add_space("s1")
    h.tick()
    assert h.store.index().org_available is True
    with h.service._mem:
        assert h.service._states[A].held["m1"].hold_reason == MINTING
    assert B in h.store.index().files[A].entries
    for actor in (B, C):
        assert actor in h.store.index().files


# -- H10 -----------------------------------------------------------------


def test_h10_release_order_respects_cwnd_and_exempts_replies() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    pin(h, A, w=8, cwnd={B: 2})
    h.service.injected(
        actor=A, message_id="req", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    for index in (1, 2, 3):
        hold_row(
            h, f"h{index}", reason=MINTING, at=now + index
        )
    hold_row(h, "hr", reason=MINTING, at=now + 4, reply_to="req")
    h.tick()
    with h.service._mem:
        state = h.service._states[A]
        assert sorted(item.message_id for item in state.outbound) == ["h1", "h2"]
        assert "h3" in state.held
        assert "hr" not in state.held


# -- H11 -----------------------------------------------------------------


def test_h11_held_reply_waits_for_the_peer_and_then_acks_once() -> None:
    publisher = RecordingPublisher()
    h = harness(publisher=publisher)
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.service.injected(
        actor=A, message_id="req", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    h.inbox.put_in(A, "req", peer=B, state="open", opened_at_ms=now)
    h.tick()
    h.fs.remove("s1", f"link/{B}.json")
    h.tick()
    assert h.service.admit_send(
        sender=A, target=B, intent="reply", reply_to="req", now_ms=now
    ) == Hold(MINTING, B, "awaiting-peer")
    hold_row(h, "hr", reason=MINTING, at=now, reply_to="req")
    h.tick()
    with h.service._mem:
        assert "req" in h.service._states[A].pending_inbound
    assert publisher.payloads(EVENT_LINK_RECLAIMED) == []
    establish(h.fs, A, B)
    h.tick()
    with h.service._mem:
        assert "hr" not in h.service._states[A].held
        assert "req" not in h.service._states[A].pending_inbound
        assert len(h.service._states[A].dwell) == 1
    assert h.inbox.row_state(A, "req") == "closed"
    # A drained request's held reply fails with LINK_RECLAIMED.
    h.service.injected(
        actor=A, message_id="d1", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    with h.service._mem:
        h.service._states[A].drained.add("d1")
    hold_row(h, "hd", reason=MINTING, at=now + 1, reply_to="d1")
    h.tick()
    assert h.inbox.applies[-1].fail.get("hd") == ipc_errors.LINK_RECLAIMED
    # An acked original (not drained) fails with MESSAGE_REPLY_UNAVAILABLE.
    h.inbox.put_in(A, "a1", peer=B, state="open", opened_at_ms=now)
    h.tick()
    h.inbox.rows[(A, "a1")] = replace(h.inbox.rows[(A, "a1")], state="acked")
    h.tick()
    hold_row(h, "ha", reason=MINTING, at=now + 2, reply_to="a1")
    h.tick()
    assert h.inbox.applies[-1].fail.get("ha") == ipc_errors.MESSAGE_REPLY_UNAVAILABLE


# -- H12 -----------------------------------------------------------------


def test_h12_expired_holds_are_never_released_or_counted() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=B)
    hold_row(h, "m1", reason=MINTING, at=now, ttl=1_000)
    h.tick()
    assert A in h.store.index().files
    h.inbox.drop_held("m1")
    h.clock.advance(5_000)
    h.tick()
    with h.service._mem:
        state = h.service._states[A]
        assert "m1" not in state.held
        assert state.outbound == []
    assert B not in h.store.index().files[A].entries


# -- H13 -----------------------------------------------------------------


def test_h13_only_really_released_ids_enter_memory() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    pin(h, A, w=8, cwnd={B: 5})
    for index in (1, 2):
        hold_row(h, f"h{index}", reason=MINTING, at=now + index)
    h.inbox.release_partial = {"h1"}
    h.tick()
    with h.service._mem:
        state = h.service._states[A]
        assert [item.message_id for item in state.outbound] == ["h1"]
        assert "h2" in state.held
    h.inbox.release_partial = None
    h.tick()
    with h.service._mem:
        assert sorted(item.message_id for item in h.service._states[A].outbound) == [
            "h1",
            "h2",
        ]
    # An apply failure keeps memory unchanged and retries next pass.
    h.inbox.fail_apply = True
    hold_row(h, "h3", reason=MINTING, at=now + 3)
    h.tick()
    h.inbox.fail_apply = False
    h.tick()
    with h.service._mem:
        state = h.service._states[A]
        assert "h3" not in state.held
        assert sum(1 for item in state.outbound if item.message_id == "h3") == 1


# -- H15 -----------------------------------------------------------------


def test_h15_only_confirmed_cwnd_holds_shrink_the_link() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=B)
    h.service.submitted(
        sender=A, target=B, message_id="m1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(WINDOW_FULL, B, "cwnd")
    hold_row(h, "m2", reason=WINDOW_FULL, at=now)
    h.tick()
    with h.service._mem:
        assert h.service._states[A].held_cwnd_peers == frozenset({B})
    # Let the cwnd hold release, then a W hold on the same link must not enter
    # held_peers.
    pin(h, A, w=2, cwnd={B: 5})
    h.tick()
    with h.service._mem:
        assert "m2" not in h.service._states[A].held
    hold_row(h, "m3", reason=WINDOW_FULL, at=now + 1)
    h.tick()
    with h.service._mem:
        state = h.service._states[A]
        assert "m3" in state.held
        assert state.held_detail["m3"] == "W"
        assert B not in state.held_cwnd_peers


# -- H16 -----------------------------------------------------------------


def test_h16_a_missing_copy_is_not_a_reclaim() -> None:
    publisher = RecordingPublisher()
    h = harness(publisher=publisher)
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.service.injected(
        actor=A, message_id="m1", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    h.fs.remove("s1", f"link/{B}.json")
    h.tick()
    assert publisher.payloads(EVENT_LINK_RECLAIMED) == []
    assert (
        h.service.admit_dispatch(recipient=A, sender=B, message_id="m1", now_ms=now)
        is True
    )
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(MINTING, B, "awaiting-peer")
    # A higher revision that no longer references A is an authoritative reclaim.
    write_reference(h.fs, B, {}, revision=9)
    h.tick()
    reclaimed = publisher.payloads(EVENT_LINK_RECLAIMED)
    assert [payload["reason"] for payload in reclaimed] == ["peer_withdrew"]
    with h.service._mem:
        assert "m1" in h.service._states[A].drained


# -- H1: observe counts the window instead of holding (E2E-001 phase2) ----


def test_h1_observe_allows_the_cwnd_full_send_and_counts_it() -> None:
    """The E2E-001 phase2 front: the cwnd=1 sender no longer self-locks."""

    logs = LogRecorder()
    h = harness(logger=logs, window_mode="observe")
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=B)
    h.service.submitted(
        sender=A, target=B, message_id="m1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=B, window_observed="cwnd")
    assert h.service.stats(A)["windowObserved"] == 1
    assert logs.count("link.window_observed") == 1
    fields = logs.fields_for("link.window_observed")[0]
    assert fields["bound"] == "cwnd"
    assert fields["site"] == "send"
    assert fields["peer"] == B


def test_h1_observe_releases_a_persisted_window_hold() -> None:
    h = harness(window_mode="observe")
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Allow(peer=B)
    h.service.submitted(
        sender=A, target=B, message_id="m1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    hold_row(h, "m2", reason=WINDOW_FULL, at=now)
    h.tick()
    with h.service._mem:
        assert "m2" not in h.service._states[A].held
        assert "m2" in [item.message_id for item in h.service._states[A].outbound]


def test_h1_enforce_still_holds_the_same_persisted_row() -> None:
    h = harness(window_mode="enforce")
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=B, message_id="m1", reply_to=None, hold_reason=None,
        now_ms=now,
    )
    hold_row(h, "m2", reason=WINDOW_FULL, at=now)
    h.tick()
    with h.service._mem:
        assert h.service._states[A].held["m2"].hold_reason == WINDOW_FULL
        assert h.service._states[A].held_detail["m2"] == "cwnd"
