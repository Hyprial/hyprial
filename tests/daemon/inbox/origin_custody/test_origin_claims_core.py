"""Core origin-claim exclusion, batch deferral, non-holding readers, and doorbell tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.support.actor_hosts import new_actor_host

from .support import (
    RecordingHarnesses,
    SilentEvents,
    build_actor_app,
    build_bridge,
    close_app,
    make_message,
    make_notice,
    register_managed_worker,
    register_session,
)


def _new_facade(tmp_path: Path, *, session_current=None):
    db = tmp_path / "inbox.sqlite3"
    host = new_actor_host()
    coordinator = DeliveryCustodyCoordinator(
        db,
        MemoryDeliveryTransport(),
        SilentEvents(),
        node_id="node-1",
        actor_host=host,
        session_current=session_current,
    )
    return DeliveryCustodyFacade(coordinator, db), host


def test_batch_selection_equals_claimed_set(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, host = _new_facade(
        tmp_path, session_current=lambda actor, ref: (actor, ref) == ("agent:op:node-1:w", "s-1")
    )
    recipient = "agent:op:node-1:w"
    try:
        assert facade.receive(make_message("m1", recipient, created_at_ms=1)).accepted
        assert facade.receive(make_message("m2", recipient, created_at_ms=2)).accepted

        claimant_a = OriginClaimant.session(recipient, "s-1")
        claimant_b = OriginClaimant.headless(recipient)

        first = facade.batch_snapshot(
            recipient,
            keys=(recipient,),
            only=("m1",),
            mark_fetched=True,
            origin_claim=claimant_a,
        )
        assert [r.message.message_id for r in first.rows] == ["m1"]
        assert facade.has_fetched("m1") is True

        second = facade.batch_snapshot(
            recipient,
            keys=(recipient,),
            mark_fetched=False,
            origin_claim=claimant_b,
        )
        assert [r.message.message_id for r in second.rows] == ["m2"]
        assert [m.message_id for m in second.deferred] == ["m1"]
        assert facade.has_fetched("m2") is False
    finally:
        facade.close()
        host.shutdown()


def test_claimed_row_returns_through_deferred_after_release_and_recaptured_by_successor(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.inbox.batch.service import capture_batch
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, host = _new_facade(tmp_path)
    recipient = "agent:op:node-1:w"
    try:
        assert facade.receive(make_message("m1", recipient, created_at_ms=1)).accepted
        assert facade.receive(make_message("m2", recipient, created_at_ms=2)).accepted

        headless = OriginClaimant.headless(recipient)
        session = OriginClaimant.session(recipient, "s-1")

        claimed = facade.claim_origin(recipient, ("m1",), headless)
        assert claimed.claimed_ids == ("m1",)
        assert claimed.claim_seq is not None

        all_held_only = capture_batch(
            facade,
            recipient,
            keys=(recipient,),
            only=("m1",),
            origin_claim=session,
        )
        assert all_held_only.response["itemCount"] == 0
        assert all_held_only.cursor == ""

        mixed = capture_batch(
            facade,
            recipient,
            keys=(recipient,),
            origin_claim=session,
        )
        assert mixed.message_ids == ("m2",)
        assert mixed.cursor != ""

        assert facade.release_origin_claims(
            ("m1",), owner_actor=recipient, claim_seq=claimed.claim_seq
        ) is True

        successor = capture_batch(
            facade,
            recipient,
            keys=(recipient,),
            after_token=mixed.cursor,
            origin_claim=session,
        )
        assert successor.message_ids == ("m1",)
    finally:
        facade.close()
        host.shutdown()


def test_notices_are_outside_origin_claims(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.batch.service import capture_batch
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, host = _new_facade(tmp_path)
    recipient = "agent:op:node-1:w"
    try:
        assert facade.receive(make_message("m1", recipient, created_at_ms=1)).accepted
        assert facade.receive_system_notice(make_notice("n1", recipient, created_at_ms=2))

        headless = OriginClaimant.headless(recipient)
        assert facade.claim_origin(recipient, ("m1",), headless).claimed_ids == ("m1",)

        view = capture_batch(
            facade,
            recipient,
            keys=(recipient,),
            origin_claim=OriginClaimant.session(recipient, "s-1"),
        )
        assert view.message_ids == ("n1",)
        assert facade.has_fetched("m1") is False
    finally:
        facade.close()
        host.shutdown()


def test_worker_ref_reads_its_own_headless_claim(tmp_path: Path) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("worker",))
    try:
        harnesses = RecordingHarnesses(("worker",))
        worker_uri = register_managed_worker(
            app, "worker", "worker-ref-1", harnesses=harnesses
        )
        assert facade.receive(make_message("m-spill", worker_uri)).accepted
        bridge = build_bridge(
            tmp_path,
            facade,
            harnesses,
            actor_host=app._agent_session_domains.actor_hosts.host_for("bridge"),
            harness_actor_uri=lambda name: app.agents.require(name).uri,
            desired_state=app.desired_state,
            actor_mode=True,
        )
        bridge.start()
        try:
            bridge._dispatch_harness_deliveries()
            deadline = time.monotonic() + 5.0
            while not harnesses.envelopes and time.monotonic() < deadline:
                time.sleep(0.01)
            assert harnesses.envelopes
            assert facade.has_fetched("m-spill") is False

            body = app.handle(
                "message.read_body",
                {"actor": "worker", "sessionRef": "worker-ref-1", "messageId": "m-spill"},
            )
            assert body["ok"] is True
            assert body["message"] == "body-m-spill"

            batch = app.handle(
                "message.pending.batch",
                {"actor": "worker", "sessionRef": "worker-ref-1"},
            )
            assert [item["messageId"] for item in batch["items"]] == ["m-spill"]
        finally:
            bridge.stop()
    finally:
        close_app(app, facade)


def test_principal_read_respects_headless_claim(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = app.agents.require("alpha").uri
        assert facade.receive(make_message("m1", alpha_uri)).accepted
        assert facade.receive(make_message("m2", alpha_uri)).accepted

        assert facade.claim_origin(
            alpha_uri, ("m1",), OriginClaimant.headless(alpha_uri)
        ).claimed_ids == ("m1",)

        with pytest.raises(DaemonRequestError) as refused:
            app.handle(
                "message.read_body",
                {"actor": "user:op", "onBehalfOf": "alpha", "messageId": "m1"},
            )
        assert refused.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
        assert facade.has_fetched("m1") is False

        batch = app.handle(
            "message.pending.batch",
            {"actor": "user:op", "onBehalfOf": "alpha"},
        )
        assert [item["messageId"] for item in batch["items"]] == ["m2"]
        assert facade.has_fetched("m1") is False
    finally:
        close_app(app, facade)


def test_principal_acquisition_holds_no_claim(tmp_path: Path) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-a")
        assert facade.receive(make_message("m1", alpha_uri)).accepted
        assert facade.receive(make_message("m2", alpha_uri)).accepted

        body = app.handle(
            "message.read_body",
            {"actor": "user:op", "onBehalfOf": "alpha", "messageId": "m1"},
        )
        assert body["ok"] is True
        assert "m1" not in facade.read_origin_claims()

        batch = app.handle(
            "message.pending.batch",
            {"actor": "user:op", "onBehalfOf": "alpha"},
        )
        assert {item["messageId"] for item in batch["items"]} == {"m1", "m2"}
        assert facade.read_origin_claims() == {}

        listed = app.handle(
            "message.pending.list",
            {"actor": "alpha", "sessionRef": "session-a", "fetched": True},
        )
        assert {row["messageId"] for row in listed["messages"]} == {"m1", "m2"}
        session_batch = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert {item["messageId"] for item in session_batch["items"]} == {"m1", "m2"}
    finally:
        close_app(app, facade)


def test_worker_ref_without_headless_claim_holds_nothing(tmp_path: Path) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("worker",))
    try:
        harnesses = RecordingHarnesses(("worker",))
        worker_uri = register_managed_worker(
            app, "worker", "worker-ref-1", harnesses=harnesses
        )
        assert facade.receive(make_message("m1", worker_uri)).accepted
        assert facade.receive(make_message("m2", worker_uri)).accepted

        batch = app.handle(
            "message.pending.batch",
            {"actor": "worker", "sessionRef": "worker-ref-1"},
        )
        assert [item["messageId"] for item in batch["items"]] == ["m1", "m2"]
        assert facade.read_origin_claims() == {}

        body = app.handle(
            "message.read_body",
            {"actor": "worker", "sessionRef": "worker-ref-1", "messageId": "m1"},
        )
        assert body["ok"] is True
        assert facade.read_origin_claims() == {}
    finally:
        close_app(app, facade)


def test_receive_drops_residual_claim_for_same_id(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    facade, host = _new_facade(
        tmp_path, session_current=lambda actor, ref: (actor, ref) == ("agent:op:node-1:w", "s-1")
    )
    recipient = "agent:op:node-1:w"
    try:
        assert facade.receive(make_message("m-reuse", recipient, expires_at_ms=50), now_ms=10).accepted
        assert facade.fetch_pending(
            recipient,
            now_ms=15,
            only=frozenset({"m-reuse"}),
            origin_claim=OriginClaimant.session(recipient, "s-1"),
        )
        assert "m-reuse" in facade.read_origin_claims()
        assert facade.ack(recipient, "m-reuse").acknowledged is True
        assert "m-reuse" not in facade.read_origin_claims()

        assert facade.fetch_pending(
            recipient,
            now_ms=16,
            origin_claim=OriginClaimant.session(recipient, "s-1"),
        ) == ()
        with facade._coordinator._active_handler._service._db:  # noqa: SLF001
            facade._coordinator._active_handler._service._db.execute(  # noqa: SLF001
                "DELETE FROM dedup WHERE dedup_key = ?", ("msgid:m-reuse",)
            )
        assert facade.receive(make_message("m-reuse", recipient), now_ms=200).accepted
        taken = facade.fetch_pending(
            recipient,
            now_ms=201,
            only=frozenset({"m-reuse"}),
            origin_claim=OriginClaimant.headless(recipient),
        )
        assert [m.message_id for m in taken] == ["m-reuse"]
    finally:
        facade.close()
        host.shutdown()


def test_channel_doorbell_does_not_take_origin_custody(tmp_path: Path) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-a")
        assert facade.receive(make_message("m-doorbell", alpha_uri)).accepted

        listed = app.handle(
            "message.pending.list",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert [row["messageId"] for row in listed["messages"]] == ["m-doorbell"]
        waited = app.handle(
            "message.pending.wait",
            {"actor": "alpha", "sessionRef": "session-a", "knownMessageIds": []},
        )
        assert waited["changed"] is True
        assert facade.read_origin_claims() == {}
        assert facade.has_fetched("m-doorbell") is False
    finally:
        close_app(app, facade)


def test_batch_cursor_replay_skips_row_taken_by_another_owner(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.batch.service import capture_batch
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    active = {("agent:op:node-1:w", "s-1"), ("agent:op:node-1:w", "s-2")}
    facade, host = _new_facade(
        tmp_path, session_current=lambda actor, ref: (actor, ref) in active
    )
    recipient = "agent:op:node-1:w"
    try:
        assert facade.receive(make_message("m1", recipient, created_at_ms=1)).accepted
        assert facade.receive(make_message("m2", recipient, created_at_ms=2)).accepted

        s1 = OriginClaimant.session(recipient, "s-1")
        s2 = OriginClaimant.session(recipient, "s-2")

        first = capture_batch(facade, recipient, keys=(recipient,), origin_claim=s1)
        assert first.message_ids == ("m1", "m2")

        active.discard((recipient, "s-1"))
        taken = facade.fetch_pending(
            recipient, only=frozenset({"m1"}), origin_claim=s2
        )
        assert [m.message_id for m in taken] == ["m1"]

        active.add((recipient, "s-1"))
        replayed = capture_batch(
            facade,
            recipient,
            keys=(recipient,),
            cursor_token=first.cursor,
            origin_claim=s1,
        )
        assert replayed.message_ids == ("m2",)
    finally:
        facade.close()
        host.shutdown()
