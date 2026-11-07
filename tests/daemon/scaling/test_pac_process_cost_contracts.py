"""Scale-cost contracts for PAC storage scans and process liveness probes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hyprial.daemon.impl.pac.graphs.authority as authority_module
import hyprial.daemon.impl.pac.storage.store as store_module
import hyprial.kernel.impl.processes.owned_process as owned_process_module
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime.operations import (
    _GraphWorkflowServiceOps,
)
from hyprial.kernel import ProcessLivenessState
from hyprial.kernel.impl.processes.owned_process import OwnedProcessGroup
from tests.support.scaling import CountingConnection, assert_flat, at_scales


OWNER = "user:scale-owner"
SCALE = 16



class _ModuleProxy:
    """Delegate to a real module, overriding only the named attributes."""

    def __init__(self, module: object, **overrides: object) -> None:
        self._module = module
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> object:
        return getattr(self._module, name)

def _insert_graph(
    database: Any,
    graph_id: str,
    *,
    closed: bool = False,
    workflow: bool = False,
) -> None:
    database.execute(
        "INSERT INTO graphs"
        "(graph_id,name,version,created_by,created_at,closed_at) "
        "VALUES (?,?,1,?,1,?)",
        (graph_id, graph_id, OWNER, 2 if closed else None),
    )
    if workflow:
        database.execute(
            "INSERT INTO workflow_graphs"
            "(graph_id,specification_ref,specification_digest,on_failure,state) "
            "VALUES (?,?,?,'terminate',?)",
            (
                graph_id,
                f"spec:{graph_id}",
                f"digest:{graph_id}",
                "completed" if closed else "running",
            ),
        )


def _insert_notification(
    database: Any,
    *,
    event_id: str,
    graph_id: str,
    at: int,
    edge: str = "edge",
) -> None:
    database.execute(
        "INSERT INTO notifications"
        "(event_id,edge,kind,recipient,text,sender,at,plan_json) "
        "VALUES (?,?,'turn',?,?,?, ?,?)",
        (
            event_id,
            edge,
            OWNER,
            f"notification for {graph_id}",
            OWNER,
            at,
            json.dumps({"graphId": graph_id}),
        ),
    )


class _RetryingSender:
    def send(self, **_kwargs: object) -> str:
        raise ConnectionError("isolated retryable delivery")


class _DrainProbe(_GraphWorkflowServiceOps):
    def __init__(self) -> None:
        self.sender = _RetryingSender()
        self.clock = lambda: 1
        self.logger = None
        self._delivery_receipt = None


def _drain_fixture(root: Path, size: int) -> tuple[PacGraphStore, str]:
    root.mkdir(parents=True)
    store = PacGraphStore(root / "pac-graph.sqlite3")
    database = store.write()
    target = "target"
    _insert_graph(database, target)
    database.execute(
        "INSERT INTO flag_events"
        "(event_id,graph_id,version,node_id,action,actor,at) "
        "VALUES ('target-event',?,1,'node','set',?,1)",
        (target, OWNER),
    )
    _insert_notification(
        database, event_id="target-event", graph_id=target, at=1
    )
    _insert_notification(
        database,
        event_id="overdue:target:v1:clock:1",
        graph_id=target,
        at=2,
        edge="clock:target",
    )
    for index in range(size):
        graph_id = f"unrelated-{index:04d}"
        _insert_graph(database, graph_id)
        database.execute(
            "INSERT INTO flag_events"
            "(event_id,graph_id,version,node_id,action,actor,at) "
            "VALUES (?,?,1,'node','set',?,?)",
            (f"flag-{index:04d}", graph_id, OWNER, 10 + index),
        )
        _insert_notification(
            database,
            event_id=f"flag-{index:04d}",
            graph_id=graph_id,
            at=10 + index,
            edge=f"flag-{index:04d}",
        )
        _insert_notification(
            database,
            event_id=f"overdue:{graph_id}:v1:clock:{index}",
            graph_id=graph_id,
            at=10_000 + index,
            edge=f"clock-{index:04d}",
        )
    database.commit()
    return store, target


def test_workflow_notify_drain_cost_is_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One graph drain must not fetch or decode other graphs' notifications."""

    json_loads = 0
    real_loads = store_module.json.loads

    def counting_loads(value: object, *args: object, **kwargs: object) -> object:
        nonlocal json_loads
        json_loads += 1
        return real_loads(value, *args, **kwargs)

    # A module-local proxy: patching the global json module would also count
    # (and affect) other threads in the same xdist worker (review 907).
    monkeypatch.setattr(store_module, "json", _ModuleProxy(json, loads=counting_loads))

    def measure(size: int) -> tuple[int, int]:
        nonlocal json_loads
        store, target = _drain_fixture(tmp_path / f"drain-{size}", size)
        counted = CountingConnection(store._db)
        store._db = counted  # type: ignore[assignment]
        json_loads = 0
        try:
            _DrainProbe()._drain(store, target)
            return counted.rows_fetched, json_loads
        finally:
            store.close()

    at_n, at_4n = at_scales(measure, SCALE)
    assert_flat(at_n, at_4n)


