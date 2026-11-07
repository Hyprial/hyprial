from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.biz.impl.routine.execution.service import RoutineFacade, RoutineService
from hyprial.biz.impl.routine.storage.store import RoutineRow, RoutineSnapshot
from hyprial.biz.impl.routine.templates import render_template


class _RoutineList:
    def list(self):
        return {
            "routines": [
                {"name": "bad", "produces": "agent:owner:old-node:bad"},
                {"name": "good", "produces": "agent:owner:new-node:good"},
            ]
        }


def test_startup_isolates_bad_routine_and_reconciles_sibling() -> None:
    app = object.__new__(DaemonApplication)
    app._routine_service = _RoutineList()
    calls: list[str] = []
    logs: list[tuple[str, dict[str, object]]] = []

    specs = ()
    app.desired_state = SimpleNamespace(load=lambda: SimpleNamespace(harnesses=specs, adapters=()))

    def ensure(routine, *, recovering, specs, adapters):
        assert specs == ()
        assert adapters == ()
        assert recovering is True
        calls.append(routine["name"])
        if routine["name"] == "bad":
            raise DaemonRequestError("ROUTINE_COORDINATOR_IDENTITY_MISMATCH", "moved")
        return {"actor": routine["produces"], "changed": False}

    app._ensure_routine_coordinator_from_specs = ensure
    app._log = lambda _level, _component, event, **fields: logs.append((event, fields))

    app._reconcile_routine_coordinators()

    assert calls == ["bad", "good"]
    assert [event for event, _fields in logs] == [
        "routine.coordinator.reconcile_failed",
        "routine.coordinator.reconciled",
    ]


def test_startup_reads_desired_state_once_for_all_coordinators() -> None:
    app = object.__new__(DaemonApplication)
    app.owner = "owner"
    app.node_id = "new-node"
    routines = [
        {"name": name, "produces": f"agent:owner:new-node:{name}"}
        for name in ("first", "second")
    ]
    app._routine_service = SimpleNamespace(list=lambda: {"routines": routines})
    reads = []

    def load():
        reads.append(True)
        return SimpleNamespace(adapters=(), harnesses=tuple(
            SimpleNamespace(
                name=row["name"], harness="pi",
                nickname=f"routine:{row['name']}:coordinator",
            )
            for row in routines
        ))

    app.desired_state = SimpleNamespace(load=load)
    logs = []
    app._log = lambda _level, _component, event, **fields: logs.append((event, fields))

    app._reconcile_routine_coordinators()

    assert len(reads) == 1
    assert [event for event, _ in logs] == ["routine.coordinator.reconciled"] * 2
    assert all(fields["changed"] is False for _, fields in logs)


def test_corrupt_projection_is_explicit_instead_of_aborting_the_read() -> None:
    facade = object.__new__(RoutineFacade)
    projection = facade._projection_of(
        RoutineSnapshot(
            routine=RoutineRow(
                name="selfdrive",
                yaml_text="version: 1\nname: selfdrive\nproduces: not-an-agent\n",
                owner="agent:test:isolated:coordinator",
                enabled=True,
                next_due_ms=1,
                source_error_streak=0,
                outcomes="[]",
                created_at_ms=1,
            ),
            in_flight=(),
        )
    ).to_payload()

    assert projection["readable"] is False
    assert "canonical agent URI" in str(projection["schemaError"])
    assert projection["produces"] == "not-an-agent"


