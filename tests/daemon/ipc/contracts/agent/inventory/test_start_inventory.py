"""agent.start: the forward-launch enumeration, survival and real producer.

``USER_AGENT_IMMUTABLE`` is declared but outside the enumeration: the catalog
dispatcher raises it before the start walk (UA1b).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

from .enumeration import (
    _START_METHOD,
    _TerminalManager,
    _declared_codes,
    _desired_harness,
    _inventory_reachable,
    _wire_af_unix,
)


def test_inventory_covers_the_start_declaration_exactly() -> None:
    declared = _declared_codes(_START_METHOD)
    reachable = _inventory_reachable()
    missing = reachable - declared
    assert not missing, f"reachable codes not declared on {_START_METHOD}: {sorted(missing)}"
    undeclared_only = declared - reachable - {
        "CALLER_NOT_AUTHORIZED",
        "TARGET_NOT_AUTHORIZED",
        "DAEMON_ERROR",
        "AGENT_DEFAULT_ROUTINE_PENDING",
        "AGENT_DEFAULT_ROUTINE_FAILED",
        "USER_AGENT_IMMUTABLE",
    }
    assert not undeclared_only, (
        f"{_START_METHOD} declares codes outside the enumeration: "
        f"{sorted(undeclared_only)}"
    )
    for code in reachable & {
        "SUBMIT_OUTCOME_UNKNOWN",
        "PORT_CLOSING",
        "DAEMON_START_FAILED",
        "LIFECYCLE_MANAGER_UNAVAILABLE",
    }:
        error = {e.code: e for e in METHOD_REGISTRY.get(_START_METHOD).spec.errors}[code]
        assert error.data_schema is not None, code
        properties = error.data_schema["properties"]
        if code == "SUBMIT_OUTCOME_UNKNOWN":
            assert set(error.data_schema["required"]) == {"operationId"}
            assert "operationId" in properties
        assert "lastError" in properties, code



# --- implB3: the forward launch set survives legacy, registry and AF_UNIX ----


@pytest.fixture
def pinned_binary(monkeypatch):
    import shutil as shutil_module

    monkeypatch.setattr(shutil_module, "which", lambda name: f"/fake/bin/{name}")


def _assert_start_survives(app, error_code: str) -> None:
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    app._lifecycle_manager = _TerminalManager(error_code)
    with pytest.raises(Exception) as legacy:
        app.handle(
            "lifecycle.start",
            {
                "provider": "claude",
                "name": "alpha",
                "headless": True,
                "args": [],
                "cwd": str(app.state_dir),
            },
        )
    assert getattr(legacy.value, "code", None) == error_code, (
        f"legacy lifecycle.start erased {error_code}: {legacy.value}"
    )

    app._lifecycle_manager = _TerminalManager(error_code)
    with pytest.raises(Exception) as routed:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha"})
    assert routed.value.code == error_code, (
        f"registry erased {error_code}: {routed.value}"
    )
    assert routed.value.data is None, error_code

    app._lifecycle_manager = _TerminalManager(error_code)
    with pytest.raises(Exception) as ipc:
        app.handle("agent.start", {"name": "alpha"})
    assert ipc.value.code == routed.value.code, error_code
    assert ipc.value.data == routed.value.data, error_code


@pytest.mark.parametrize(
    "error_code",
    sorted(_inventory_reachable() - {"SUBMIT_OUTCOME_UNKNOWN"}),
)
def test_every_reachable_start_code_survives_legacy_registry_and_ipc(
    app, pinned_binary, error_code: str
) -> None:
    _desired_harness(app)
    _assert_start_survives(app, error_code)


def test_start_reservation_outcome_unknown_survives_with_its_operation_id(
    app, pinned_binary
) -> None:
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import (
        LifecycleReservationOutcomeUnknown,
    )
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    class _AdmissionUnknown:
        crashed = False

        def submit(self, operation):
            raise LifecycleReservationOutcomeUnknown(operation.operation_id)

        def wait(self, _operation_id: str, _timeout: float):
            raise AssertionError("never waited; the reservation never admitted")

        def drain(self, _timeout: float) -> bool:
            return True

    _desired_harness(app)
    app._lifecycle_manager = _AdmissionUnknown()
    with pytest.raises(Exception) as routed:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha"})
    assert routed.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert set(routed.value.data) == {"operationId"}
    assert routed.value.data["operationId"].startswith("lifecycle-start:")

    app._lifecycle_manager = _AdmissionUnknown()
    with pytest.raises(Exception) as ipc:
        app.handle("agent.start", {"name": "alpha"})
    assert ipc.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert set(ipc.value.data) == {"operationId"}


def test_real_missing_cwd_launch_keeps_its_code_across_every_boundary(
    app, pinned_binary
) -> None:
    """The real producer regression: a fully composed forward launch whose
    cwd is missing must reach the boundary as HARNESS_CWD_MISSING, not as a
    terminal-manager fixture code."""

    import threading

    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.daemon.impl.composition import (
        AgentIdentityProjectionView,
        CorrelatedDomainEvents,
        HarnessPortClient,
    )
    from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    class _Process:
        pid = None

        def __init__(self):
            self.running = True
            self.stopping = threading.Event()
            self.release = threading.Event()
            self.stops = 0

        def stop(self):
            self.stops += 1
            self.stopping.set()
            assert self.release.wait(5), "test did not release process stop"
            self.running = False

    class _Launcher:
        def __init__(self):
            self.processes = []

        def start(self, _spec):
            process = _Process()
            self.processes.append(process)
            return process

    class _Registration:
        def close(self):
            return None

    class _Transport:
        def declare_liveliness(self, _key):
            return _Registration()

        def subscribe(self, _key, _callback):
            return _Registration()

        def declare_queryable(self, _key, _handler):
            return _Registration()

    class _LocalDelivery:
        def register_actor(self, _actor):
            return _Registration()

    class _Inbox:
        def receive(self, _message):
            return None

        def has_received(self, _message_id):
            return False

        def has_fetched(self, _message_id):
            return False

    class _Presence:
        def actor_online(self, _actor: str) -> bool:
            return False

        def is_blocked(self, _actor: str) -> bool:
            return False

    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(
        launcher,
        event_sink=events,
        desired_state=app.desired_state,
        agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
    )
    harnesses = HarnessPortClient(actor, events)
    app._harnesses = harnesses
    app._inbox, app._presence = _Inbox(), _Presence()
    app._start_lifecycle_manager(
        transport=_Transport(),
        inbox=app._inbox,
        local_delivery=_LocalDelivery(),
        harnesses=harnesses,
    )
    missing = (Path(app.state_dir) / "missing-worktree").resolve()
    app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(missing))
    )
    try:
        with pytest.raises(Exception) as routed:
            METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha"})
        assert routed.value.code == "HARNESS_CWD_MISSING", routed.value
        response = _wire_af_unix(app, "agent.start", {"name": "alpha"})
        assert response["error"]["code"] == "HARNESS_CWD_MISSING"
        extra = set(response["error"].get("data") or {}) - {"lastError"}
        assert not extra, response["error"]
    finally:
        app._lifecycle_manager = None