class _TickProbe(_GraphWorkflowServiceOps):
    def __init__(self, store: PacGraphStore) -> None:
        self.store = store
        self.clock = lambda: 1
        self.logger = None
        self._asynchronous = False

    def _open(self) -> PacGraphStore:
        return self.store

    def _project(self, *_args: object) -> None:
        raise AssertionError("a completed historical graph was projected")


def _tick_rows(root: Path, size: int) -> int:
    root.mkdir(parents=True)
    store = PacGraphStore(root / "pac-graph.sqlite3")
    database = store.write()
    for index in range(size):
        _insert_graph(
            database, f"closed-{index:04d}", closed=True, workflow=True
        )
    database.commit()
    counted = CountingConnection(store._db)
    store._db = counted  # type: ignore[assignment]
    _TickProbe(store)._tick(at=1)
    return counted.rows_fetched


def test_pac_tick_rows_are_flat_with_closed_history(tmp_path: Path) -> None:
    """TICK_GRAPH_SELECT must not project closed completed history."""

    at_n, at_4n = at_scales(
        lambda size: _tick_rows(tmp_path / f"tick-{size}", size), SCALE
    )
    assert_flat(at_n, at_4n)
    assert at_n == 0


def _legacy_scan_fixture(root: Path, dead_rows: int) -> Path:
    root.mkdir(parents=True)
    database_path = root / "pac-graph.sqlite3"
    store = PacGraphStore(database_path)
    database = store.write()
    for index in range(dead_rows):
        graph_id = f"workflow-dead-{index:04d}"
        # Closed: the permanently stuck case, not merely a busy open graph.
        _insert_graph(database, graph_id, workflow=True, closed=True)
        _insert_notification(
            database,
            event_id=f"dead-{index:04d}",
            graph_id=graph_id,
            at=index,
            edge=f"dead-{index:04d}",
        )
    _insert_graph(database, "legacy-live")
    _insert_notification(
        database,
        event_id="live-delivery",
        graph_id="legacy-live",
        at=dead_rows + 1,
        edge="live",
    )
    database.commit()
    store.close()
    return database_path


def _bare_authority(database: Path) -> PacGraphAuthority:
    authority = object.__new__(PacGraphAuthority)
    authority.database = database
    return authority


@pytest.mark.xfail(
    strict=True,
    reason="card af23508b: legacy scan applies LIMIT 128 before dead-row filtering",
)
def test_legacy_delivery_scan_returns_row_after_dead_prefix(tmp_path: Path) -> None:
    """Dead shared-table rows must not starve a later legacy delivery."""

    def delivered(size: int) -> bool:
        database = _legacy_scan_fixture(tmp_path / f"delivery-{size}", size)
        rows = _bare_authority(database)._scan_deliveries()
        return any(row.event_id == "live-delivery" for row in rows)

    at_n, at_4n = at_scales(delivered, 40)
    assert at_n and at_4n


def test_legacy_delivery_scan_rows_stay_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One legacy scan has an explicit row-read cap despite dead history."""

    real_store = PacGraphStore
    observed: list[CountingConnection] = []

    def measuring_store(path: Path, *, read_only: bool = False) -> PacGraphStore:
        store = real_store(path, read_only=read_only)
        counted = CountingConnection(store._db)
        store._db = counted  # type: ignore[assignment]
        observed.append(counted)
        return store

    monkeypatch.setattr(authority_module, "PacGraphStore", measuring_store)

    def rows_fetched(size: int) -> int:
        observed.clear()
        database = _legacy_scan_fixture(tmp_path / f"scan-{size}", size)
        _bare_authority(database)._scan_deliveries()
        assert len(observed) == 1
        return observed[0].rows_fetched

    at_n, at_4n = at_scales(rows_fetched, 160)
    assert_flat(at_n, at_4n)
    # 128 candidate rows plus at most one workflow/graph lookup row each.
    assert at_n <= 256


def test_liveness_subprocess_calls_are_zero_per_poll_facts_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Darwin PollFacts round stays fork-free as live group count grows."""

    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=0, stdout="birth-stamp\n")

    # Module-local proxies, not the global sys/os/subprocess (review 907).
    monkeypatch.setattr(owned_process_module, "sys", _ModuleProxy(sys, platform="darwin"))
    monkeypatch.setattr(
        owned_process_module, "os", _ModuleProxy(os, killpg=lambda *_args: None)
    )
    monkeypatch.setattr(
        owned_process_module, "subprocess", _ModuleProxy(subprocess, run=fake_run)
    )
    monkeypatch.setattr(
        owned_process_module,
        "_darwin_process_birth_identity",
        lambda _pid: "ps:birth-stamp",
        raising=False,
    )

    def calls_per_round(group_count: int) -> int:
        nonlocal calls
        groups = [OwnedProcessGroup(label="scale-probe") for _ in range(group_count)]
        for offset, group in enumerate(groups):
            group._observed = True
            group._pid = 42 + offset
            group._identity = "ps:birth-stamp"
        calls = 0
        for group in groups:
            assert group.liveness().state is ProcessLivenessState.ALIVE
        return calls

    at_n, at_4n = at_scales(calls_per_round, SCALE)
    assert_flat(at_n, at_4n)
    assert at_n == 0
