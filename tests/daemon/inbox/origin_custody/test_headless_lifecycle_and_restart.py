"""Headless release CAS, generation restart, daemon restart, and stranded sweep tests."""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.startup import _StartupMixin
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)
from tests.support.actor_hosts import new_actor_host

from .support import (
    RecordingHarnesses,
    SilentEvents,
    build_bridge,
    make_message,
)


def _new_facade(tmp_path: Path, *, db_path: Path | None = None, session_current=None):
    db = db_path or (tmp_path / "inbox.sqlite3")
    host = new_actor_host()
    coordinator = DeliveryCustodyCoordinator(
        db,
        MemoryDeliveryTransport(),
        SilentEvents(),
        node_id="node-1",
        actor_host=host,
        session_current=session_current,
    )
    return DeliveryCustodyFacade(coordinator, db), coordinator, host


def test_origin_claims_survive_inbox_generation_restart(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, coordinator, host = _new_facade(tmp_path)
    recipient = "agent:op:node-1:worker"
    try:
        assert facade.receive(make_message("m1", recipient)).accepted
        claimed = facade.claim_origin(
            recipient, ("m1",), OriginClaimant.headless(recipient)
        )
        assert claimed.claimed_ids == ("m1",)
        gen_before = coordinator.generation

        def boom(*_args, **_kwargs):
            raise RuntimeError("injected handler fault")

        coordinator._active_handler._service.fail = boom  # type: ignore[method-assign]  # noqa: SLF001
        with pytest.raises(Exception):
            facade.fail(recipient, "m1", "boom")

        deadline = time.monotonic() + 5.0
        while coordinator.generation == gen_before and time.monotonic() < deadline:
            time.sleep(0.01)
        assert coordinator.generation > gen_before

        assert facade.fetch_pending(
            recipient,
            only=frozenset({"m1"}),
            origin_claim=OriginClaimant.session(recipient, "session-1"),
        ) == ()
        assert "m1" in facade.read_origin_claims()
    finally:
        facade.close()
        host.shutdown()


def test_unfetched_origin_claim_expires_at_daemon_restart(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    db = tmp_path / "restart.sqlite3"
    recipient = "agent:op:node-1:worker"
    facade1, _c1, host1 = _new_facade(tmp_path, db_path=db)
    try:
        assert facade1.receive(make_message("m1", recipient)).accepted
        assert facade1.claim_origin(
            recipient, ("m1",), OriginClaimant.headless(recipient)
        ).claimed_ids == ("m1",)
        assert facade1.has_fetched("m1") is False
    finally:
        facade1.close()
        host1.shutdown()

    facade2, _c2, host2 = _new_facade(tmp_path, db_path=db)
    try:
        assert facade2.read_origin_claims() == {}
        taken = facade2.fetch_pending(
            recipient,
            only=frozenset({"m1"}),
            origin_claim=OriginClaimant.session(recipient, "session-1"),
        )
        assert [m.message_id for m in taken] == ["m1"]
    finally:
        facade2.close()
        host2.shutdown()


def test_fetched_request_row_after_restart_has_one_interactive_owner(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    db = tmp_path / "restart-fetched.sqlite3"
    recipient = "agent:op:node-1:worker"
    facade1, _c1, host1 = _new_facade(tmp_path, db_path=db)
    try:
        assert facade1.receive(make_message("m-req", recipient, intent="request")).accepted
        assert facade1.fetch_pending(
            recipient,
            only=frozenset({"m-req"}),
            origin_claim=OriginClaimant.session(recipient, "session-1"),
        )
    finally:
        facade1.close()
        host1.shutdown()

    facade2, _c2, host2 = _new_facade(
        tmp_path,
        db_path=db,
        session_current=lambda actor, ref: (actor, ref) == (recipient, "session-1"),
    )
    try:
        assert facade2.dispatchable_messages(recipient) == ()
        taken = facade2.fetch_pending(
            recipient,
            only=frozenset({"m-req"}),
            origin_claim=OriginClaimant.session(recipient, "session-1"),
        )
        assert [m.message_id for m in taken] == ["m-req"]
    finally:
        facade2.close()
        host2.shutdown()


def test_failure_retry_row_after_restart_has_one_holder(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    db = tmp_path / "restart-retry.sqlite3"
    recipient = "agent:op:node-1:worker"
    now_ms = int(time.time() * 1000)
    facade1, _c1, host1 = _new_facade(tmp_path, db_path=db)
    try:
        assert facade1.receive(make_message("m-retry", recipient, intent="request")).accepted
        facade1.fetch_pending(
            recipient,
            only=frozenset({"m-retry"}),
            origin_claim=OriginClaimant.headless(recipient),
        )
        facade1.settle_harness_failure(
            recipient,
            "m-retry",
            "TRANSIENT",
            permanent=False,
            max_attempts=3,
            backoff_ms=(1,),
            now_ms=now_ms - 100,
        )
    finally:
        facade1.close()
        host1.shutdown()

    facade2, _c2, host2 = _new_facade(
        tmp_path,
        db_path=db,
        session_current=lambda actor, ref: (actor, ref) == (recipient, "session-1"),
    )
    try:
        harnesses = RecordingHarnesses(("worker",))
        bridge = build_bridge(
            tmp_path,
            facade2,
            harnesses,
            actor_host=host2,
            harness_actor_uri=lambda name: f"agent:op:node-1:{name}",
        )
        bridge.start()
        try:
            assert facade2.pending_messages(recipient)
            assert facade2.claim_origin(
                recipient, ("m-retry",), OriginClaimant.headless(recipient), now_ms=now_ms
            ).claimed_ids == ("m-retry",)
            assert facade2.fetch_pending(
                recipient,
                only=frozenset({"m-retry"}),
                origin_claim=OriginClaimant.session(recipient, "session-1"),
            ) == ()
        finally:
            bridge.stop()
    finally:
        facade2.close()
        host2.shutdown()


@pytest.mark.parametrize("claim_first", [False, True])
def test_stranded_sweep_settles_independently_of_origin_claims(
    tmp_path: Path, claim_first: bool
) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, _c, host = _new_facade(
        tmp_path,
        session_current=lambda actor, ref: (actor, ref) == ("agent:op:node-1:worker", "session-1"),
    )
    recipient = "agent:op:node-1:worker"
    try:
        assert facade.receive(make_message("m-note", recipient, intent="note")).accepted
        assert facade.receive(make_message("m-req", recipient, intent="request")).accepted
        facade.fetch_pending(recipient, only=frozenset({"m-note", "m-req"}))

        session_claim = OriginClaimant.session(recipient, "session-1")
        if claim_first:
            facade.fetch_pending(
                recipient,
                only=frozenset({"m-note", "m-req"}),
                origin_claim=session_claim,
            )

        harnesses = RecordingHarnesses(("worker",))
        bridge = build_bridge(
            tmp_path,
            facade,
            harnesses,
            actor_host=host,
            harness_actor_uri=lambda name: f"agent:op:node-1:{name}",
        )
        bridge.start()
        try:
            if not claim_first:
                facade.fetch_pending(
                    recipient,
                    only=frozenset({"m-note", "m-req"}),
                    origin_claim=session_claim,
                )
            assert facade.pending_messages(recipient) == (
                [m for m in facade.pending_messages(recipient) if m.message_id == "m-req"][0],
            )
            ack_note = facade.ack(recipient, "m-note")
            assert ack_note.acknowledged is True
            assert ack_note.code == "MESSAGE_ALREADY_SETTLED"
            assert "m-note" not in facade.read_origin_claims()
            if claim_first:
                assert "m-req" in facade.read_origin_claims()
        finally:
            bridge.stop()
    finally:
        facade.close()
        host.shutdown()


def test_stranded_sweep_runs_before_ipc_accept() -> None:
    source = inspect.getsource(_StartupMixin.run)
    runtime_idx = source.find("_start_runtime")
    server_idx = source.find("_start_server")
    assert runtime_idx != -1
    assert server_idx != -1
    assert runtime_idx < server_idx


def test_headless_offer_refusal_releases_origin_claim(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, _c, host = _new_facade(
        tmp_path,
        session_current=lambda actor, ref: (actor, ref) == ("agent:op:node-1:worker", "session-1"),
    )
    recipient = "agent:op:node-1:worker"
    try:
        assert facade.receive(make_message("m1", recipient)).accepted
        harnesses = RecordingHarnesses(("worker",), answer=False)
        bridge = build_bridge(
            tmp_path,
            facade,
            harnesses,
            actor_host=host,
            harness_actor_uri=lambda name: f"agent:op:node-1:{name}",
        )
        bridge.start()
        try:
            bridge._dispatch_harness_deliveries()
            deadline = time.monotonic() + 5.0
            while not harnesses.envelopes and time.monotonic() < deadline:
                time.sleep(0.01)
            deadline = time.monotonic() + 5.0
            while "m1" in facade.read_origin_claims() and time.monotonic() < deadline:
                bridge._drain_dispatch_offer_outcomes()
                time.sleep(0.01)
            assert "m1" not in facade.read_origin_claims()
        finally:
            bridge.stop()

        first = facade.claim_origin(recipient, ("m1",), OriginClaimant.headless(recipient))
        assert first.claim_seq is not None
        assert facade.release_origin_claims(
            ("m1",), owner_actor=recipient, claim_seq=first.claim_seq + 99
        ) is False
        assert "m1" in facade.read_origin_claims()
        assert facade.release_origin_claims(
            ("m1",), owner_actor=recipient, claim_seq=first.claim_seq
        ) is True
        assert "m1" not in facade.read_origin_claims()
    finally:
        facade.close()
        host.shutdown()


def test_headless_claim_survives_batch_fetch_stamp_timeout(tmp_path: Path, monkeypatch) -> None:
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from hyprial.daemon.impl.inbox.actor.errors import InboxAuthorityTimeout
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant
    from hyprial.kernel import HarnessLaunchSpec

    facade, _c, host = _new_facade(tmp_path)
    recipient = "agent:op:node-1:worker"
    try:
        assert facade.receive(make_message("m1", recipient)).accepted
        harnesses = RecordingHarnesses(("worker",), answer=True)
        desired_state = DesiredStateStore(tmp_path / "desired-state.json")
        desired_state.upsert_harness(
            HarnessLaunchSpec(
                name="worker", harness="codex", cwd="/work/worker", headless=True
            )
        )
        bridge = build_bridge(
            tmp_path,
            facade,
            harnesses,
            actor_host=host,
            harness_actor_uri=lambda name: f"agent:op:node-1:{name}",
            desired_state=desired_state,
        )
        bridge.start()
        try:
            real_fetch = facade.fetch_pending
            calls = {"n": 0}

            def flaky_fetch(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise InboxAuthorityTimeout("simulated stamp timeout")
                return real_fetch(*args, **kwargs)

            monkeypatch.setattr(facade, "fetch_pending", flaky_fetch)
            bridge._dispatch_harness_deliveries()
            deadline = time.monotonic() + 5.0
            while calls["n"] == 0 and time.monotonic() < deadline:
                bridge._drain_dispatch_offer_outcomes()
                time.sleep(0.01)
            assert calls["n"] >= 1
            assert facade.has_fetched("m1") is False
            assert facade.fetch_pending(
                recipient,
                only=frozenset({"m1"}),
                origin_claim=OriginClaimant.session(recipient, "s-1"),
            ) == ()
            bridge._retry_batch_fetches()
            assert facade.has_fetched("m1") is True
            assert len(harnesses.envelopes) == 1
        finally:
            bridge.stop()
    finally:
        facade.close()
        host.shutdown()


def test_origin_release_and_ack_race_is_single_winner(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, _c, host = _new_facade(tmp_path)
    recipient = "agent:op:node-1:worker"
    try:
        assert facade.receive(make_message("m1", recipient)).accepted
        claim = facade.claim_origin(recipient, ("m1",), OriginClaimant.headless(recipient))
        assert claim.claim_seq is not None

        first_ack = facade.ack(recipient, "m1")
        assert first_ack.acknowledged is True
        assert first_ack.code is None
        assert "m1" not in facade.read_origin_claims()

        assert facade.release_origin_claims(
            ("m1",), owner_actor=recipient, claim_seq=claim.claim_seq
        ) is False
        second_ack = facade.ack(recipient, "m1")
        assert second_ack.acknowledged is True
        assert second_ack.code == "MESSAGE_ALREADY_SETTLED"
    finally:
        facade.close()
        host.shutdown()


def test_headless_keep_releases_origin_claim_after_turn_end(tmp_path: Path) -> None:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, _c, host = _new_facade(
        tmp_path,
        session_current=lambda actor, ref: (actor, ref) == ("agent:op:node-1:worker", "session-1"),
    )
    recipient = "agent:op:node-1:worker"
    try:
        assert facade.receive(make_message("m-keep", recipient, intent="request")).accepted
        harnesses = RecordingHarnesses(("worker",), answer=True)
        bridge = build_bridge(
            tmp_path,
            facade,
            harnesses,
            actor_host=host,
            harness_actor_uri=lambda name: f"agent:op:node-1:{name}",
            actor_mode=False,
        )
        bridge.start()
        try:
            assert bridge._dispatch_harness_deliveries() == 1
            assert "m-keep" in facade.read_origin_claims()
            harnesses.drain_results = lambda: (  # type: ignore[method-assign]
                HarnessResult(
                    delivery_id="m-keep",
                    recipient=recipient,
                    status=HarnessResultStatus.COMPLETED,
                    output="ok",
                ),
            )
            assert bridge._complete_harness_results() == 1
            assert "m-keep" not in facade.read_origin_claims()
        finally:
            bridge.stop()

        taken = facade.fetch_pending(
            recipient,
            only=frozenset({"m-keep"}),
            origin_claim=OriginClaimant.session(recipient, "session-1"),
        )
        assert [m.message_id for m in taken] == ["m-keep"]
    finally:
        facade.close()
        host.shutdown()


def test_headless_spill_unavailable_releases_origin_claim(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.batch.service import capture_batch
    from hyprial.daemon.impl.inbox.batch.spill import SpillUnavailable
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, _c, host = _new_facade(tmp_path)
    recipient = "agent:op:node-1:worker"
    try:
        assert facade.receive(make_message("m-big", recipient, body="x" * 2048)).accepted
        with pytest.raises(SpillUnavailable):
            capture_batch(
                facade,
                recipient,
                keys=(recipient,),
                limit_bytes=1024,
                actor_home=None,
                mark_fetched=False,
                origin_claim=OriginClaimant.headless(recipient),
            )
        assert "m-big" not in facade.read_origin_claims()
    finally:
        facade.close()
        host.shutdown()
