"""S8 with OrgFS unavailable: identity and messaging are local; cells catch up.

Two outage shapes (``tests/daemon/acceptance`` ``Outage``):

- ``space-unreachable``: the OrgFS runtime itself refuses, so no cell
  runtime composes.  attach still registers the agent identity locally and
  answers ``cell.state == "deferred"``; messages flow; a detach in the
  outage is recorded on the agent too.  When OrgFS returns and the runtime
  composes, the agent cell is reconciled to the record (custody param,
  membership matching the last attach/detach).
- ``fs-raises``: the runtime is composed but every OrgFS call raises.  The
  cell is recorded in the local cell cache (``"recorded"``) and the
  document is written once OrgFS is back.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.identity import org_space_name
from tests.daemon.acceptance.acceptance_support import (
    Outage,
    attach_cell_orgfs,
    send,
    skip_compose_backoff,
)
from tests.daemon.attach.attach_support import CUSTODY_PARAM, attach, detach, operator
from tests.support.golden.app import ORG, OWNER_USER, golden_daemon as golden_daemon
from tests.support.orgfs_fakes.app import join_test_org


def _pending(app: Any, actor: str) -> list[str]:
    listed = app.handle("message.pending.list", operator(actor))
    return [str(item["messageId"]) for item in listed["messages"]]


def test_attach_and_messaging_work_without_a_cell_runtime(golden_daemon, monkeypatch) -> None:
    daemon = golden_daemon()
    app = daemon.app
    cells = attach_cell_orgfs(app, monkeypatch)
    outage = Outage(daemon, cells, "space-unreachable")
    outage.down()

    attached = attach(app, "ext")
    assert attached["created"] is True and attached["attached"] is True
    assert attached["cell"] == {"state": "deferred", "revision": None}
    assert getattr(app, "_cell_runtime", None) is None
    agent = app.agents.get("ext")
    assert agent is not None and attached["cellId"] == agent.cell_id

    message = str(send(app, OWNER_USER, agent.uri, "down")["messageId"])
    assert _pending(app, agent.uri) == [message]
    app.handle("message.read_body", {**operator(agent.uri), "messageId": message})
    assert app.handle("message.ack", {**operator(agent.uri), "messageId": message})["ok"]
    assert attach(app, "ext")["changed"] is False

    outage.up()
    skip_compose_backoff(app)
    runtime = cell_runtime(app)
    assert runtime is not None
    cell = runtime.lookup(agent.cell_id)
    assert cell is not None and cell.param(CUSTODY_PARAM) == "external"
    assert [(m.ref, m.role) for m in cell.members] == [(agent.uri, "harness")]


def test_a_detach_during_the_outage_reaches_the_cell(golden_daemon, monkeypatch) -> None:
    daemon = golden_daemon()
    app = daemon.app
    cells = attach_cell_orgfs(app, monkeypatch)
    attach(app, "ext")
    agent = app.agents.get("ext")
    runtime = app._cell_runtime
    assert runtime.lookup(agent.cell_id).has_member(agent.uri)

    app._agent_cells_detach()
    assert runtime.close()
    app._cell_runtime = None
    outage = Outage(daemon, cells, "space-unreachable")
    outage.down()
    detached = detach(app, "ext")
    assert detached["changed"] is True and detached["cell"]["state"] == "deferred"
    assert dict(app.agents.get("ext").capabilities)["attached"] is False

    outage.up()
    skip_compose_backoff(app)
    fresh = cell_runtime(app)
    assert fresh is not None and fresh is not runtime
    assert fresh.lookup(agent.cell_id).members == ()


def test_fs_raises_records_locally_and_flushes_on_return(golden_daemon, monkeypatch) -> None:
    daemon = golden_daemon()
    app = daemon.app
    cells = attach_cell_orgfs(app, monkeypatch)
    join_test_org(app, ORG)
    runtime = cell_runtime(app)
    assert runtime is not None
    runtime.run_once()
    outage = Outage(daemon, cells, "fs-raises")
    outage.down()

    attached = attach(app, "ext")
    agent = app.agents.get("ext")
    assert attached["cell"]["state"] == "recorded"
    runtime.run_once()
    assert agent.cell_id in runtime.dirty()
    message = str(send(app, OWNER_USER, agent.uri, "fs-down")["messageId"])
    assert _pending(app, agent.uri) == [message]

    outage.up()
    runtime.run_once()
    assert agent.cell_id not in runtime.dirty()
    space = next(info for info in cells.inner.spaces() if info.name == org_space_name(ORG))
    content, _ = cells.inner.read_text(space.space_id, f"cells/agent/{agent.cell_id}.json")
    assert CUSTODY_PARAM in content and agent.uri in content
