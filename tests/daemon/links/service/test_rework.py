"""WP2-A rework tests (first acceptance: probes P-1, P-3, P-4; case H14).

Spec: wp2.md §2.2 step 1.4, §3.5 (thread confinement), §3.6 / §7 H14; the
rework list is ``wp2a-acceptance-1.md`` §6.  Rework 3 adds the pending-file
slot probe Q2b and the ruling-70 settled-id cases (``wp2a-acceptance-2.md``
§6 items 1–2).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import replace

import pytest

import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.model import LinkEntry, LinkFile, with_reference
from hyprial.daemon.impl.links.ports import Allow
from hyprial.kernel import DaemonRequestError, ipc_errors

from tests.daemon.links.service.test_hold_release import hold_row, pin
from tests.support.links.org import (
    BlockPoint,
    FakeOrgFs,
    establish,
    make_service,
)

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"
D = "agent:o:n1:d"
U = "user:p"
MINTING = ipc_errors.LINK_MINTING


def _fill_w(h, now: int) -> None:
    """Occupy the whole window W of A with unanswered requests to ``user:p``."""

    w = h.service.stats(A)["W"]
    for index in range(w):
        h.service.submitted(
            sender=A, target=U, message_id=f"fill{index}", reply_to=None,
            hold_reason=None, now_ms=now,
        )


def _blocked_world(*, linked: bool = True):
    write = BlockPoint()
    fs = FakeOrgFs(write_block=write)
    fs.add_space("s1")
    write.go()
    h = make_service(fs=fs, local_names=("a", "b", "c", "d"))
    if linked:
        establish(fs, A, B)
    h.tick()
    return h, write


# -- P-1 -----------------------------------------------------------------


def test_p1_reply_to_a_non_owner_user_is_allowed_when_w_is_full() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(fs=fs)
    establish(fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.inbox.inject(A, "mu-req", sender=U, received_at_ms=now)
    _fill_w(h, now)
    # Ruling 53 still refuses a NEW request to a non-owner user ...
    with pytest.raises(DaemonRequestError) as refusal:
        h.service.admit_send(
            sender=A, target=U, intent="request", reply_to=None, now_ms=now
        )
    assert refusal.value.code == ipc_errors.LINK_WINDOW_FULL
    # ... but a reply closes an open item and must never be refused (§2.2 1.4).
    assert h.service.admit_send(
        sender=A, target=U, intent="reply", reply_to="mu-req", now_ms=now
    ) == Allow(peer=None)


# -- P-3 -----------------------------------------------------------------


def test_p3_commit_keeps_an_intent_that_changed_during_the_flush() -> None:
    h, write = _blocked_world()
    base = h.store.index().files[A]
    first = with_reference(base, LinkEntry(peer=C, minted_at_ms=1, revision=0))
    second = with_reference(first, LinkEntry(peer=D, minted_at_ms=2, revision=0))
    h.store.write(first)
    write.reset()
    flusher = threading.Thread(target=h.store.flush, daemon=True)
    flusher.start()
    assert write.wait_entered(5.0)
    # The newer intent lands while the first is mid-write; the commit must not
    # clear an intent it did not write (CAS).
    h.store.write(second)
    write.go()
    flusher.join(5.0)
    assert h.store.intents().get(A) == second
    h.store.flush()
    assert h.store.index().files[A].revision == second.revision
    assert h.store.intents().get(A) is None


def test_p3_a_drop_racing_a_flush_is_not_lost() -> None:
    h, write = _blocked_world()
    now = h.clock.now_ms()
    # A real local write staged for the next pass: store.flush blocks on it.
    # It is C's own (empty) file, so it cannot create a link edge that would
    # oscillate the A/B pair under test.
    h.store.write(LinkFile(actor=C, revision=1, entries={}))
    write.reset()
    ticker = threading.Thread(target=lambda: h.service.tick(now), daemon=True)
    ticker.start()
    assert write.wait_entered(5.0), "the pass never reached the orgfs write"
    # The operator drop arrives while the service thread sits inside the flush.
    outcome = h.service.drop(actor=A, peer=B, now_ms=now)
    assert outcome["state"] == "reclaiming" and outcome["written"] is False
    write.go()
    ticker.join(5.0)
    assert not ticker.is_alive()
    for step in range(1, 7):
        h.tick(now + step * 1_000)
    files = h.store.index().files
    assert B not in files[A].entries, "the retraction was lost"
    assert A not in files[B].entries
    peers = {row["peer"] for row in h.service.list_links(B)["links"]}
    assert A not in peers
    with h.service._mem:
        assert A in h.service._states[B].reclaimed


def test_p3_a_mint_racing_a_flush_is_not_lost() -> None:
    h, write = _blocked_world(linked=False)
    now = h.clock.now_ms()
    assert h.service.add(actor=A, peer=C, now_ms=now)["state"] == "pending"
    write.reset()
    ticker = threading.Thread(target=lambda: h.service.tick(now), daemon=True)
    ticker.start()
    assert write.wait_entered(5.0)
    # Concurrent mint while the service thread is inside the flush.
    assert h.service.add(actor=A, peer=D, now_ms=now)["state"] == "pending"
    write.go()
    ticker.join(5.0)
    for step in range(1, 6):
        h.tick(now + step * 1_000)
    entries = h.store.index().files[A].entries
    assert C in entries and D in entries


def test_p3_admin_calls_only_enqueue_on_the_calling_thread() -> None:
    h, _write = _blocked_world()
    now = h.clock.now_ms()
    writes_before = len(h.fs.calls)
    applies_before = len(h.inbox.applies)
    outcome = h.service.drop(actor=A, peer=B, now_ms=now)
    assert outcome["state"] == "reclaiming" and outcome["written"] is False
    assert len(h.fs.calls) == writes_before
    assert len(h.inbox.applies) == applies_before
    assert h.store.intents().get(A) is None
    assert h.service._states[A].pending_reclaims == [(B, "dropped", now)]
    h.tick(now + 1_000)
    assert B not in h.store.index().files[A].entries


# -- P-4 -----------------------------------------------------------------


def test_p4_refreshing_presence_never_breaks_a_concurrent_dispatch() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(fs=fs)
    establish(fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    errors: list[BaseException] = []
    stop = threading.Event()

    def churn() -> None:
        try:
            for index in range(4_000):
                h.presence.set([f"agent:o:n1:n{index}"])
                h.service._refresh_seen(now + index)
        except BaseException as error:  # noqa: BLE001 - recorded for the assert
            errors.append(error)
        finally:
            stop.set()

    def dispatch() -> None:
        index = 0
        try:
            while not stop.is_set():
                h.service.admit_dispatch(
                    recipient=A, sender=B, message_id=f"p{index}", now_ms=now
                )
                index += 1
        except BaseException as error:  # noqa: BLE001 - recorded for the assert
            errors.append(error)
            stop.set()

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=churn), threading.Thread(target=dispatch)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30.0)
    finally:
        sys.setswitchinterval(interval)
    assert errors == []


# -- H14 -----------------------------------------------------------------


def test_h14_a_row_expiring_inside_the_pass_is_only_expired() -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(fs=fs)
    establish(fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    pin(h, A, w=8, cwnd={B: 5})
    hold_row(h, "h1", reason=MINTING, at=now + 1)
    results = []
    real_apply = h.inbox.apply

    def racing(update, *, now_ms):
        row = h.inbox.held.get("h1")
        if row is not None:
            # The deadline passes between the evaluation and the transaction.
            h.inbox.held["h1"] = replace(row, expires_at_ms=now_ms)
        result = real_apply(update, now_ms=now_ms)
        results.append((update, result))
        return result

    h.inbox.apply = racing
    h.tick()
    released = [r for _u, r in results if "h1" in r.released]
    expired = [r for _u, r in results if "h1" in r.expired]
    assert any("h1" in update.release for update, _r in results)
    assert released == [] and len(expired) == 1
    with h.service._mem:
        assert h.service._states[A].outbound == []
    assert h.service.top_fields(A)["outboundInFlight"] == 0


# -- Rework 3, Q2b: the pending slot covers the file handed to the store --


def test_q2b_a_withdrawal_blocked_in_store_write_is_not_resurrected() -> None:
    """A mint racing a blocked withdrawal must build on the withdrawn file.

    Deterministic replay of acceptance-2 probe Q2b: the withdrawal is blocked
    inside ``store.write``; while it sits there the pending slot must still
    cover the file, so a concurrent ``add`` mints from the post-withdrawal
    file.  After the release the committed file of A never contains B again
    and every committed revision of A's file is strictly increasing.
    """

    h, _fs_write = _blocked_world()
    now = h.clock.now_ms()
    assert h.service.drop(actor=A, peer=B, now_ms=now)["state"] == "reclaiming"
    block = BlockPoint()
    hit = threading.Event()
    real_write = h.store.write

    def blocking_write(file: LinkFile) -> None:
        if file.actor == A and B not in file.entries and not hit.is_set():
            hit.set()
            block.wait()
        real_write(file)

    h.store.write = blocking_write
    commits: list[int] = []
    real_flush = h.store.flush

    def recording_flush() -> tuple[str, ...]:
        before = h.store.index().files[A].revision
        result = real_flush()
        after = h.store.index().files[A].revision
        if after != before:
            commits.append(after)
        return result

    h.store.flush = recording_flush
    ticker = threading.Thread(target=lambda: h.service.tick(now), daemon=True)
    ticker.start()
    assert block.wait_entered(5.0), "the withdrawal never reached store.write"
    # The mint lands while the withdrawal write is in flight.
    assert h.service.add(actor=A, peer=C, now_ms=now)["state"] == "pending"
    block.go()
    ticker.join(5.0)
    assert not ticker.is_alive()
    for step in range(1, 5):
        h.tick(now + step * 1_000)
    entries = h.store.index().files[A].entries
    assert B not in entries, "the withdrawn peer was written back"
    assert C in entries
    assert len(commits) >= 2
    assert all(older < newer for older, newer in zip(commits, commits[1:])), commits


# -- Rework 3, ruling 70: settled inbound ids are never readmitted --------


def _r70_readmission_case(state: str) -> None:
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(fs=fs)
    establish(fs, A, B)
    h.tick()
    now = h.clock.now_ms()
    h.inbox.put_in(A, "m1", peer=B, state="open", opened_at_ms=now)
    assert h.service.admit_dispatch(
        recipient=A, sender=B, message_id="m1", now_ms=now
    )
    # The request settles in the ledger; the next reconcile must record it.
    row = h.inbox.rows[(A, "m1")]
    h.inbox.rows[(A, "m1")] = replace(row, state=state)
    h.tick(now + 1_000)
    with h.service._mem:
        assert h.service._states[A].pending_inbound == {}
    # Ruling 70: a second admission is idempotent-True, but the settled id
    # must not re-enter the window, take a W seat or count as in-flight.
    assert h.service.admit_dispatch(
        recipient=A, sender=B, message_id="m1", now_ms=now + 2_000
    )
    with h.service._mem:
        st = h.service._states[A]
        assert "m1" not in st.admitted
        assert "m1" not in st.pending_inbound
        assert "m1" in st.settled
        assert policy.unclosed(h.service._view(A, now + 2_000)) == 0


def test_r70_an_acked_request_is_settled_and_never_readmitted() -> None:
    _r70_readmission_case("acked")


def test_r70_an_expired_request_is_settled_and_never_readmitted() -> None:
    _r70_readmission_case("expired")