def test_invalid_stored_routine_is_visible_removable_and_does_not_hide_siblings(
    tmp_path,
) -> None:
    service = RoutineService(pac=object(), alarm=object(), state_dir=tmp_path)
    text = render_template(
        "selfdrive",
        owner="agent:test:isolated:coordinator",
        escalate_to="user:test",
    )
    healthy = text.replace("name: selfdrive", "name: healthy").replace(
        "agent:test:isolated:coordinator",
        "agent:test:isolated:healthy",
    )
    unexpected: str | None = None
    routines: list[dict[str, object]] = []
    corrupt: dict[str, object] = {}
    healthy_status: dict[str, object] = {}
    removed: dict[str, object] = {}
    remaining: list[dict[str, object]] = []
    try:
        service.add(yaml_text=text, owner="agent:test:isolated:coordinator")
        service.add(yaml_text=healthy, owner="agent:test:isolated:healthy")
        with sqlite3.connect(tmp_path / "routines.sqlite3") as db:
            db.execute(
                "UPDATE routines SET yaml_text=? WHERE name='selfdrive'",
                ("version: 1\nname: selfdrive\nproduces: not-an-agent\n",),
            )
            db.commit()

        routines = service.list()["routines"]
        corrupt = service.status(name="selfdrive")
        healthy_status = service.status(name="healthy")
        removed = service.remove(name="selfdrive")
        remaining = service.list()["routines"]
    except Exception as error:  # mutation must still reach teardown cleanly
        unexpected = f"{type(error).__name__}: {error}"
    finally:
        service.close()

    assert unexpected is None
    assert [item["name"] for item in routines] == ["selfdrive", "healthy"]
    assert corrupt["readable"] is False
    assert "canonical agent URI" in str(corrupt["schemaError"])
    assert corrupt["produces"] == "not-an-agent"
    assert "schemaError" not in healthy_status
    assert removed["removed"] is True
    assert [item["name"] for item in remaining] == ["healthy"]


def test_unreadable_routine_retires_only_marker_owned_coordinator() -> None:
    app = object.__new__(DaemonApplication)
    app.owner = "test"
    app.node_id = "isolated"
    app.desired_state = SimpleNamespace(
        load=lambda: SimpleNamespace(
            harnesses=(
                SimpleNamespace(
                    name="coordinator",
                    nickname="routine:selfdrive:coordinator",
                    harness="codex",
                ),
            )
        )
    )
    calls: list[tuple[str, dict[str, str]]] = []
    app.handle = lambda method, params: calls.append((method, params)) or {
        "removed": True
    }

    result = app._retire_routine_coordinator(
        {
            "name": "selfdrive",
            "produces": "not-an-agent",
            "schemaError": "produces must be canonical",
        }
    )

    assert calls == [("down", {"target": "coordinator", "provider": "codex"})]
    assert result == {
        "actor": "agent:test:isolated:coordinator",
        "retired": True,
        "changed": True,
    }


def test_routine_coordinator_uses_shared_matrix_resolver(monkeypatch) -> None:
    app = object.__new__(DaemonApplication)
    app.owner = "test"
    app.node_id = "isolated"
    app.desired_state = SimpleNamespace(
        load=lambda: SimpleNamespace(harnesses=(), adapters=())
    )
    choice = SimpleNamespace(
        harness="codex",
        provider=None,
        model="gpt-5.6-luna",
    )
    calls: list[tuple[str, bool]] = []

    def resolve(tier):
        calls.append(tier)
        return choice

    monkeypatch.setattr("hyprial.daemon.impl.dispatch.matrix.resolve", resolve)
    app.handle = lambda method, params: {
        "actor": "agent:test:isolated:coordinator",
        "operation": params["operationId"],
    }
    result = app._ensure_routine_coordinator(
        {
            "name": "selfdrive",
            "produces": "agent:test:isolated:coordinator",
        },
        recovering=False,
    )
    assert calls == ["fast"]
    assert result["actor"] == "agent:test:isolated:coordinator"


def test_missing_coordinator_is_not_reported_retired() -> None:
    app = object.__new__(DaemonApplication)
    app.desired_state = SimpleNamespace(
        load=lambda: SimpleNamespace(harnesses=())
    )
    result = app._retire_routine_coordinator(
        {
            "name": "selfdrive",
            "produces": "agent:test:isolated:coordinator",
        }
    )
    assert result == {
        "actor": "agent:test:isolated:coordinator",
        "retired": False,
        "changed": False,
    }
