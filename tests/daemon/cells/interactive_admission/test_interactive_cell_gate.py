"""Tests for PR-B interactive cell admission before inbox acquisition."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.cells.actor import CellActorSnapshot, CellActorState
from hyprial.daemon.impl.cells.runtime import create_cell_cache
from hyprial.daemon.impl.composition.cell_delivery import compose_headless_cell_delivery
from hyprial.daemon.impl.composition.cell_delivery.headless import HeadlessCellDelivery
from hyprial.daemon.impl.cells.actor.contracts import CellDispatchStamp
from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.cells.runtime.cell_runtime_support import ALICE
from tests.daemon.composition.cells.test_cell_runtime_wiring import (
    _Agents,
    _agent,
    _app_with_actors,
)
from tests.daemon.composition.cell_delivery.test_real_port import _external_agent
from tests.daemon.inbox.origin_custody.support import (
    RecordingHarnesses,
    build_actor_app,
    close_app,
    make_message,
    register_managed_worker,
    register_session,
)


def _snapshot(
    *,
    cell_id: str = "c.alpha",
    epoch: str = "ce-1",
    state: CellActorState = CellActorState.ACTIVE,
    entry_member: str | None = None,
    effective_revision: int = 3,
) -> CellActorSnapshot:
    return CellActorSnapshot(
        cell_id=cell_id,
        epoch=epoch,
        revision=effective_revision,
        effective_revision=effective_revision,
        entry_member=entry_member,
        state=state,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "missing-world",
        "snapshot-none",
        "empty",
        "degraded",
        "retiring",
        "retired",
        "active-no-entry",
        "entry-mismatch",
        "snapshot-raises",
    ],
)
def test_interactive_admission_unavailable_defers(
    tmp_path: Path, scenario: str
) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-a")
        assert facade.receive(make_message("m1", alpha_uri)).accepted

        if scenario == "missing-world":
            port = HeadlessCellDelivery(lambda _w: None, lambda _c: None)
        elif scenario == "snapshot-none":
            port = HeadlessCellDelivery(lambda _w: "c.alpha", lambda _c: None)
        elif scenario == "empty":
            port = HeadlessCellDelivery(
                lambda _w: "c.alpha",
                lambda _c: _snapshot(state=CellActorState.EMPTY, entry_member=None),
            )
        elif scenario == "degraded":
            port = HeadlessCellDelivery(
                lambda _w: "c.alpha",
                lambda _c: _snapshot(state=CellActorState.DEGRADED, entry_member=alpha_uri),
            )
        elif scenario == "retiring":
            port = HeadlessCellDelivery(
                lambda _w: "c.alpha",
                lambda _c: _snapshot(state=CellActorState.RETIRING, entry_member=alpha_uri),
            )
        elif scenario == "retired":
            port = HeadlessCellDelivery(
                lambda _w: "c.alpha",
                lambda _c: _snapshot(state=CellActorState.RETIRED, entry_member=alpha_uri),
            )
        elif scenario == "active-no-entry":
            port = HeadlessCellDelivery(
                lambda _w: "c.alpha",
                lambda _c: _snapshot(state=CellActorState.ACTIVE, entry_member=None),
            )
        elif scenario == "entry-mismatch":
            port = HeadlessCellDelivery(
                lambda _w: "c.alpha",
                lambda _c: _snapshot(
                    state=CellActorState.ACTIVE, entry_member="agent:op:node-1:other"
                ),
            )
        else:
            def _boom(_cell_id: str):
                raise RuntimeError("engine closed")

            port = HeadlessCellDelivery(lambda _w: "c.alpha", _boom)

        app._cell_delivery = port

        observed = app.handle(
            "message.pending.list",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert [row["messageId"] for row in observed["messages"]] == ["m1"]

        fetched_list = app.handle(
            "message.pending.list",
            {"actor": "alpha", "sessionRef": "session-a", "fetched": True},
        )
        assert fetched_list["ok"] is True
        assert fetched_list["messages"] == []
        assert facade.has_fetched("m1") is False

        batch = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert batch["ok"] is True
        assert batch["itemCount"] == 0
        assert batch["items"] == []
        assert batch["cursor"] == ""
        assert batch["delivery"]["text"] == ""
        assert facade.has_fetched("m1") is False

        with pytest.raises(DaemonRequestError) as refused:
            app.handle(
                "message.read_body",
                {"actor": "alpha", "sessionRef": "session-a", "messageId": "m1"},
            )
        assert refused.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
        assert facade.has_fetched("m1") is False
        assert facade.read_origin_claims() == {}

        principal_body = app.handle(
            "message.read_body",
            {"actor": "user:op", "onBehalfOf": "alpha", "messageId": "m1"},
        )
        assert principal_body["ok"] is True
        assert facade.has_fetched("m1") is True

        reread = app.handle(
            "message.read_body",
            {"actor": "alpha", "sessionRef": "session-a", "messageId": "m1"},
        )
        assert reread["ok"] is True
        assert reread["fetched"] is True

        harnesses = RecordingHarnesses(("worker",))
        worker_uri = register_managed_worker(
            app, "worker", "worker-ref-1", harnesses=harnesses
        )
        assert facade.receive(make_message("m-w", worker_uri)).accepted
        worker_batch = app.handle(
            "message.pending.batch",
            {"actor": "worker", "sessionRef": "worker-ref-1"},
        )
        assert [item["messageId"] for item in worker_batch["items"]] == ["m-w"]
    finally:
        close_app(app, facade)


def test_interactive_claim_keeps_snapshot_member_after_entry_change(
    tmp_path: Path,
) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha", "beta"))
    try:
        alpha_uri = register_session(app, "alpha", "session-a")
        beta_uri = register_session(app, "beta", "session-b")
        holder = {
            alpha_uri: _snapshot(
                cell_id="c.alpha", state=CellActorState.ACTIVE, entry_member=alpha_uri
            ),
            beta_uri: _snapshot(
                cell_id="c.alpha", state=CellActorState.ACTIVE, entry_member=alpha_uri
            ),
        }
        cell_map = {alpha_uri: "c.alpha", beta_uri: "c.alpha"}
        app._cell_delivery = HeadlessCellDelivery(
            lambda actor: cell_map.get(actor),
            lambda cell_id: holder[alpha_uri] if cell_id == "c.alpha" else None,
        )

        assert facade.receive(make_message("m1", alpha_uri)).accepted
        first = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert [item["messageId"] for item in first["items"]] == ["m1"]

        holder[alpha_uri] = _snapshot(
            cell_id="c.alpha",
            epoch="ce-2",
            state=CellActorState.ACTIVE,
            entry_member=beta_uri,
            effective_revision=4,
        )

        reread = app.handle(
            "message.read_body",
            {"actor": "alpha", "sessionRef": "session-a", "messageId": "m1"},
        )
        assert reread["ok"] is True
        assert app.handle(
            "message.ack",
            {"actor": "alpha", "sessionRef": "session-a", "messageId": "m1"},
        )["acknowledged"] is True

        assert facade.receive(make_message("m2", alpha_uri)).accepted
        deferred = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert deferred["itemCount"] == 0

        assert facade.receive(make_message("m3", beta_uri)).accepted
        admitted_b = app.handle(
            "message.pending.batch",
            {"actor": "beta", "sessionRef": "session-b"},
        )
        assert [item["messageId"] for item in admitted_b["items"]] == ["m3"]
    finally:
        close_app(app, facade)


def test_blocked_cell_snapshot_does_not_block_inbox_actor(tmp_path: Path) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-a")
        assert facade.receive(make_message("m1", alpha_uri)).accepted

        entered = threading.Event()
        release = threading.Event()

        def slow_snapshot(_cell_id: str) -> CellActorSnapshot:
            entered.set()
            assert release.wait(5.0)
            return _snapshot(
                cell_id="c.alpha", state=CellActorState.ACTIVE, entry_member=alpha_uri
            )

        app._cell_delivery = HeadlessCellDelivery(
            lambda _actor: "c.alpha",
            slow_snapshot,
        )
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def run_ipc() -> None:
            try:
                results.append(
                    app.handle(
                        "message.pending.batch",
                        {"actor": "alpha", "sessionRef": "session-a"},
                    )
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run_ipc, daemon=True)
        thread.start()
        try:
            assert entered.wait(2.0)
            assert facade.receive(make_message("m2", alpha_uri)).accepted
            assert facade.ack(alpha_uri, "m2").acknowledged is True
        finally:
            release.set()
            thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert errors == []
        assert len(results) == 1
        assert [item["messageId"] for item in results[0]["items"]] == ["m1"]
    finally:
        close_app(app, facade)


def test_interactive_or_headless_steer_does_not_reselect_entry() -> None:
    reads: list[str] = []

    def snapshot_of(_cell_id: str) -> CellActorSnapshot:
        reads.append(_cell_id)
        return _snapshot(
            cell_id="c.alpha",
            state=CellActorState.ACTIVE,
            entry_member="agent:op:node-1:other",
            effective_revision=99,
        )

    port = HeadlessCellDelivery(lambda _w: "c.alpha", snapshot_of)
    stamp = CellDispatchStamp(
        "c.alpha", "agent:op:node-1:alpha", "ce-1", 3, ("m1",)
    )
    envelope = port.steer_envelope(stamp, "m1")
    assert envelope is not None
    assert envelope.stamp.member == "agent:op:node-1:alpha"
    assert envelope.stamp.revision == 3
    assert reads == []


def test_real_port_interactive_admission_matches_canonical_uri_and_entry(
    tmp_path: Path,
) -> None:
    attached = _external_agent("ext-attached", attached=True)
    detached = _external_agent("ext-detached", attached=False)
    app = _app_with_actors(tmp_path, _Agents(attached, detached))
    try:
        port = compose_headless_cell_delivery(app)
        deadline = time.monotonic() + 10.0
        admitted = None
        while admitted is None and time.monotonic() < deadline:
            admitted = port.interactive_admission(attached.uri)
            if admitted is None:
                time.sleep(0.05)
        assert admitted is not None
        assert admitted.cell_id == attached.cell_id
        assert admitted.member == attached.uri

        deadline = time.monotonic() + 10.0
        settled = False
        while time.monotonic() < deadline:
            runtime = app._cell_runtime
            assert runtime is not None
            cell = runtime.lookup(detached.cell_id)
            if cell is not None and not cell.members:
                settled = True
                break
            time.sleep(0.05)
        assert settled
        assert port.interactive_admission(detached.uri) is None
        assert port.interactive_admission("agent:alice:mac:stranger") is None
    finally:
        close_cell_runtime(app)


@pytest.mark.parametrize("port_value", [None, object()])
def test_interactive_admission_fails_closed_when_cell_delivery_absent(
    tmp_path: Path, port_value: object
) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha",))
    try:
        alpha_uri = register_session(app, "alpha", "session-a")
        assert facade.receive(make_message("m1", alpha_uri)).accepted
        app._cell_delivery = port_value

        observed = app.handle(
            "message.pending.list",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert [row["messageId"] for row in observed["messages"]] == ["m1"]

        fetched_list = app.handle(
            "message.pending.list",
            {"actor": "alpha", "sessionRef": "session-a", "fetched": True},
        )
        assert fetched_list["ok"] is True
        assert fetched_list["messages"] == []
        assert facade.has_fetched("m1") is False
        assert facade.read_origin_claims() == {}

        batch = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert batch["ok"] is True
        assert batch["itemCount"] == 0
        assert batch["items"] == []
        assert batch["cursor"] == ""
        assert facade.has_fetched("m1") is False
        assert facade.read_origin_claims() == {}

        with pytest.raises(DaemonRequestError) as refused:
            app.handle(
                "message.read_body",
                {"actor": "alpha", "sessionRef": "session-a", "messageId": "m1"},
            )
        assert refused.value.code == ipc_errors.MESSAGE_BODY_UNAVAILABLE
        assert facade.has_fetched("m1") is False
        assert facade.read_origin_claims() == {}

        harnesses = RecordingHarnesses(("worker",))
        worker_uri = register_managed_worker(
            app, "worker", "worker-ref-1", harnesses=harnesses
        )
        assert facade.receive(make_message("m-w", worker_uri)).accepted
        worker_batch = app.handle(
            "message.pending.batch",
            {"actor": "worker", "sessionRef": "worker-ref-1"},
        )
        assert [item["messageId"] for item in worker_batch["items"]] == ["m-w"]

        principal_body = app.handle(
            "message.read_body",
            {"actor": "user:op", "onBehalfOf": "alpha", "messageId": "m1"},
        )
        assert principal_body["ok"] is True
        assert facade.has_fetched("m1") is True
    finally:
        close_app(app, facade)


def test_interactive_cursor_replay_survives_entry_move_for_owned_rows(
    tmp_path: Path,
) -> None:
    app, _coordinator, facade = build_actor_app(tmp_path, actors=("alpha", "beta"))
    try:
        alpha_uri = register_session(app, "alpha", "session-a")
        beta_uri = register_session(app, "beta", "session-b")
        holder = {
            "snap": _snapshot(
                cell_id="c.alpha", state=CellActorState.ACTIVE, entry_member=alpha_uri
            )
        }
        cell_map = {alpha_uri: "c.alpha", beta_uri: "c.alpha"}
        app._cell_delivery = HeadlessCellDelivery(
            lambda actor: cell_map.get(actor),
            lambda cell_id: holder["snap"] if cell_id == "c.alpha" else None,
        )

        assert facade.receive(make_message("m1", alpha_uri, created_at_ms=1)).accepted
        assert facade.receive(make_message("m2", alpha_uri, created_at_ms=2)).accepted

        first = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a"},
        )
        assert [item["messageId"] for item in first["items"]] == ["m1", "m2"]
        cursor = first["cursor"]
        assert cursor

        assert app.handle(
            "message.ack",
            {"actor": "alpha", "sessionRef": "session-a", "messageId": "m1"},
        )["acknowledged"] is True

        holder["snap"] = _snapshot(
            cell_id="c.alpha",
            epoch="ce-2",
            state=CellActorState.ACTIVE,
            entry_member=beta_uri,
            effective_revision=4,
        )
        assert facade.receive(make_message("m3", alpha_uri, created_at_ms=3)).accepted

        replay = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a", "cursor": cursor},
        )
        assert replay["itemCount"] == 1
        assert [item["messageId"] for item in replay["items"]] == ["m2"]

        successor = app.handle(
            "message.pending.batch",
            {"actor": "alpha", "sessionRef": "session-a", "afterCursor": cursor},
        )
        assert successor["itemCount"] == 0
        assert successor["items"] == []
        assert facade.has_fetched("m3") is False
        assert "m3" not in facade.read_origin_claims()
    finally:
        close_app(app, facade)


def test_real_port_defers_on_cache_writer_mismatch_and_recovers_after_cache_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OC1a writer mismatch keeps interactive admission fail-closed until cache archive."""

    import hyprial.daemon.impl.composition.cells.wiring as wiring

    worker = _agent("worker")
    state = tmp_path / "state"
    create_cell_cache(
        state / "cells.sqlite3",
        authority=ALICE,
        issuer="",
        writer="user:alice-old",
    )
    app = _app_with_actors(tmp_path, _Agents(worker))
    try:
        port = compose_headless_cell_delivery(app)
        assert getattr(app, "_cell_runtime", None) is None
        assert getattr(app, "_cell_actor_registry", None) is None
        assert "cell.runtime_unavailable" in app.logged
        assert port.interactive_admission(worker.uri) is None

        backup = tmp_path / "cells-backup"
        backup.mkdir()
        for name in ("cells.sqlite3", "cells.sqlite3-wal", "cells.sqlite3-shm"):
            source = state / name
            if source.exists():
                source.rename(backup / name)

        monkeypatch.setattr(wiring, "COMPOSE_RETRY_S", 0.0)
        assert cell_runtime(app) is not None
        deadline = time.monotonic() + 10.0
        admitted = None
        while admitted is None and time.monotonic() < deadline:
            admitted = port.interactive_admission(worker.uri)
            if admitted is None:
                time.sleep(0.05)
        assert admitted is not None
        assert admitted.cell_id == worker.cell_id
        assert admitted.member == worker.uri
    finally:
        close_cell_runtime(app)
