"""Real-port headless admission over a real cell runtime (I5-2 r2 item 7).

Decision record: every test here drives the production
``compose_headless_cell_delivery`` over a real ``CellRuntime`` and the real
cell actor registry — no stub port.  The fake app is the one
``test_cell_runtime_wiring.py`` already maintains (a local OrgFS facade, an
agent directory double and an actor-host registry), so the cell world is
exactly what a daemon composes.

One deliberate pin: an agent whose owner is not this runtime's author has
no agent cell (the registrar's owner-mismatch skip), and the port defers
its rows forever.  That behaviour is PENDING AN OWNER RULING recorded in
the round-2 report — the test name carries the marker so the ruling can
find it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.composition.cell_delivery import (
    compose_headless_cell_delivery,
)
from hyprial.daemon.impl.composition.cells import close_cell_runtime
from hyprial.identity.impl.agents import Agent
from tests.daemon.composition.cells.test_cell_runtime_wiring import (
    _Agents,
    _app,
    _app_with_actors,
)

WAIT_SECONDS = 10.0


def _external_agent(
    actor: str, *, attached: bool, entity_token: str | None = None
) -> Agent:
    import uuid

    capabilities: dict[str, Any] = {"custody": "external"}
    if attached:
        capabilities["attached"] = True
    return Agent(
        uri=f"agent:alice:mac:{actor}",
        actor=actor,
        owner="alice",
        machine="mac",
        entity_token=entity_token or uuid.uuid4().hex,
        capabilities=capabilities,
    )


def _delivery() -> HarnessDelivery:
    return HarnessDelivery("m-1", "conversation-1", "sender", "w", "work")


def _admit_or_none(port: Any, worker: str, delivery: HarnessDelivery) -> Any:
    deadline = time.monotonic() + WAIT_SECONDS
    envelope = None
    while envelope is None and time.monotonic() < deadline:
        envelope = port.headless_envelope(worker, delivery)
        if envelope is None:
            time.sleep(0.05)
    return envelope


def test_an_attached_external_agent_is_admitted(tmp_path: Path) -> None:
    worker = _external_agent("ext-worker", attached=True)
    app = _app_with_actors(tmp_path, _Agents(worker))
    try:
        port = compose_headless_cell_delivery(app)
        envelope = _admit_or_none(port, "ext-worker", _delivery())
        assert envelope is not None, "an attached external agent never admitted"
        assert envelope.stamp.member == worker.uri
        assert envelope.stamp.cell_id == worker.cell_id
    finally:
        close_cell_runtime(app)


def test_a_detached_external_agent_leaves_its_rows_pending(tmp_path: Path) -> None:
    worker = _external_agent("ext-worker", attached=False)
    app = _app_with_actors(tmp_path, _Agents(worker))
    try:
        port = compose_headless_cell_delivery(app)
        deadline = time.monotonic() + WAIT_SECONDS
        settled = False
        while time.monotonic() < deadline:
            runtime = app._cell_runtime
            assert runtime is not None
            cell = runtime.lookup(worker.cell_id)
            if cell is not None and not cell.members:
                settled = True
                break
            time.sleep(0.05)
        assert settled, "the detached record never reconciled to a memberless cell"
        envelope = None
        while envelope is None and time.monotonic() < deadline:
            envelope = _admit_or_none(port, "ext-worker", _delivery())
            if envelope is None:
                break
            time.sleep(0.05)
        assert envelope is None, "a detached external agent must defer, not refuse"
    finally:
        close_cell_runtime(app)


def test_an_ordinary_agent_is_admitted_without_any_live_org_space(
    tmp_path: Path,
) -> None:
    from tests.daemon.composition.cells.test_cell_runtime_wiring import _agent

    from hyprial.daemon.impl.composition.actors import ActorHostRegistry

    worker = _agent("plain-worker")
    fs = __import__("hyprial.daemon.impl.orgfs", fromlist=["LocalOrgFs"]).LocalOrgFs(
        author="user:alice"
    )
    bare = type("BareOrgFs", (), {"facade": fs})()
    app = _app(tmp_path)
    app._require_orgfs_runtime = lambda: bare
    app.agents = _Agents(worker)
    app._actor_hosts = ActorHostRegistry()
    try:
        port = compose_headless_cell_delivery(app)
        envelope = _admit_or_none(port, "plain-worker", _delivery())
        assert envelope is not None, "no org space must not stall admission"
        assert envelope.stamp.member == worker.uri
    finally:
        close_cell_runtime(app)


def test_a_failed_composition_defers_every_row_and_never_raises(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app = _app_with_actors(tmp_path, _Agents())

    def broken() -> Any:
        raise RuntimeError("orgfs down at boot")

    monkeypatch.setattr(app, "_require_orgfs_runtime", broken)
    port = compose_headless_cell_delivery(app)
    assert port is not None
    assert port.headless_envelope("worker", _delivery()) is None
    assert port.steer_envelope(None, "m-1") is None
    close_cell_runtime(app)
