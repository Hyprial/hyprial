"""Durable typed routine add/remove saga and effect replay."""

from __future__ import annotations

import sqlite3
import threading
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import PortAdmission
from hyprial.biz.impl.routine.execution.coordinator import (
    AgentCreationCompensation, RoutineCoordinator, RoutineCoordinatorError,
    RoutineCoordinatorTimeout, _Generation, add_command, remove_command,
)
from hyprial.biz.impl.routine.execution.service import RoutineServiceError
from hyprial.biz.impl.routine.execution.service import RoutineService
from hyprial.biz.impl.routine.templates import render_template
from hyprial.biz.impl.routine.contracts.schema import load_routine_text


def _yaml(name: str) -> str:
    return render_template(
        "selfdrive", owner="agent:test:node:coordinator", escalate_to="user:test"
    ).replace("name: selfdrive", f"name: {name}")


class Routines:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.add_calls = 0
        self.removal_reservations: dict[str, str] = {}

    def add(self, *, yaml_text, owner, enabled, registration_id=None):
        self.add_calls += 1
        spec = load_routine_text(yaml_text)
        name = spec.name
        if name in self.rows:
            raise RoutineServiceError("ROUTINE_EXISTS", "already exists")
        self.rows[name] = {
            "name": name, "owner": owner, "enabled": enabled,
            "registrationId": registration_id,
            "produces": spec.produces,
            "inFlight": [],
        }
        return {"name": name, "enabled": enabled}

    def status(self, *, name):
        if name not in self.rows:
            raise RoutineServiceError("ROUTINE_NOT_FOUND", name)
        return dict(self.rows[name])

    def list(self):
        return {"routines": list(self.rows.values())}

    def pause(self, *, name):
        self.rows[name]["enabled"] = False
        return {"enabled": False}

    def resume(self, *, name, align_schedule=False):
        self.rows[name]["enabled"] = True
        return {"enabled": True}

    def remove(self, *, name, enforce_last=False, reservation_id=None):
        del enforce_last
        if name not in self.rows:
            raise RoutineServiceError("ROUTINE_NOT_FOUND", name)
        if (
            reservation_id is not None
            and self.removal_reservations.get(name) != reservation_id
        ):
            raise RoutineServiceError("ROUTINE_COORDINATOR_CONFLICT", name)
        self.rows.pop(name)
        self.removal_reservations.pop(name, None)
        return {"removed": True, "name": name}

    def reserve_remove(self, *, name, reservation_id, enforce_last=False):
        del enforce_last
        incumbent = self.removal_reservations.get(name)
        if incumbent not in (None, reservation_id):
            raise RoutineServiceError("ROUTINE_BUSY", name)
        self.removal_reservations[name] = reservation_id
        return self.status(name=name)

    def cancel_remove(self, *, name, reservation_id):
        if self.removal_reservations.get(name) == reservation_id:
            self.removal_reservations.pop(name)
        return {"cancelled": True}


def test_add_remove_saga_has_durable_admission_and_off_mailbox_effects(
    tmp_path: Path,
) -> None:
    routines = Routines()
    threads: list[str] = []
    closed: list[tuple[str, str]] = []

    def ensure(routine):
        threads.append(threading.current_thread().name)
        return {"actor": routine["produces"], "changed": True}

    coordinator = RoutineCoordinator(
        state_dir=tmp_path, routines=routines,
        ensure_coordinator=ensure,
        retire_coordinator=lambda routine: {"actor": routine["produces"], "retired": True},
        close_graph=lambda graph_id, actor: closed.append((graph_id, actor)),
    )
    try:
        add = add_command(
            operation_id="add-1", name="first", yaml_text=_yaml("first"),
            owner="agent:test:node:duty", produces="agent:test:node:coordinator",
        )
        assert coordinator.begin(add) is PortAdmission.ACCEPTED
        with sqlite3.connect(coordinator.database) as db:
            assert db.execute(
                "SELECT request_digest FROM routine_coordinator_operations WHERE operation_id='add-1'"
            ).fetchone() is not None
        assert coordinator.wait("add-1", 3.0)["enabled"] is True
        assert coordinator.stats()["effectCapacity"] == 32
        assert coordinator.stats()["operationsPending"] == 0
        assert len(threads) == 1 and threads[0] != threading.current_thread().name
        assert routines.rows["first"]["registrationId"]

        routines.rows["first"]["inFlight"] = [{"runId": "graph-1"}]
        remove = remove_command(operation_id="remove-1", name="first")
        assert coordinator.begin(remove) is PortAdmission.ACCEPTED
        assert coordinator.wait("remove-1", 3.0)["removed"] is True
        assert closed == [("graph-1", "agent:test:node:duty")]
        assert routines.rows == {}
    finally:
        assert coordinator.close(3.0)


