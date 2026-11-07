"""Emergency bypass (``HYPRIAL_LINKS_BYPASS``): local sends and durable holds.

With the switch off every case below holds exactly as today; with it on the
same link-state hold admits, link bookkeeping is frozen (no OrgFS writes), and
a rate-limited ``link.bypass_admitted`` line records what would have held.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.links.ports import Allow, Hold, Mint
from hyprial.kernel import DaemonRequestError, ipc_errors

from tests.support.links.org import FakeOrgFs, LogRecorder, make_service

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"
X = "agent:p:n2:x"

MINTING = ipc_errors.LINK_MINTING
SELF_FULL = ipc_errors.LINK_SELF_FULL


def harness(*, bypass: bool, spaces: tuple[str, ...] = ("s1",), logger=None):
    fs = FakeOrgFs()
    for space in spaces:
        fs.add_space(space)
    return make_service(
        fs=fs, local_names=("a", "b", "c"), bypass=bypass, logger=logger
    )


def send(h, target: str = B, *, sender: str = A, reply_to: str | None = None):
    return h.service.admit_send(
        sender=sender,
        target=target,
        intent="reply" if reply_to else "request",
        reply_to=reply_to,
        now_ms=h.clock.now_ms(),
    )


def hold_row(h, message_id: str, *, target: str = B, reason: str = MINTING) -> None:
    now = h.clock.now_ms()
    h.inbox.hold(
        message_id, sender=A, target=target, reason=reason, reply_to=None,
        created_at_ms=now, expires_at_ms=now + 600_000,
    )
    h.service.submitted(
        sender=A, target=target, message_id=message_id, reply_to=None,
        hold_reason=reason, now_ms=now,
    )


def craft_k(h, actor: str, k: int) -> None:
    from dataclasses import replace

    with h.service._mem:
        state = h.service._state(actor)
        assert state.limits is not None
        state.limits = replace(state.limits, k=k)


# -- (a) a send to a peer with no link -----------------------------------


def test_unlinked_send_holds_with_the_switch_off() -> None:
    h = harness(bypass=False)
    h.tick()
    assert send(h) == Mint(peer=B)


def test_switch_on_does_not_admit_a_non_local_sender() -> None:
    h = harness(bypass=True)
    h.tick()
    with pytest.raises(DaemonRequestError) as refused:
        send(h, sender=X)
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_unlinked_send_is_admitted_with_the_switch_on_without_minting() -> None:
    logs = LogRecorder()
    h = harness(bypass=True, logger=logs)
    h.tick()
    decision = send(h)
    assert isinstance(decision, Allow)
    assert decision.peer == B
    # Bookkeeping is frozen: no one-sided reference is staged or committed.
    h.tick()
    assert B not in h.store.index().files.get(A, type("F", (), {"entries": {}})()).entries
    fields = logs.fields_for("link.bypass_admitted")
    assert len(fields) == 1
    assert fields[0]["actor"] == A
    assert fields[0]["peer"] == B
    assert fields[0]["wouldHoldReason"] == MINTING
    assert fields[0]["direction"] == "send"


def test_bypass_log_is_rate_limited_per_actor_peer_reason() -> None:
    logs = LogRecorder()
    h = harness(bypass=True, logger=logs)
    h.tick()
    send(h)
    send(h)
    send(h, C)
    assert [(f["peer"]) for f in logs.fields_for("link.bypass_admitted")] == [B, C]
    h.clock.advance(60_001)
    send(h)
    assert len(logs.fields_for("link.bypass_admitted")) == 3


def test_one_sided_pending_link_admits_with_the_switch_on() -> None:
    for bypass in (False, True):
        h = harness(bypass=bypass)
        h.tick()
        first = send(h, X)
        if not bypass:
            assert first == Mint(peer=X)
            hold_row(h, "m1", target=X)
        h.tick()
        # Held mail keeps its one-sided reference; bypass freezes bookkeeping.
        if bypass:
            assert X not in h.store.index().files.get(A, type("F", (), {"entries": {}})()).entries
        else:
            assert X in h.store.index().files[A].entries
        second = send(h, X)
        if bypass:
            assert second == Allow(peer=X, bypassed=MINTING)
        else:
            assert isinstance(second, Hold)


def test_unlinked_reply_admits_with_the_switch_on() -> None:
    for bypass in (False, True):
        h = harness(bypass=bypass)
        h.tick()
        now = h.clock.now_ms()
        h.service.injected(
            actor=A, message_id="r1", sender=X, intent="request",
            reply_to=None, received_at_ms=now,
        )
        decision = send(h, X, reply_to="r1")
        if bypass:
            assert isinstance(decision, Allow), decision
        else:
            assert decision == Hold(MINTING, X, "awaiting-peer")


# -- (c) k-full and org-unavailable --------------------------------------


def test_k_full_holds_off_and_admits_on_without_minting_or_evicting() -> None:
    for bypass in (False, True):
        h = harness(bypass=bypass)
        h.tick()
        craft_k(h, A, 0)
        decision = send(h)
        if not bypass:
            assert decision == Hold(SELF_FULL, B, "k")
            continue
        assert decision == Allow(peer=B, bypassed=SELF_FULL)
        with h.service._mem:
            staged = h.service._states[A].pending_file
            assert staged is None or B not in staged.entries
            assert h.service._states[A].pending_reclaims == []


def test_org_unavailable_holds_off_and_admits_on() -> None:
    for bypass in (False, True):
        h = harness(bypass=bypass, spaces=())
        h.tick()
        assert h.store.index().org_available is False
        decision = send(h)
        if bypass:
            assert decision == Allow(peer=B, bypassed=SELF_FULL)
        else:
            assert decision == Hold(SELF_FULL, B, "org-unavailable")


def test_a_held_queue_head_does_not_block_a_new_send_under_bypass() -> None:
    h = harness(bypass=True, spaces=())
    h.tick()
    hold_row(h, "m0", reason=SELF_FULL)
    assert isinstance(send(h), Allow)


# -- (d) the existing backlog drains -------------------------------------


def test_held_entries_stay_held_with_the_switch_off() -> None:
    h = harness(bypass=False, spaces=())
    h.tick()
    hold_row(h, "m1", reason=SELF_FULL)
    h.tick()
    with h.service._mem:
        assert "m1" in h.service._states[A].held


def test_held_entries_drain_on_the_next_pass_with_the_switch_on() -> None:
    logs = LogRecorder()
    h = harness(bypass=True, spaces=(), logger=logs)
    h.tick()
    hold_row(h, "m1", reason=SELF_FULL)
    hold_row(h, "m2", target=C, reason=MINTING)
    h.tick()
    released = {mid for update in h.inbox.applies for mid in update.release}
    assert {"m1", "m2"} <= released
    with h.service._mem:
        state = h.service._states[A]
        assert "m1" not in state.held and "m2" not in state.held
        assert {"m1", "m2"} <= {item.message_id for item in state.outbound}
    directions = {f["direction"] for f in logs.fields_for("link.bypass_admitted")}
    assert directions == {"release"}


def test_backlog_held_before_start_drains_at_startup() -> None:
    from tests.support.links.ledger import FakeInboxPort

    inbox = FakeInboxPort()
    inbox.hold(
        "m9", sender=A, target=B, reason=SELF_FULL, reply_to=None,
        created_at_ms=999_000, expires_at_ms=999_000 + 600_000,
    )
    fs = FakeOrgFs()
    h = make_service(fs=fs, inbox=inbox, local_names=("a", "b"), bypass=True)
    h.service.start()
    try:
        assert "m9" not in inbox.held
    finally:
        h.service.close(1.0)


# -- queries ---------------------------------------------------------------


def test_list_and_stats_report_the_switch() -> None:
    for bypass in (False, True):
        h = harness(bypass=bypass)
        h.tick()
        assert h.service.list_links(A)["bypass"] is bypass
        assert h.service.stats(A)["bypass"] is bypass
        assert h.service.bypass is bypass



def test_bypass_on_issues_zero_link_file_writes() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(
        fs=fs, local_names=("a", "b", "c"), bypass=True
    )
    h.tick()
    fs.calls.clear()
    fs.write_versions.clear()
    for target in (B, C, X):
        decision = send(h, target)
        assert isinstance(decision, Allow), decision
        assert decision.mint is False, decision
    h.tick()
    assert fs.write_versions == []
    assert [c for c in fs.calls if c[0] in ("mkdir", "write_text", "move", "remove")] == []
    assert all(not file.entries for file in h.store.index().files.values())


def test_bypass_off_still_writes_link_files_and_mints() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(fs=fs, local_names=("a", "b", "c"), bypass=False)
    h.tick()
    fs.write_versions.clear()
    assert send(h) == Mint(peer=B)
    hold_row(h, "m1")
    h.tick()
    assert fs.write_versions
    assert B in h.store.index().files[A].entries


def test_bypass_on_logs_bookkeeping_disabled_once() -> None:
    logs = LogRecorder()
    h = harness(bypass=True, logger=logs)
    h.tick()
    h.tick()
    fields = logs.fields_for("link.bookkeeping_disabled")
    assert len(fields) == 1
    assert fields[0]["reason"] == "bypass"



def test_bypass_bookkeeping_counter_both_arms() -> None:
    def run(bypass: bool) -> tuple[int, int]:
        fs = FakeOrgFs()
        fs.add_space("s1")
        h = make_service(fs=fs, local_names=("a", "b", "c"), bypass=bypass)
        h.tick()
        fs.calls.clear()
        fs.write_versions.clear()
        if bypass:
            assert isinstance(send(h), Allow)
        else:
            assert send(h) == Mint(peer=B)
            hold_row(h, "m1")
        h.tick()
        created = sum(
            1 for call in fs.calls if call[0] in ("mkdir", "write_text", "move")
        )
        removed = sum(1 for call in fs.calls if call[0] == "remove")
        return created, removed

    created_on, removed_on = run(True)
    created_off, removed_off = run(False)
    print(
        f"created_on_bypass={created_on} removed_on_bypass={removed_on} "
        f"created_off_bypass={created_off} removed_off_bypass={removed_off}"
    )
    assert created_on == 0 and created_off >= 1, (
        f"created_on_bypass={created_on}, created_off_bypass={created_off}, "
        f"removed_on_bypass={removed_on}, removed_off_bypass={removed_off}"
    )


def test_link_add_under_bypass_does_not_claim_established() -> None:
    h = harness(bypass=True)
    h.tick()

    added = h.service.add(actor=A, peer=B, now_ms=h.clock.now_ms())

    assert added["state"] == "not-recorded", added
    assert added["reason"] == "bookkeeping-disabled-by-bypass"
    assert h.service.list_links(A)["bookkeeping"] is False
