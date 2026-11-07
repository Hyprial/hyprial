"""WP2 sample and degradation tests (spec §4.5): SA1-SA16.

Every case drives the real ``LinkService.tick`` with a fake clock and fixed
seeds; ``controller.evaluate`` is never stubbed (review B17).  Where a test
needs a controller baseline it pins the *input* output the next pass reads,
which is a test-only fixture of the same kind ``test_hold_release`` uses.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.links.controller import (
    ControllerOutput,
    WindowStats,
)
from hyprial.daemon.impl.links.model import LinkEntry
from hyprial.daemon.impl.links.ports import Allow, Hold, Mint
from hyprial.daemon.impl.links.service import SAMPLE_CAP
from hyprial.kernel import DaemonRequestError, ipc_errors

from tests.support.links.org import (
    FakeOrgFs,
    establish,
    establish_many,
    make_service,
    write_reference,
)

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"
X = "agent:p:n2:x"
OWNER = "user:o"
U = "user:p"

WINDOW_FULL = ipc_errors.LINK_WINDOW_FULL
SELF_FULL = ipc_errors.LINK_SELF_FULL
MESSAGE_REPLY_UNAVAILABLE = ipc_errors.MESSAGE_REPLY_UNAVAILABLE


def harness(*, local_names=("a",), **kwargs):
    fs = FakeOrgFs()
    fs.add_space("s1")
    return make_service(fs=fs, local_names=local_names, **kwargs)


def pin(
    h,
    actor: str,
    *,
    k: int | None = None,
    w: int | None = None,
    cwnd: dict[str, int] | None = None,
    node: WindowStats | None = None,
    links: dict[str, WindowStats] | None = None,
    period_ms: int | None = None,
) -> None:
    """Pin the controller output the next pass reads (test-only)."""

    with h.service._mem:
        state = h.service._state(actor)
        base = state.limits
        assert base is not None, "tick() must have produced a controller output"
        state.limits = ControllerOutput(
            n_active=base.n_active,
            k=base.k if k is None else k,
            w=base.w if w is None else w,
            cwnd=dict(base.cwnd) if cwnd is None else dict(cwnd),
            period_ms=base.period_ms if period_ms is None else period_ms,
            node=base.node if node is None else node,
            links=dict(base.links) if links is None else dict(links),
            diff=base.diff,
            grad=base.grad,
        )


def _empty(t_max: int | None = None) -> WindowStats:
    return WindowStats(0, None, t_max, None, None, None, None)


def _outbound(h, message_id: str) -> bool:
    with h.service._mem:
        return any(
            item.message_id == message_id for item in h.service._states[A].outbound
        )


def test_last_used_is_a_real_activity_timestamp() -> None:
    """A link that carried a send must not report the zero sentinel as epoch."""

    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()

    h.service.admit_send(
        sender=A,
        target=B,
        intent="request",
        reply_to=None,
        now_ms=now,
    )

    rows = h.service.list_links(A)["links"]
    link = next(row for row in rows if row["peer"] == B)
    assert link["lastUsedAtMs"] == now


# -- SA1 -----------------------------------------------------------------


def test_sa1_reply_samples_keep_their_direction() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    # A reply this node sends feeds the node loop only.
    h.service.submitted(
        sender=A,
        target=B,
        message_id="m-sent",
        reply_to="req-1",
        hold_reason=None,
        now_ms=now,
    )
    # A reply this node receives on the A-B link feeds that link only.
    h.service.submitted(
        sender=A,
        target=B,
        message_id="m-req",
        reply_to=None,
        hold_reason=None,
        now_ms=now,
    )
    h.service.reply_received(
        actor=A,
        reply_message_id="r-1",
        reply_to="m-req",
        sender=B,
        received_at_ms=now + 5,
    )
    with h.service._mem:
        replies = list(h.service._states[A].replies)
    assert [(sample.direction, sample.peer) for sample in replies] == [
        ("sent", None),
        ("received", B),
    ]
    h.tick(now + 10)
    stats = h.service.stats(A)
    link = next(item for item in stats["links"] if item["peer"] == B)
    assert link["rttMinMs"] == 5
    assert stats["tBarMs"] is None


# -- SA2 -----------------------------------------------------------------


def test_sa2_dwell_is_measured_from_arrival_not_admission() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    t0 = h.clock.now_ms()
    h.service.injected(
        actor=A,
        message_id="in-1",
        sender=B,
        intent="request",
        reply_to=None,
        received_at_ms=t0,
    )
    assert (
        h.service.admit_dispatch(
            recipient=A, sender=B, message_id="in-1", now_ms=t0 + 30_000
        )
        is True
    )
    h.service.submitted(
        sender=A,
        target=B,
        message_id="out-1",
        reply_to="in-1",
        hold_reason=None,
        now_ms=t0 + 40_000,
    )
    with h.service._mem:
        dwell = [sample.dwell_ms for sample in h.service._states[A].dwell]
    assert dwell == [40_000]


# -- SA3 -----------------------------------------------------------------


def test_sa3_rtt_uses_the_ledger_sent_at_not_the_acceptance_time() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    s = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=B, message_id="m-1", reply_to=None, hold_reason=None, now_ms=s
    )
    sent = s + 5_000
    received = s + 60_000
    h.inbox.put_out(
        A,
        "m-1",
        peer=B,
        state="closed",
        opened_at_ms=s,
        sent_at_ms=sent,
        event_at_ms=received,
        sample_pending=True,
    )
    h.tick(received + 30_000)
    with h.service._mem:
        rtt = [item for item in h.service._states[A].rtt if item.peer == B]
    assert [item.rtt_ms for item in rtt] == [received - sent]


# -- SA4 -----------------------------------------------------------------


def test_sa4_a_fast_reply_is_sampled_once_from_the_ledger() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    s = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=B, message_id="m-1", reply_to=None, hold_reason=None, now_ms=s
    )
    # The reply lands and is consumed before any pass; the sink callback is
    # dropped, so only the durable sample_pending flag can close it.
    h.inbox.put_out(
        A,
        "m-1",
        peer=B,
        state="closed",
        opened_at_ms=s,
        sent_at_ms=s,
        event_at_ms=s + 7,
        sample_pending=True,
    )
    h.tick(s + 10)
    h.tick(s + 20)
    with h.service._mem:
        rtt = [item for item in h.service._states[A].rtt if item.peer == B]
    assert [item.rtt_ms for item in rtt] == [7]


# -- SA5 -----------------------------------------------------------------


def test_sa5_exempt_inbound_counts_towards_w_once() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    # A managed owner request has no fetched stamp yet; the injection callback
    # is what makes it visible.
    h.service.injected(
        actor=A,
        message_id="o1",
        sender=OWNER,
        intent="request",
        reply_to=None,
        received_at_ms=now,
    )
    # pending.list / wait / read_body / push retry all inject the same id.
    for _ in range(3):
        h.service.injected(
            actor=A,
            message_id="o1",
            sender=OWNER,
            intent="request",
            reply_to=None,
            received_at_ms=now,
        )
    # A row the inbox still holds is invisible until its injection commits.
    h.inbox.queue_inbound(A, "waiting", sender=OWNER, received_at_ms=now)
    # A system notice is not a request and never occupies W.
    h.service.injected(
        actor=A,
        message_id="sn",
        sender=OWNER,
        intent="notice",
        reply_to=None,
        received_at_ms=now,
    )
    with h.service._mem:
        pending = dict(h.service._states[A].pending_inbound)
    assert sorted(pending) == ["o1"]
    # The next managed admission in the same thread already sees the owner
    # request: a second one fills W and the send is held.
    h.service.injected(
        actor=A,
        message_id="o2",
        sender=OWNER,
        intent="request",
        reply_to=None,
        received_at_ms=now,
    )
    assert h.service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=now
    ) == Hold(WINDOW_FULL, B, "W")


# -- SA6 -----------------------------------------------------------------


def test_sa6_a_user_reply_closes_through_the_ledger() -> None:
    h = harness()
    h.tick()
    s = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=U, message_id="mu", reply_to=None, hold_reason=None, now_ms=s
    )
    received = s + 40
    h.inbox.put_out(
        A,
        "mu",
        peer=U,
        state="closed",
        opened_at_ms=s,
        sent_at_ms=s,
        event_at_ms=received,
        sample_pending=True,
    )
    h.tick(received + 5)
    with h.service._mem:
        state = h.service._states[A]
        assert not any(item.message_id == "mu" for item in state.outbound)
        rtt = [item for item in state.rtt if item.peer == U]
    assert [item.rtt_ms for item in rtt] == [40]


# -- SA7 -----------------------------------------------------------------


def test_sa7_abandonment_uses_the_link_then_node_threshold() -> None:
    # (a) A measured RTT_max gives the 4x threshold and one censored sample.
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    pin(h, A, links={B: WindowStats(1, 100, 1000, 100.0, 1.0, 1, 1000)})
    h.service.submitted(
        sender=A,
        target=B,
        message_id="m-old",
        reply_to=None,
        hold_reason=None,
        now_ms=now - 4001,
    )
    h.tick(now)
    with h.service._mem:
        state = h.service._states[A]
        assert "m-old" in state.released_outbound
        censored = [item for item in state.rtt if item.peer == B and item.censored]
    assert [item.rtt_ms for item in censored] == [4000]

    # (b) Without an RTT_max the node window T_max is the fallback.
    h2 = harness()
    establish(h2.fs, A, B)
    h2.tick()
    now2 = h2.clock.now_ms()
    pin(h2, A, links={}, node=WindowStats(1, 100, 1000, 100.0, 1.0, 1, 1000))
    h2.service.submitted(
        sender=A,
        target=B,
        message_id="m-old",
        reply_to=None,
        hold_reason=None,
        now_ms=now2 - 4001,
    )
    h2.tick(now2)
    assert _outbound(h2, "m-old") is False

    # (c) With no baseline at all nothing is fabricated: agent outbound stays.
    h3 = harness()
    establish(h3.fs, A, B)
    h3.tick()
    now3 = h3.clock.now_ms()
    pin(h3, A, links={}, node=_empty())
    h3.service.submitted(
        sender=A,
        target=B,
        message_id="m-old",
        reply_to=None,
        hold_reason=None,
        now_ms=now3 - 10_000_000,
    )
    h3.tick(now3)
    with h3.service._mem:
        assert "m-old" not in h3.service._states[A].released_outbound
    assert _outbound(h3, "m-old") is True


# -- SA8 -----------------------------------------------------------------


def test_sa8_censored_inbound_is_recorded_once_and_stays_replyable() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    pin(h, A, links={}, node=WindowStats(1, 100, 1000, 100.0, 1.0, 1, 1000))
    h.service.injected(
        actor=A,
        message_id="m1",
        sender=B,
        intent="request",
        reply_to=None,
        received_at_ms=now - 5000,
    )
    h.tick(now)
    with h.service._mem:
        state = h.service._states[A]
        assert "m1" in state.censored_inbound
        assert "m1" not in state.pending_inbound
        assert "m1" not in state.admitted
        censored = [item for item in state.dwell if item.censored]
    assert [item.dwell_ms for item in censored] == [4000]
    # The censored sample is its threshold: T_max is not fed by it, so the
    # next threshold does not self-amplify.
    assert h.service.stats(A)["tMaxMs"] is None
    # Repeated passes neither resample nor re-occupy W.
    h.tick(now + 1)
    with h.service._mem:
        assert len([item for item in h.service._states[A].dwell if item.censored]) == 1
    # A late reply is still allowed, and it records no second dwell.
    assert h.service.admit_send(
        sender=A, target=B, intent="reply", reply_to="m1", now_ms=now + 2
    ) == Allow(peer=B)
    h.service.submitted(
        sender=A,
        target=B,
        message_id="r1",
        reply_to="m1",
        hold_reason=None,
        now_ms=now + 3,
    )
    with h.service._mem:
        assert len([item for item in h.service._states[A].dwell if item.censored]) == 1
    # An acked request is no longer answerable.
    h.inbox.put_in(A, "m1", peer=B, state="acked", opened_at_ms=now - 5000)
    h.tick(now + 4)
    with pytest.raises(DaemonRequestError) as unavailable:
        h.service.admit_send(
            sender=A, target=B, intent="reply", reply_to="m1", now_ms=now + 5
        )
    assert unavailable.value.code == MESSAGE_REPLY_UNAVAILABLE


# -- SA9 -----------------------------------------------------------------


def test_sa9_period_comes_from_limits_and_evaluation_is_reproducible() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    # P is read from the controller output; the service never recomputes it.
    pin(h, A, period_ms=12345)
    assert h.service.stats(A)["periodMs"] == 12345
    # Every tick evaluates, so nActive follows presence once the 24h window
    # slides past the agents that went offline.
    h.presence.set([f"agent:p:n{i}" for i in range(5)])
    h.tick(now + 1)
    assert h.service.stats(A)["nActive"] == 5
    h.clock.advance(25 * 60 * 60 * 1000)
    h.presence.set(["agent:p:n0", "agent:p:n1"])
    h.tick()
    assert h.service.stats(A)["nActive"] == 2

    def run(seed: int) -> dict:
        hh = harness(seed=seed)
        establish(hh.fs, A, B)
        hh.tick()
        hh.presence.set(["agent:p:n0", "agent:p:n1"])
        hh.tick(hh.clock.now_ms() + 1)
        return hh.service.stats(A)

    assert run(7) == run(7)


# -- SA10 ----------------------------------------------------------------


def test_sa10_n_active_tracks_the_presence_window_and_excludes_the_actor() -> None:
    h = harness()
    h.tick()
    now = h.clock.now_ms()
    peers = [f"agent:p:n{i}" for i in range(30)]
    h.presence.set([A, *peers])
    h.tick(now)
    assert h.service.stats(A)["nActive"] == 30
    h.clock.advance(25 * 60 * 60 * 1000)
    h.presence.set([A, *peers[:4]])
    h.tick()
    assert h.service.stats(A)["nActive"] == 4


# -- SA11 ----------------------------------------------------------------


def test_sa11_window_growth_is_bounded_and_never_grows_on_positive_gradient() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    base = h.clock.now_ms()

    def feed(now: int, low_ms: int, high_ms: int, *, extra_replies: int) -> None:
        # Ten dwell samples whose window seeds ``T_min`` at ``low_ms`` and whose
        # spread keeps ``T_bar`` close enough to ``T_min`` that ``diff < alpha``:
        # this is the increase branch the old scenario never reached (P-7).
        for index in range(10):
            dwell = low_ms + index * (high_ms - low_ms) // 9
            h.service.injected(
                actor=A,
                message_id=f"in{now}-{index}",
                sender=B,
                intent="request",
                reply_to=None,
                received_at_ms=now - dwell,
            )
            h.service.submitted(
                sender=A,
                target=B,
                message_id=f"out{now}-{index}",
                reply_to=f"in{now}-{index}",
                hold_reason=None,
                now_ms=now,
            )
        for reply in range(extra_replies):
            h.service.submitted(
                sender=A,
                target=B,
                message_id=f"reply{now}-{reply}",
                reply_to="ghost",
                hold_reason=None,
                now_ms=now,
            )

    previous = h.service.stats(A)
    grew = 0
    step = 0
    # Phase 1: a tight, unchanging load; W must climb one at a time.
    for _index in range(7):
        step += 1
        now = base + step * 11_000
        feed(now, 1_000, 1_900, extra_replies=3)
        h.tick(now)
        current = h.service.stats(A)
        assert current["W"] >= current["k"]
        assert current["W"] - previous["W"] <= 1
        if current["W"] > previous["W"]:
            grew += 1
            assert current["grad"] is None or current["grad"] <= 0
        previous = current
    assert grew >= 1, "W never entered the increase branch"
    # Phase 2: the load rises, so W must fall back and settle above k.
    fell = 0
    for index in range(6):
        step += 1
        now = base + step * 11_000
        low = 3_000 + index * 3_000
        feed(now, low, low + 900, extra_replies=1)
        h.tick(now)
        current = h.service.stats(A)
        assert current["W"] >= current["k"]
        if current["W"] < previous["W"]:
            fell += 1
        previous = current
    assert fell >= 1, "W never fell back under load"
    assert previous["W"] > previous["k"]


# -- SA12 ----------------------------------------------------------------


def test_sa12_a_flooded_peer_does_not_disturb_another_links_samples() -> None:
    h = harness()
    establish_many(h.fs, [(A, B), (A, C)])
    h.tick()
    now = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=C, message_id="mc", reply_to=None, hold_reason=None, now_ms=now
    )
    h.service.reply_received(
        actor=A,
        reply_message_id="rc",
        reply_to="mc",
        sender=C,
        received_at_ms=now + 9,
    )
    for index in range(1100):
        h.service.submitted(
            sender=A,
            target=B,
            message_id=f"mb{index}",
            reply_to=None,
            hold_reason=None,
            now_ms=now,
        )
        h.service.reply_received(
            actor=A,
            reply_message_id=f"rb{index}",
            reply_to=f"mb{index}",
            sender=B,
            received_at_ms=now + 10,
        )
    h.tick(now + 100)
    with h.service._mem:
        by_peer: dict[str, list] = {}
        for sample in h.service._states[A].rtt:
            by_peer.setdefault(sample.peer, []).append(sample)
    assert len(by_peer[B]) <= SAMPLE_CAP
    assert [sample.rtt_ms for sample in by_peer[C]] == [9]


# -- SA13 ----------------------------------------------------------------


def test_sa13_abandoned_head_is_never_resent_and_frees_the_next_hold() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    pin(
        h,
        A,
        cwnd={B: 1},
        links={B: WindowStats(1, 100, 1000, 100.0, 1.0, 1, 1000)},
        period_ms=1000,
    )
    h.service.submitted(
        sender=A,
        target=B,
        message_id="m-old",
        reply_to=None,
        hold_reason=None,
        now_ms=now - 4001,
    )
    h.inbox.hold(
        "m-next",
        sender=A,
        target=B,
        reason=WINDOW_FULL,
        created_at_ms=now,
        expires_at_ms=now + 600_000,
    )
    h.service.submitted(
        sender=A,
        target=B,
        message_id="m-next",
        reply_to=None,
        hold_reason=WINDOW_FULL,
        now_ms=now,
    )
    h.tick(now)
    with h.service._mem:
        state = h.service._states[A]
        assert "m-old" in state.released_outbound
        assert not any(item.message_id == "m-old" for item in state.outbound)
        censored = [item for item in state.rtt if item.peer == B and item.censored]
    assert [item.rtt_ms for item in censored] == [4000]
    # The freed cwnd releases the held follow-up in the same pass.
    assert "m-next" not in h.inbox.held
    assert h.inbox.row_state(A, "m-next") == "open"
    # The censored threshold never entered T_max.
    link = next(item for item in h.service.stats(A)["links"] if item["peer"] == B)
    assert link["rttMaxMs"] is None


# -- SA14 ----------------------------------------------------------------


def test_sa14_lru_churn_recovery_and_seeded_reproducibility() -> None:
    h = harness()
    write_reference(
        h.fs,
        A,
        {
            B: LinkEntry(peer=B, minted_at_ms=990_000, revision=1, last_used_ms=1_000),
            C: LinkEntry(peer=C, minted_at_ms=990_000, revision=1, last_used_ms=2_000),
        },
        revision=1,
    )
    write_reference(h.fs, B, {A: LinkEntry(peer=A, minted_at_ms=990_000, revision=1)},
                    revision=1)
    write_reference(h.fs, C, {A: LinkEntry(peer=A, minted_at_ms=990_000, revision=1)},
                    revision=1)
    h.tick()
    now = h.clock.now_ms()
    pin(h, A, k=2, period_ms=1000)
    # The least-recently-used eligible link is the eviction victim.
    assert h.service.admit_send(
        sender=A, target=X, intent="request", reply_to=None, now_ms=now
    ) == Mint(peer=X, evict=B)

    # A link inside its minimum lifetime is never evicted.
    h2 = harness()
    write_reference(
        h2.fs,
        A,
        {
            B: LinkEntry(peer=B, minted_at_ms=1_000_000, revision=1, last_used_ms=1_000),
            C: LinkEntry(peer=C, minted_at_ms=1_000_000, revision=1, last_used_ms=2_000),
        },
        revision=1,
    )
    write_reference(h2.fs, B, {A: LinkEntry(peer=A, minted_at_ms=990_000, revision=1)},
                    revision=1)
    write_reference(h2.fs, C, {A: LinkEntry(peer=A, minted_at_ms=990_000, revision=1)},
                    revision=1)
    h2.tick()
    now2 = h2.clock.now_ms()
    pin(h2, A, k=2, period_ms=1000)
    assert h2.service.admit_send(
        sender=A, target=X, intent="request", reply_to=None, now_ms=now2
    ) == Hold(SELF_FULL, X, "k")

    # Churn blocks minting until the period passes.
    h3 = harness()
    establish_many(h3.fs, [(A, B), (A, C)])
    h3.tick()
    now3 = h3.clock.now_ms()
    pin(h3, A, k=2, period_ms=1000)
    with h3.service._mem:
        h3.service._states[A].recent_reclaims_ms = [now3, now3, now3]
    assert h3.service.admit_send(
        sender=A, target=X, intent="request", reply_to=None, now_ms=now3
    ) == Hold(SELF_FULL, X, "churn")
    later = now3 + 2_000
    with h3.service._mem:
        h3.service._states[A].recent_reclaims_ms = [now3]
    assert h3.service.admit_send(
        sender=A, target=X, intent="request", reply_to=None, now_ms=later
    ) == Mint(peer=X, evict=B)

    # Different seeds jitter the evaluation instant; the same seed reproduces.
    def evaluation_ms(seed: int) -> int | None:
        hh = harness(seed=seed)
        establish(hh.fs, A, B)
        hh.tick()
        base = hh.clock.now_ms()
        for index in range(1, 13):
            at = base + index * 1_000
            hh.service.injected(
                actor=A,
                message_id=f"in{index}",
                sender=B,
                intent="request",
                reply_to=None,
                received_at_ms=at - 1_000,
            )
            hh.service.submitted(
                sender=A,
                target=B,
                message_id=f"out{index}",
                reply_to=f"in{index}",
                hold_reason=None,
                now_ms=at,
            )
            hh.tick(at)
        with hh.service._mem:
            return hh.service._states[A].controller.w.next_eval_ms

    assert evaluation_ms(7) == evaluation_ms(7)
    assert evaluation_ms(7) != evaluation_ms(8)


# -- SA15 ----------------------------------------------------------------


def test_sa15_cold_start_holds_w_until_the_window_has_ten_samples() -> None:
    h = harness()
    establish(h.fs, A, B)
    h.tick()
    base = h.clock.now_ms()
    k = h.service.stats(A)["k"]
    for index in range(1, 10):
        now = base + index * 100
        h.service.injected(
            actor=A,
            message_id=f"in{index}",
            sender=B,
            intent="request",
            reply_to=None,
            received_at_ms=now - 100,
        )
        h.service.submitted(
            sender=A,
            target=B,
            message_id=f"out{index}",
            reply_to=f"in{index}",
            hold_reason=None,
            now_ms=now,
        )
        h.tick(now)
        assert h.service.stats(A)["W"] == k
    now = base + 1_000
    h.service.injected(
        actor=A,
        message_id="in10",
        sender=B,
        intent="request",
        reply_to=None,
        received_at_ms=now - 100,
    )
    h.service.submitted(
        sender=A,
        target=B,
        message_id="out10",
        reply_to="in10",
        hold_reason=None,
        now_ms=now,
    )
    h.tick(now)
    with h.service._mem:
        assert h.service._states[A].controller.w.next_eval_ms is not None


# -- SA16 ----------------------------------------------------------------


def test_sa16_without_a_baseline_an_idle_link_is_still_evictable() -> None:
    """Fewer than the 10 minimum samples leave period_ms None; that must not
    freeze LRU eviction (production sends were stuck at LINK_SELF_FULL/k)."""

    h = harness()
    establish_many(h.fs, [(A, B), (A, C)])
    h.tick()
    now = h.clock.now_ms()
    assert h.service.stats(A)["periodMs"] is None
    decision = h.service.admit_send(
        sender=A, target=X, intent="request", reply_to=None, now_ms=now
    )
    assert isinstance(decision, Mint)
    assert decision.peer == X
    assert decision.evict in {B, C}
    with h.service._mem:
        state = h.service._states[A]
        assert not state.reclaimed
        assert not state.released_outbound