def test_add_replays_same_registration_after_effect_late_close(tmp_path: Path) -> None:
    routines = Routines()
    entered = threading.Event()
    release = threading.Event()

    def slow_ensure(routine):
        entered.set()
        assert release.wait(3.0)
        return {"actor": routine["produces"], "changed": True}

    first = RoutineCoordinator(
        state_dir=tmp_path, routines=routines,
        ensure_coordinator=slow_ensure,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    add = add_command(
        operation_id="add-replay", name="first", yaml_text=_yaml("first"),
        owner="agent:test:node:duty", produces="agent:test:node:coordinator",
    )
    assert first.begin(add) is PortAdmission.ACCEPTED
    assert entered.wait(1.0)
    assert not first.close(0.02)
    release.set()
    assert first.close(3.0)

    second = RoutineCoordinator(
        state_dir=tmp_path, routines=routines,
        ensure_coordinator=lambda routine: {"actor": routine["produces"], "changed": False},
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    try:
        assert second.wait("add-replay", 3.0)["enabled"] is True
        assert routines.add_calls == 1
        conflict = add_command(
            operation_id="add-replay", name="different", yaml_text=_yaml("different"),
            owner="agent:test:node:duty", produces="agent:test:node:coordinator",
        )
        with pytest.raises(RoutineCoordinatorError) as caught:
            second.begin(conflict)
        assert caught.value.code == "ROUTINE_OPERATION_CONFLICT"
    finally:
        assert second.close(3.0)


def test_coordinator_uses_real_routine_writer_registration_receipt(
    tmp_path: Path,
) -> None:
    routines = RoutineService(
        pac=SimpleNamespace(rearm_boundary=lambda **_kwargs: 0), alarm=object(), state_dir=tmp_path,
        source_query=lambda _filter: [],
    )
    yaml_text = render_template(
        "selfdrive", owner="agent:test:isolated:coordinator",
        escalate_to="user:test",
    )
    coordinator = RoutineCoordinator(
        state_dir=tmp_path, routines=routines,
        ensure_coordinator=lambda routine: {"actor": routine["produces"]},
        retire_coordinator=lambda routine: {"actor": routine["produces"]},
        close_graph=lambda _graph, _actor: None,
    )
    try:
        command = add_command(
            operation_id="real-add", name="selfdrive", yaml_text=yaml_text,
            owner="agent:test:isolated:coordinator",
            produces="agent:test:isolated:coordinator",
        )
        assert coordinator.begin(command) is PortAdmission.ACCEPTED
        assert coordinator.wait("real-add", 3.0)["enabled"] is True
        assert routines.status(name="selfdrive")["registrationId"]
        remove = remove_command(operation_id="real-remove", name="selfdrive")
        assert coordinator.begin(remove) is PortAdmission.ACCEPTED
        assert coordinator.wait("real-remove", 3.0)["removed"] is True
    finally:
        assert coordinator.close(3.0)
        routines.close()


def test_failed_ensure_compensates_using_marker_even_without_ensure_receipt(
    tmp_path: Path,
) -> None:
    routines = Routines()
    retired: list[str] = []

    def failed_ensure(_routine):
        raise RoutineCoordinatorError(
            "INJECTED_LIFECYCLE_FAILURE", "start result was lost", "add-fail"
        )

    coordinator = RoutineCoordinator(
        state_dir=tmp_path, routines=routines,
        ensure_coordinator=failed_ensure,
        retire_coordinator=lambda routine: retired.append(str(routine["name"])) or None,
        close_graph=lambda _graph, _actor: None,
    )
    try:
        add = add_command(
            operation_id="add-fail", name="first", yaml_text=_yaml("first"),
            owner="agent:test:node:duty", produces="agent:test:node:coordinator",
        )
        assert coordinator.begin(add) is PortAdmission.ACCEPTED
        with pytest.raises(RoutineCoordinatorError) as caught:
            coordinator.wait("add-fail", 3.0)
        assert caught.value.code == "INJECTED_LIFECYCLE_FAILURE"
        assert retired == ["first"]
        assert routines.rows == {}
    finally:
        assert coordinator.close(3.0)


def test_projection_returns_deeply_defensive_result(tmp_path: Path) -> None:
    routines = Routines()
    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda routine: {"actor": routine["produces"]},
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    try:
        command = add_command(
            operation_id="immutable-projection",
            name="first",
            yaml_text=_yaml("first"),
            owner="agent:test:node:duty",
            produces="agent:test:node:coordinator",
        )
        assert coordinator.begin(command) is PortAdmission.ACCEPTED
        coordinator.wait("immutable-projection", 3.0)
        first = coordinator.projection("immutable-projection")
        assert first is not None and first.result is not None
        first.result["nested"] = {"items": ["mutated"]}
        second = coordinator.projection("immutable-projection")
        assert second is not None and second.result is not None
        assert "nested" not in second.result
    finally:
        assert coordinator.close(3.0)


def test_real_coordinator_parallel_last_binding_keeps_survivor_operational(
    tmp_path: Path,
) -> None:
    actor = "agent:test:node:resident"
    routines = RoutineService(
        pac=SimpleNamespace(rearm_boundary=lambda **_kwargs: 0), alarm=object(), state_dir=tmp_path,
        source_query=lambda _filter: [],
    )
    owned = {"first", "second"}
    for name in sorted(owned):
        routines.add(
            yaml_text=(
                "version: 2\n"
                "mode: scheduled\n"
                f"name: {name}\n"
                "schedule:\n  interval: 15m\n"
                f"actor: {actor}\n"
                "role: dispatch\n"
                "task: work\n"
                "on_task_timeout:\n"
                "  action: escalate\n"
                "  escalate_to: user:test\n"
            ),
            owner="user:test",
            enabled=True,
        )

    def retire(routine):  # type: ignore[no-untyped-def]
        owned.discard(str(routine["name"]))
        return {"actor": routine["actor"], "retired": True}

    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda routine: {"actor": routine["actor"]},
        retire_coordinator=retire,
        close_graph=lambda _graph, _actor: None,
    )
    outcomes: list[str] = []

    def remove(name: str) -> None:
        operation_id = f"parallel-remove:{name}"
        try:
            assert coordinator.begin(
                remove_command(
                    operation_id=operation_id, name=name, enforce_last=True
                )
            ) is PortAdmission.ACCEPTED
            coordinator.wait(operation_id, 3.0)
            outcomes.append("removed")
        except RoutineCoordinatorError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=remove, args=(name,)) for name in sorted(owned)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3.0)
            assert not thread.is_alive()
        assert sorted(outcomes) == ["ROUTINE_LAST_BINDING", "removed"]
        [survivor] = routines.list()["routines"]
        assert survivor["enabled"] is True
        assert survivor["name"] in owned
    finally:
        assert coordinator.close(3.0)
        routines.close()


