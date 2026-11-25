"""The default agent routine is always the packaged ``agent-home-setup`` template.

Registration reads no cell data.  IPC creation (``agent.create``) composes
the cell runtime so the new agent's cell is registered; the managed-launch
path (``agent start``, PAC workers, the startup seed) runs at daemon boot,
before OrgFS is needed, and composes none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hyprial.biz.impl.routine.storage.store import RoutineStore
from hyprial.daemon.impl.cells.runtime import CACHE_FILENAME
from tests.identity.agents.registry.entity.support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
    enable_routines,
)


class _CapturingService:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def list(self) -> dict[str, list[object]]:
        return {"routines": []}

    def add(self, *, yaml_text: str, owner: str, enabled: bool) -> None:
        self.added.append({"yaml_text": yaml_text, "owner": owner, "enabled": enabled})

    def close(self) -> None:
        return None


def _uri(name: str) -> str:
    return f"agent:{OWNER}:{NODE}:{name}"


def _capturing_app(tmp_path: Path) -> tuple[Any, _CapturingService]:
    app = application(tmp_path)
    service = _CapturingService()
    app._routine_service = service
    app._routine_admit = lambda _spec: None
    return app, service


def test_agent_create_registers_the_packaged_template(tmp_path: Path) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    created = app.handle("agent.create", {"name": "delta"})
    assert created["ok"] is True and "routineWarning" not in created
    store = RoutineStore(tmp_path / "routines.sqlite3")
    try:
        rows = {row.name: row for row in store.list_routines()}
    finally:
        store.close()
    # Keyed by the agent's official address (id URI under UUID addressing).
    routine = rows[app._default_agent_routine_name(app.agents.require("delta").uri)]
    assert yaml.safe_load(routine.yaml_text)["schedule"]["interval"] == "15m"


def test_agent_create_composes_the_cell_runtime_and_registers_the_agent_cell(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    assert getattr(app, "_cell_runtime", None) is None
    assert app.handle("agent.create", {"name": "delta"})["ok"] is True
    runtime = getattr(app, "_cell_runtime", None)
    assert runtime is not None and runtime.identity.writer == f"user:{OWNER}"
    assert runtime.lookup(app.agents.require("delta").cell_id) is not None


def test_composition_failure_does_not_fail_agent_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    enable_routines(app, tmp_path)

    def unavailable() -> None:
        raise RuntimeError("orgfs down")

    monkeypatch.setattr(app, "_require_orgfs_runtime", unavailable)
    created = app.handle("agent.create", {"name": "delta"})
    assert created["ok"] is True and "routineWarning" not in created
    assert getattr(app, "_cell_runtime", None) is None


def test_managed_launch_composes_no_cell_runtime(tmp_path: Path) -> None:
    app, service = _capturing_app(tmp_path)
    assert app._ensure_agent(_uri("foo"), harness="pi", interactive=False) is not None
    assert getattr(app, "_cell_runtime", None) is None
    assert list(tmp_path.rglob(CACHE_FILENAME)) == []
    (added,) = service.added
    assert added["enabled"] is True
    assert yaml.safe_load(added["yaml_text"])["schedule"]["interval"] == "15m"


def test_already_bound_agent_registers_nothing(tmp_path: Path) -> None:
    app, service = _capturing_app(tmp_path)
    app._routines_bound_to = lambda _uri: True
    assert app._register_default_agent_routine(app.agents.create("worker")) is None
    assert service.added == []
