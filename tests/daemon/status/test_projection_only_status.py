"""Status IPC reads are inert projections, even when network owners stall."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging.status.refresh import (
    CoalescedRefreshAuthority,
)
from tests.shell.diagnostics.test_ps_status_snapshot import (
    _TEST_APPLICATIONS,
    application,
)
from tests.support.network_owner_context import FunctionPoint, OwnerContextTracer


@pytest.fixture(autouse=True)
def _close_applications():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        app = _TEST_APPLICATIONS.pop()
        app._status_refresh.close()
        app.agents.close()


class _HostPresence:
    def __init__(self, *nodes: str) -> None:
        self.nodes = nodes

    def raw_online_actors(self) -> tuple[str, ...]:
        return self.nodes

    def liveness_keeps(self, _actor: str) -> bool:
        return True


class _ChangingTransport:
    def __init__(self) -> None:
        self.status: dict[str, object] = {"state": "closed", "generation": 1}

    def session_health(self) -> dict[str, object]:
        return dict(self.status)


class _BlackHoledTransport:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def session_health(self) -> dict[str, object]:
        self.entered.set()
        assert self.release.wait(2.0)
        return {"state": "open"}


def test_stalled_presence_evidence_ages_from_online_to_stale_then_unknown(
    tmp_path,
) -> None:
    now_ms = [10_000]
    app = application(tmp_path)
    app._status_refresh.close()
    app._status_refresh = CoalescedRefreshAuthority(
        stale_after_ms=1_000,
        expire_after_ms=2_000,
        clock_ms=lambda: now_ms[0],
    )
    app._presence = _HostPresence("node-1", "remote-node")  # type: ignore[assignment]
    arrivals = [
        SimpleNamespace(
            identity=node,
            observed_at_ms=now_ms[0],
            kind="actor",
            change="put",
            admission="accepted",
        )
        for node in ("node-1", "remote-node")
    ]
    now_ms[0] = 10_500  # callback processing must not renew the sample time
    for event in arrivals:
        app._trace_presence_observation(event)

    fresh = app.handle("hosts", {})["hosts"]
    assert {row["status"] for row in fresh} == {"online"}
    assert {row["observedAtMs"] for row in fresh} == {10_000}
    assert {row["ageMs"] for row in fresh} == {500}
    assert {row["stale"] for row in fresh} == {False}

    now_ms[0] = 11_001
    stale = app.handle("hosts", {})["hosts"]
    assert {row["status"] for row in stale} == {"online"}
    assert {row["observedAtMs"] for row in stale} == {10_000}
    assert {row["ageMs"] for row in stale} == {1_001}
    assert {row["stale"] for row in stale} == {True}

    now_ms[0] = 12_001
    expired_but_present = app.handle("hosts", {})["hosts"]
    assert {row["status"] for row in expired_but_present} == {"online"}
    # The old arrival time remains truthful even though the materialized
    # liveliness set is current evidence that the hosts are still present.
    assert {row["observedAtMs"] for row in expired_but_present} == {10_000}
    assert {row["ageMs"] for row in expired_but_present} == {2_001}
    assert {row["stale"] for row in expired_but_present} == {True}

    app._presence.nodes = ("node-1",)
    app._trace_presence_observation(
        SimpleNamespace(
            identity="remote-node",
            observed_at_ms=12_002,
            kind="actor",
            change="delete",
            admission="accepted",
        )
    )
    removed = {row["nodeId"]: row for row in app.handle("hosts", {})["hosts"]}
    assert removed["remote-node"]["status"] == "unknown"
    assert removed["remote-node"]["observedAtMs"] == 12_002
    assert removed["remote-node"]["ageMs"] == 0
    assert removed["remote-node"]["stale"] is False


def test_status_reads_do_not_cleanup_or_mutate_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = application(tmp_path)

    def unexpected(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("an observational status read attempted cleanup")

    monkeypatch.setattr(
        app, "_clean_dead_interactive_sessions", unexpected, raising=False
    )
    monkeypatch.setattr(app, "_expire_stale_channel_routes", unexpected)

    app.handle("ps", {})
    app.handle("targets", {})
    app.handle("hosts", {})


def test_refresh_settlement_publishes_new_endpoint_evidence(tmp_path) -> None:
    app = application(tmp_path)
    transport = _ChangingTransport()
    app._transport = transport  # type: ignore[assignment]
    transport.status = {"state": "open", "generation": 2}

    admission = app._request_status_refresh()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        refresh_status = app._status_refresh_fields()
        if refresh_status["refreshState"] == "settled":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("status refresh did not settle")

    ps = app.handle("ps", {})
    assert ps["zenoh"]["session"]["state"] == "open"
    assert ps["zenoh"]["session"]["generation"] == 2
    assert ps["zenoh"]["session"]["refreshOperationId"] == admission.operation_id
    assert ps["zenoh"]["session"]["refreshState"] == "settled"
    assert ps["tailnet"]["supported"] is False
    assert ps["tailnet"]["reason"] == "TAILNET_STATUS_PRODUCER_UNAVAILABLE"
    unsupported = app.handle("ps", {"refreshTailnetStatus": True})["refresh"]
    assert unsupported == {
        "accepted": False,
        "queued": False,
        "operationId": None,
        "coalesced": False,
        "reason": "TAILNET_STATUS_PRODUCER_UNAVAILABLE",
    }


def test_later_refresh_does_not_hide_earlier_operation_settlement() -> None:
    now_ms = [10_000]
    generations = iter((1, 2, 3))
    authority = CoalescedRefreshAuthority(
        workers=1,
        settlement_limit=2,
        settlement_retention_ms=1_000,
        clock_ms=lambda: now_ms[0],
    )

    def settle_refresh() -> str:
        admission = authority.request(
            "endpoint", lambda: {"generation": next(generations)}
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            status = authority.status("endpoint", operation_id=admission.operation_id)
            if status["refreshState"] == "settled":
                return admission.operation_id
            time.sleep(0.01)
        raise AssertionError("status refresh did not settle")

    try:
        first_operation_id = settle_refresh()
        now_ms[0] += 1
        second_operation_id = settle_refresh()

        assert second_operation_id != first_operation_id
        first = authority.status("endpoint", operation_id=first_operation_id)
        assert first["refreshOperationId"] == first_operation_id
        assert first["refreshState"] == "settled"

        now_ms[0] += 1
        settle_refresh()
        count_expired = authority.status(
            "endpoint", operation_id=first_operation_id
        )
        assert count_expired["refreshOperationId"] == first_operation_id
        assert count_expired["refreshState"] == "unknown-expired"

        now_ms[0] += 1_001
        age_expired = authority.status(
            "endpoint", operation_id=second_operation_id
        )
        assert age_expired["refreshOperationId"] == second_operation_id
        assert age_expired["refreshState"] == "unknown-expired"
    finally:
        assert authority.close()


def test_black_holed_transport_cannot_delay_or_enter_status_ipc_owner(
    tmp_path,
) -> None:
    app = application(tmp_path)
    transport = _BlackHoledTransport()
    app._transport = transport  # type: ignore[assignment]
    tracer = OwnerContextTracer(
        owners=(FunctionPoint.from_callable("status_ipc", DaemonApplication.handle),),
        boundaries=(
            FunctionPoint.from_callable(
                "transport_health", _BlackHoledTransport.session_health
            ),
        ),
    )

    started = time.monotonic()
    try:
        with tracer.installed():
            ping = app.handle("ping", {})
            hosts = app.handle("hosts", {"refresh": True})
            ps = app.handle("ps", {"refresh": True})
            targets = app.handle("targets", {})
    finally:
        transport.release.set()

    assert time.monotonic() - started < 0.5
    assert ping["running"] is True
    host_refresh = hosts["refresh"]
    ps_refresh = ps["refresh"]
    assert isinstance(host_refresh["operationId"], str)
    assert host_refresh["accepted"] is True
    assert host_refresh["queued"] is True
    assert isinstance(ps_refresh["operationId"], str)
    assert ps_refresh["operationId"] == host_refresh["operationId"]
    assert host_refresh["coalesced"] is False
    assert ps_refresh["coalesced"] is True
    assert ps_refresh["accepted"] is True
    assert ps_refresh["queued"] is True
    assert not {"accepted", "queued", "operationId", "coalesced"}.intersection(hosts)
    assert not {"accepted", "queued", "operationId", "coalesced"}.intersection(ps)
    assert "targets" in targets