@pytest.mark.parametrize("recreate", [False, True])
def test_timed_out_add_late_failure_compensates_only_original_agent(
    tmp_path: Path, recreate: bool,
) -> None:
    routines = Routines()
    entered = threading.Event()
    release = threading.Event()
    current_token: list[str | None] = ["entity-original"]

    def fail_late(_routine):
        entered.set()
        assert release.wait(3.0)
        raise RoutineCoordinatorError(
            "INJECTED_LATE_FAILURE", "late ensure failure", "agent-add"
        )

    def destroy_if_current(_settlement: str, _actor: str, expected: str) -> bool:
        if current_token[0] != expected:
            return False
        current_token[0] = None
        return True

    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=fail_late,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
        compensate_agent=destroy_if_current,
    )
    command = add_command(
        operation_id="agent-add",
        name="first",
        yaml_text=_yaml("first"),
        owner="user:test",
        produces="agent:test:node:coordinator",
        agent_compensation=AgentCreationCompensation(
            actor="resident",
            expected_entity_token="entity-original",
            settlement_id="settle-agent-add",
        ),
    )
    try:
        assert coordinator.begin(command) is PortAdmission.ACCEPTED
        assert entered.wait(1.0)
        with pytest.raises(RoutineCoordinatorTimeout):
            coordinator.wait("agent-add", 0.01)
        if recreate:
            current_token[0] = "entity-successor"
        release.set()
        with pytest.raises(RoutineCoordinatorError) as caught:
            coordinator.wait("agent-add", 3.0)
        assert caught.value.code == "INJECTED_LATE_FAILURE"
        assert current_token[0] == ("entity-successor" if recreate else None)
    finally:
        release.set()
        assert coordinator.close(3.0)


