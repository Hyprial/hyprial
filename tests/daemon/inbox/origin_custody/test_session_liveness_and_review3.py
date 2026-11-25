"""Session claim liveness, B5 no-stale-block invariants, lock order, and REVIEW3 cross-actor ref reuse."""

from __future__ import annotations

import threading
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
    register_session,
    unregister_session,
)


@pytest.mark.parametrize("acquirer_kind", ["new-session", "principal", "headless-retry"])
def test_cross_actor_session_ref_reuse_does_not_keep_dead_actor_claim_alive(
    tmp_path: Path, acquirer_kind: str
) -> None:
    """REVIEW3 mandatory regression: (actor, sessionRef) must bind claims.

    Actor A claims m1 under shared-ref, then unregisters.  Actor B registers
    with the identical shared-ref.  Because validity checks (actor, sessionRef)
    together, A's dead claim must yield to every subsequent acquirer of A.
    """
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha", "beta"))
    try:
        alpha_uri = register_session(app, "alpha", "shared-ref")
        assert facade.receive(make_message("m1", alpha_uri, created_at_ms=1)).accepted

        batch_a = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "shared-ref"},
        )
        assert [item["messageId"] for item in batch_a["items"]] == ["m1"]
        claims = facade.read_origin_claims()
        assert claims["m1"].owner_actor == alpha_uri
        assert claims["m1"].owner_id == "shared-ref"

        unregister_session(app, "alpha", "shared-ref")
        register_session(app, "beta", "shared-ref")

        if acquirer_kind == "new-session":
            register_session(app, "alpha", "session-a2")
            listed = app.handle(
                "message.pending.list",
                {"actor": "alpha", "sessionRef": "session-a2", "fetched": True},
            )
            assert [row["messageId"] for row in listed["messages"]] == ["m1"]
            batch_a2 = app.handle(
                "message.pending.batch",
                {"actor": "alpha", "sessionRef": "session-a2"},
            )
            assert [item["messageId"] for item in batch_a2["items"]] == ["m1"]
            assert facade.read_origin_claims()["m1"].owner_id == "session-a2"
        elif acquirer_kind == "principal":
            batch_op = app.handle(
                "message.pending.batch",
                {"actor": "user:op", "onBehalfOf": "alpha"},
            )
            assert [item["messageId"] for item in batch_op["items"]] == ["m1"]
            assert "m1" not in facade.read_origin_claims()
        else:
            from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

            now_ms = int(time.time() * 1000)
            facade.settle_harness_failure(
                alpha_uri,
                "m1",
                "TRANSIENT",
                permanent=False,
                max_attempts=3,
                backoff_ms=(1,),
                now_ms=now_ms - 100,
            )
            dispatched = facade.admitted_dispatchable(
                alpha_uri,
                now_ms=now_ms,
                origin_claim=OriginClaimant.headless(alpha_uri),
            )
            assert [m.message_id for m in dispatched] == ["m1"]
            taken = facade.claim_origin(
                alpha_uri,
                ("m1",),
                OriginClaimant.headless(alpha_uri),
                now_ms=now_ms,
            )
            assert taken.claimed_ids == ("m1",)
            assert facade.read_origin_claims()["m1"].owner_kind.value == "headless"
    finally:
        close_app(app, facade)


def test_superseded_session_claim_is_replaced_on_contention(tmp_path: Path) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-1")
        assert facade.receive(make_message("m1", alpha_uri)).accepted

        first = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-1"},
        )
        assert [item["messageId"] for item in first["items"]] == ["m1"]

        register_session(app, "alpha", "session-2")

        with pytest.raises(DaemonRequestError) as refused:
            app.handle(
                "message.pending.batch",
                {"actor": "alpha", "sessionRef": "session-1"},
            )
        assert refused.value.code == ipc_errors.SESSION_SUPERSEDED

        second = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-2"},
        )
        assert [item["messageId"] for item in second["items"]] == ["m1"]
        assert facade.read_origin_claims()["m1"].owner_id == "session-2"
    finally:
        close_app(app, facade)


def test_unregistered_session_claim_is_replaced_by_new_session(tmp_path: Path) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-1")
        assert facade.receive(make_message("m1", alpha_uri)).accepted
        app.handle(
            "message.pending.list",
            {"actor": "alpha", "sessionRef": "session-1", "fetched": True},
        )
        unregister_session(app, "alpha", "session-1")
        register_session(app, "alpha", "session-2")
        batch = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-2"},
        )
        assert [item["messageId"] for item in batch["items"]] == ["m1"]
        assert facade.read_origin_claims()["m1"].owner_id == "session-2"
    finally:
        close_app(app, facade)


