"""I7a: the agent-cell registrar never erases an adapter member the owner wrote into the user agent cell.

Decision notes (contract i7-0 v2 sections 2.3 and 2.4):

- The live user agent cell carries ``agent.template: "user"`` (the registrar
  seeds the template's cell params), which is the marker the adapter
  routing index skips.  The registrar's ensure returns an existing cell
  unchanged and its seed writes only missing keys, so a reconcile and an
  index pass leave the owner's members and the revision as they were.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.cells.adapter_routing import build_adapter_routing_index
from hyprial.daemon.impl.cells.model import CellMember
from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from tests.daemon.composition.cells.test_agent_cell_registration import _Agents, _app
from tests.daemon.composition.cells.test_agent_template_params import _agent


def test_the_registrar_keeps_an_adapter_member_of_the_user_agent_cell(tmp_path: Path) -> None:
    user = _agent("hyprial-user", template="user")
    app = _app(tmp_path, _Agents(user))
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        cell = runtime.lookup(user.cell_id)
        assert cell is not None
        assert (cell.members, cell.param("agent.template")) == ((), "user")
        edited = runtime.update(
            user.cell_id,
            members=(CellMember("im", "adapter"),),
            params={**dict(cell.params), "cell.members": {"im": {"adapter": "personal-bot"}}},
            expect_revision=cell.revision,
        )

        app._agent_cell_registrar.reconcile()
        app._agent_cell_registrar.settle("hyprial-user")
        runtime.run_once()

        after = runtime.lookup(user.cell_id)
        assert after is not None
        assert (after.members, after.revision) == (edited.members, edited.revision)
        assert build_adapter_routing_index(runtime.index()).lookup("personal-bot") is None
    finally:
        close_cell_runtime(app)