def test_remove_holds_reservation_through_typed_pac_close_fault(
    tmp_path: Path,
) -> None:
    routines = Routines()
    routines.rows["first"] = {
        "name": "first",
        "owner": "user:test",
        "enabled": True,
        "registrationId": "registration",
        "produces": "agent:test:node:coordinator",
        "inFlight": [{"runId": "graph-1"}],
    }
    entered = threading.Event()
    release = threading.Event()
    retired: list[str] = []
    restored: list[str] = []

    def close_fault(graph_id: str, actor: str) -> None:
        assert (graph_id, actor) == ("graph-1", "user:test")
        entered.set()
        assert release.wait(3.0)
        raise RoutineCoordinatorError(
            "PAC_CLOSE_FAULT", "typed close failed", "remove-fault"
        )

    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda routine: restored.append(
            str(routine["name"])
        ),
        retire_coordinator=lambda routine: retired.append(str(routine["name"])),
        close_graph=close_fault,
    )
    try:
        assert coordinator.begin(
            remove_command(operation_id="remove-fault", name="first")
        ) is PortAdmission.ACCEPTED
        assert entered.wait(1.0)
        assert routines.removal_reservations == {"first": "remove-fault"}
        release.set()
        with pytest.raises(RoutineCoordinatorError) as caught:
            coordinator.wait("remove-fault", 3.0)
        assert caught.value.code == "PAC_CLOSE_FAULT"
        assert routines.removal_reservations == {}
        assert "first" in routines.rows
        assert routines.rows["first"]["enabled"] is True
        assert retired == []
        assert restored == ["first"]
    finally:
        release.set()
        assert coordinator.close(3.0)