def test_stale_session_claim_yields_on_headless_retry_path(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-1")
        assert facade.receive(make_message("m1", alpha_uri)).accepted
        app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-1"},
        )
        unregister_session(app, "alpha", "session-1")

        now_ms = int(time.time() * 1000)
        facade.settle_harness_failure(
            alpha_uri,
            "m1",
            "TRANSIENT",
            permanent=False,
            max_attempts=3,
            backoff_ms=(1,),
            now_ms=now_ms - 100,
        )
        admitted = facade.admitted_dispatchable(
            alpha_uri,
            now_ms=now_ms,
            origin_claim=OriginClaimant.headless(alpha_uri),
        )
        assert [m.message_id for m in admitted] == ["m1"]
        claimed = facade.claim_origin(
            alpha_uri,
            ("m1",),
            OriginClaimant.headless(alpha_uri),
            now_ms=now_ms,
        )
        assert claimed.claimed_ids == ("m1",)
        assert facade.read_origin_claims()["m1"].owner_kind.value == "headless"
    finally:
        close_app(app, facade)


@pytest.mark.parametrize(
    "case",
    ["ordinary-headless", "new-session", "principal-batch", "headless-retry"],
)
def test_stale_session_claim_on_request_row_blocks_no_acquirer(
    tmp_path: Path, case: str
) -> None:
    """B5 required test: stale session claim never creates a new block."""
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-1")
        assert facade.receive(make_message("m-req", alpha_uri, intent="request")).accepted
        app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-1"},
        )
        assert facade.has_fetched("m-req") is True
        unregister_session(app, "alpha", "session-1")

        if case == "ordinary-headless":
            harnesses = RecordingHarnesses(("alpha",))
            bridge = build_bridge(
                tmp_path,
                facade,
                harnesses,
                actor_host=app._agent_session_domains.actor_hosts.host_for("bridge"),
                harness_actor_uri=lambda _name: alpha_uri,
            )
            bridge.start()
            try:
                assert bridge._dispatch_harness_deliveries() == 0
                assert harnesses.envelopes == []
            finally:
                bridge.stop()

            db_plain = tmp_path / "plain.sqlite3"
            plain_host = new_actor_host()
            plain_coord = DeliveryCustodyCoordinator(
                db_plain, MemoryDeliveryTransport(), SilentEvents(), actor_host=plain_host
            )
            plain_facade = DeliveryCustodyFacade(plain_coord, db_plain)
            try:
                assert plain_facade.receive(make_message("m-req", alpha_uri, intent="request")).accepted
                plain_facade.fetch_pending(alpha_uri, only=frozenset({"m-req"}))
                plain_harnesses = RecordingHarnesses(("alpha",))
                plain_bridge = build_bridge(
                    tmp_path / "plain-bridge",
                    plain_facade,
                    plain_harnesses,
                    actor_host=plain_host,
                    harness_actor_uri=lambda _name: alpha_uri,
                )
                plain_bridge.start()
                try:
                    assert plain_bridge._dispatch_harness_deliveries() == 0
                    assert plain_harnesses.envelopes == []
                finally:
                    plain_bridge.stop()
            finally:
                plain_facade.close()
                plain_host.shutdown()
        elif case == "new-session":
            register_session(app, "alpha", "session-2")
            listed = app.handle(
                "message.pending.list",
                {"actor": "alpha", "sessionRef": "session-2", "fetched": True},
            )
            assert [row["messageId"] for row in listed["messages"]] == ["m-req"]
            batch = app.handle(
                "message.pending.batch",
                {"actor": "alpha", "sessionRef": "session-2"},
            )
            assert [item["messageId"] for item in batch["items"]] == ["m-req"]
            assert facade.read_origin_claims()["m-req"].owner_id == "session-2"
        elif case == "principal-batch":
            batch = app.handle(
                "message.pending.batch",
                {"actor": "user:op", "onBehalfOf": "alpha"},
            )
            assert [item["messageId"] for item in batch["items"]] == ["m-req"]
            assert "m-req" not in facade.read_origin_claims()
        else:
            from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

            now_ms = int(time.time() * 1000)
            facade.settle_harness_failure(
                alpha_uri,
                "m-req",
                "TRANSIENT",
                permanent=False,
                max_attempts=3,
                backoff_ms=(1,),
                now_ms=now_ms - 100,
            )
            admitted = facade.admitted_dispatchable(
                alpha_uri,
                now_ms=now_ms,
                origin_claim=OriginClaimant.headless(alpha_uri),
            )
            assert [m.message_id for m in admitted] == ["m-req"]
            claimed = facade.claim_origin(
                alpha_uri,
                ("m-req",),
                OriginClaimant.headless(alpha_uri),
                now_ms=now_ms,
            )
            assert claimed.claimed_ids == ("m-req",)
    finally:
        close_app(app, facade)


def test_same_session_ref_reregister_keeps_claim(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-1")
        assert facade.receive(make_message("m1", alpha_uri)).accepted
        app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-1"},
        )
        unregister_session(app, "alpha", "session-1")
        register_session(app, "alpha", "session-1")

        assert facade.claim_origin(
            alpha_uri, ("m1",), OriginClaimant.headless(alpha_uri)
        ).claimed_ids == ()
        replayed = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-1"},
        )
        assert [item["messageId"] for item in replayed["items"]] == ["m1"]
    finally:
        close_app(app, facade)


def test_owner_only_relocation_keeps_origin_claim(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    current = [("agent:new-owner:node-1:alpha", "session-1")]
    db = tmp_path / "reloc.sqlite3"
    host = new_actor_host()
    coordinator = DeliveryCustodyCoordinator(
        db,
        MemoryDeliveryTransport(),
        SilentEvents(),
        actor_host=host,
        session_current=lambda actor, ref: any(
            item_ref == ref and (
                item_actor == actor
                or (
                    item_actor.split(":")[1] != actor.split(":")[1]
                    and item_actor.split(":")[2:] == actor.split(":")[2:]
                )
            )
            for item_actor, item_ref in current
        ),
    )
    facade = DeliveryCustodyFacade(coordinator, db)
    try:
        old_uri = "agent:old-owner:node-1:alpha"
        new_uri = "agent:new-owner:node-1:alpha"
        other_uri = "agent:new-owner:node-1:beta"
        assert facade.receive(make_message("m1", new_uri)).accepted
        first = facade.batch_snapshot(
            new_uri,
            keys=(new_uri,),
            mark_fetched=False,
            origin_claim=OriginClaimant.session(old_uri, "session-1"),
        )
        assert [row.message.message_id for row in first.rows] == ["m1"]
        assert facade.claim_origin(
            new_uri, ("m1",), OriginClaimant.headless(new_uri)
        ).claimed_ids == ()
        taken_same = facade.batch_snapshot(
            new_uri,
            keys=(new_uri,),
            mark_fetched=False,
            origin_claim=OriginClaimant.session(new_uri, "session-1"),
        )
        assert [row.message.message_id for row in taken_same.rows] == ["m1"]

        current[0] = (other_uri, "session-1")
        assert facade.claim_origin(
            new_uri, ("m1",), OriginClaimant.headless(new_uri)
        ).claimed_ids == ("m1",)
    finally:
        facade.close()
        host.shutdown()


def test_session_projection_read_is_leaf_lock(tmp_path: Path) -> None:
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-1")
        assert facade.receive(make_message("m1", alpha_uri)).accepted
        assert facade.fetch_pending(
            alpha_uri,
            only=frozenset({"m1"}),
            origin_claim=OriginClaimant.session(alpha_uri, "session-1"),
        )
        proj = app._agent_session_domains.session._session_projection  # noqa: SLF001
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def hold_and_contend() -> None:
            try:
                with proj._lock:  # noqa: SLF001
                    entered.set()
                    assert release.wait(5.0)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=hold_and_contend, daemon=True)
        thread.start()
        assert entered.wait(2.0)

        def run_inbox() -> None:
            try:
                facade.claim_origin(
                    alpha_uri, ("m1",), OriginClaimant.headless(alpha_uri)
                )
            except BaseException as error:
                errors.append(error)

        contender = threading.Thread(target=run_inbox, daemon=True)
        contender.start()
        release.set()
        thread.join(timeout=5.0)
        contender.join(timeout=5.0)
        assert not thread.is_alive()
        assert not contender.is_alive()
        assert errors == []
    finally:
        close_app(app, facade)


def test_stale_eviction_rechecks_session_after_lock_reacquire() -> None:
    from hyprial.daemon.impl.inbox.actor.internal.origin_claims import OriginCustody
    from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant

    active = True
    stale_check_started = threading.Event()
    resume_stale_check = threading.Event()
    calls = 0
    liveness_results: list[bool] = []

    def session_current(_actor: str, _session_ref: str) -> bool:
        nonlocal calls
        calls += 1
        observed = active
        liveness_results.append(observed)
        if calls == 1:
            stale_check_started.set()
            assert resume_stale_check.wait(timeout=2)
        return observed

    custody = OriginCustody(session_current=session_current)
    claimant = OriginClaimant.session("agent:op:node-1:alpha", "session-1")
    assert custody.claim_origins(("m1",), claimant).claimed_ids == ("m1",)
    active = False

    result: list[tuple[str, ...]] = []
    worker = threading.Thread(
        target=lambda: result.append(
            custody.filter_unheld_origins(
                ("m1",), OriginClaimant.session("agent:op:node-1:alpha", "session-2")
            )
        )
    )
    worker.start()
    assert stale_check_started.wait(timeout=2)
    active = True
    resume_stale_check.set()
    worker.join(timeout=2)

    assert liveness_results == [False, True]
    assert result == [()]
    assert custody.snapshot()["m1"].owner_id == "session-1"