def test_legacy_remove_journal_is_migrated_and_same_request_replays(
    tmp_path: Path,
) -> None:
    routines = Routines()
    routines.rows["first"] = {
        "name": "first",
        "owner": "user:test",
        "enabled": False,
        "registrationId": "registration",
        "produces": "agent:test:node:coordinator",
        "inFlight": [],
    }
    initializer = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    database = initializer.database
    assert initializer.close(3.0)
    legacy_payload = {"enforceLast": True}
    legacy_digest = hashlib.sha256(
        json.dumps(
            {"kind": "remove", "name": "first", "payload": legacy_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO routine_coordinator_operations VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-remove", "remove", "first", legacy_digest,
                json.dumps(legacy_payload), "running", "close_graphs",
                json.dumps({"routine": routines.rows["first"]}),
                None, None, None, 4,
            ),
        )

    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    try:
        assert coordinator.begin(
            remove_command(
                operation_id="legacy-remove", name="first", enforce_last=True
            )
        ) is PortAdmission.ACCEPTED
        assert coordinator.wait("legacy-remove", 3.0)["removed"] is True
        with sqlite3.connect(database) as db:
            payload, digest = db.execute(
                "SELECT payload_json,request_digest FROM "
                "routine_coordinator_operations WHERE operation_id=?",
                ("legacy-remove",),
            ).fetchone()
        assert json.loads(payload)["reservationId"] == "legacy-remove"
        assert digest != legacy_digest
    finally:
        assert coordinator.close(3.0)


def test_failed_remove_replays_owner_rejection_data_after_restart(tmp_path: Path) -> None:
    actor = "agent:test:node:duty"

    class LastBindingRoutines(Routines):
        def reserve_remove(self, *, name, reservation_id, enforce_last=False):
            del reservation_id, enforce_last
            raise RoutineServiceError(
                "ROUTINE_LAST_BINDING",
                f"routine {name!r} is the last routine bound to {actor}",
                (("routine", name), ("actor", actor)),
            )

    routines = LastBindingRoutines()

    def coordinator() -> RoutineCoordinator:
        return RoutineCoordinator(
            state_dir=tmp_path,
            routines=routines,
            ensure_coordinator=lambda _routine: None,
            retire_coordinator=lambda _routine: None,
            close_graph=lambda _graph, _actor: None,
        )

    first = coordinator()
    try:
        command = remove_command(
            operation_id="last-binding-replay", name="first", enforce_last=True
        )
        assert first.begin(command) is PortAdmission.ACCEPTED
        with pytest.raises(RoutineCoordinatorError) as failure:
            first.wait(command.operation_id, 3.0)
        assert failure.value.code == "ROUTINE_LAST_BINDING"
        assert dict(failure.value.data) == {"routine": "first", "actor": actor}
        with sqlite3.connect(first.database) as db:
            row = db.execute(
                "SELECT receipts_json FROM routine_coordinator_operations "
                "WHERE operation_id=?", (command.operation_id,)
            ).fetchone()
        assert row is not None
        assert json.loads(row[0])["rejectionData"] == {
            "code": "ROUTINE_LAST_BINDING",
            "data": [["routine", "first"], ["actor", actor]],
        }
    finally:
        assert first.close(3.0)

    restarted = coordinator()
    try:
        assert restarted.begin(
            remove_command(
                operation_id="last-binding-replay", name="first", enforce_last=True
            )
        ) is PortAdmission.ACCEPTED
        with pytest.raises(RoutineCoordinatorError) as replayed:
            restarted.wait("last-binding-replay", 3.0)
        assert replayed.value.code == "ROUTINE_LAST_BINDING"
        assert dict(replayed.value.data) == {"routine": "first", "actor": actor}
    finally:
        assert restarted.close(3.0)


def test_agent_compensation_error_retains_custody_across_restart(
    tmp_path: Path,
) -> None:
    routines = Routines()
    first_attempt = threading.Event()

    class AgentTimeout(RuntimeError):
        code = "AGENT_COMMAND_TIMEOUT"

    def fail_ensure(_routine):
        raise RoutineCoordinatorError(
            "INJECTED_LATE_FAILURE", "late ensure failure", "agent-replay"
        )

    def timed_out_destroy(_settlement: str, _actor: str, _token: str) -> bool:
        first_attempt.set()
        raise AgentTimeout("destroy result remains unsettled")

    command = add_command(
        operation_id="agent-replay",
        name="first",
        yaml_text=_yaml("first"),
        owner="user:test",
        produces="agent:test:node:coordinator",
        agent_compensation=AgentCreationCompensation(
            actor="resident",
            expected_entity_token="entity-original",
            settlement_id="settle-agent-replay",
        ),
    )
    first = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=fail_ensure,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
        compensate_agent=timed_out_destroy,
    )
    assert first.begin(command) is PortAdmission.ACCEPTED
    assert first_attempt.wait(2.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        projection = first.projection("agent-replay")
        if projection is not None and projection.state == "running":
            break
        time.sleep(0.01)
    assert projection is not None and projection.state == "running"
    with pytest.raises(RoutineCoordinatorTimeout):
        first.wait("agent-replay", 0.01)
    assert first.close(3.0)

    destroy_calls: list[tuple[str, str]] = []
    second = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=fail_ensure,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
        compensate_agent=lambda _settlement, actor, token: (
            destroy_calls.append((actor, token)) or False
        ),
    )
    try:
        with pytest.raises(RoutineCoordinatorError) as caught:
            second.wait("agent-replay", 4.0)
        assert caught.value.code == "INJECTED_LATE_FAILURE"
        assert destroy_calls == [("resident", "entity-original")]
    finally:
        assert second.close(3.0)


def test_legacy_agent_compensation_gains_stable_settlement_id(
    tmp_path: Path,
) -> None:
    routines = Routines()
    initializer = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    database = initializer.database
    assert initializer.close(3.0)
    operation_id = "legacy-agent-compensation"
    yaml_text = _yaml("first")
    registration_id = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
    legacy_payload = {
        "yaml": yaml_text,
        "owner": "user:test",
        "produces": "agent:test:node:coordinator",
        "registrationId": registration_id,
        "agentCompensation": {
            "actor": "resident",
            "expectedEntityToken": "entity-original",
        },
    }
    legacy_digest = hashlib.sha256(
        json.dumps(
            {"kind": "add", "name": "first", "payload": legacy_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO routine_coordinator_operations VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                "add",
                "first",
                legacy_digest,
                json.dumps(legacy_payload),
                "running",
                "agent_compensate",
                json.dumps(
                    {
                        "originalError": {
                            "code": "INJECTED_LATE_FAILURE",
                            "detail": "late failure",
                        }
                    }
                ),
                None,
                None,
                None,
                1,
            ),
        )

    settlements: list[tuple[str, str, str]] = []
    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
        compensate_agent=lambda settlement, actor, token: (
            settlements.append((settlement, actor, token)) or False
        ),
    )
    try:
        command = add_command(
            operation_id=operation_id,
            name="first",
            yaml_text=yaml_text,
            owner="user:test",
            produces="agent:test:node:coordinator",
            agent_compensation=AgentCreationCompensation(
                actor="resident",
                expected_entity_token="entity-original",
                settlement_id="agent-default-compensation:entity-original",
            ),
        )
        assert coordinator.begin(command) is PortAdmission.ACCEPTED
        with pytest.raises(RoutineCoordinatorError) as caught:
            coordinator.wait(operation_id, 3.0)
        assert caught.value.code == "INJECTED_LATE_FAILURE"
        assert settlements == [
            (
                "agent-default-compensation:entity-original",
                "resident",
                "entity-original",
            )
        ]
    finally:
        assert coordinator.close(3.0)


def test_legacy_post_snapshot_last_binding_refusal_restores_binding(
    tmp_path: Path,
) -> None:
    routines = RoutineService(
        pac=SimpleNamespace(rearm_boundary=lambda **_kwargs: 0), alarm=object(), state_dir=tmp_path,
        source_query=lambda _filter: [],
    )
    routines.add(yaml_text=_yaml("first"), owner="user:test", enabled=True)
    original = routines.status(name="first")
    routines.pause(name="first")
    initializer = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    database = initializer.database
    assert initializer.close(3.0)
    legacy_payload = {"enforceLast": True}
    legacy_digest = hashlib.sha256(
        json.dumps(
            {"kind": "remove", "name": "first", "payload": legacy_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO routine_coordinator_operations VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-stranded", "remove", "first", legacy_digest,
                json.dumps(legacy_payload), "running", "close_graphs",
                json.dumps({"routine": original}), None, None, None, 2,
            ),
        )
    restored: list[str] = []
    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda routine: restored.append(str(routine["name"])),
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    try:
        with pytest.raises(RoutineCoordinatorError) as caught:
            coordinator.wait("legacy-stranded", 3.0)
        assert caught.value.code == "ROUTINE_LAST_BINDING"
        assert routines.status(name="first")["enabled"] is True
        assert restored == ["first"]
    finally:
        assert coordinator.close(3.0)
        routines.close()


def test_legacy_migration_does_not_publish_before_commit(
    tmp_path: Path,
) -> None:
    routines = Routines()
    initializer = RoutineCoordinator(
        state_dir=tmp_path,
        routines=routines,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph, _actor: None,
    )
    database = initializer.database
    assert initializer.close(3.0)
    legacy_payload = {}
    legacy_digest = hashlib.sha256(
        json.dumps(
            {"kind": "remove", "name": "first", "payload": legacy_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with sqlite3.connect(database) as db:
        for index in (1, 2):
            db.execute(
                "INSERT INTO routine_coordinator_operations VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"legacy-{index}", "remove", f"routine-{index}",
                    legacy_digest, json.dumps(legacy_payload), "running", "snapshot",
                    "{}", None, None, None, 0,
                ),
            )
        db.execute(
            "CREATE TRIGGER fail_second_legacy_update BEFORE UPDATE ON "
            "routine_coordinator_operations WHEN OLD.operation_id='legacy-2' "
            "BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"
        )

    published: list[object] = []

    class Owner:
        def __init__(self) -> None:
            self.database = database

        def _publish(self, projection: object) -> None:
            published.append(projection)

    with pytest.raises(sqlite3.IntegrityError):
        _Generation(Owner(), 1)  # type: ignore[arg-type]
    assert published == []
