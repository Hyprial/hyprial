"""A teleport-landed agent is visible through the agent domain view.

``lifecycle.start`` resolves the agent through ``app.agents`` (the agent
domain's projection).  The teleport target must therefore land through that
same view, otherwise the projection never observes the row and the strict
resume path searches the wrong home.  ``undo_landing`` must leave the same
view empty again.
"""

from __future__ import annotations

from pathlib import Path

from ..test_daemon_composition import OWNER, app  # noqa: F401
from ..test_registry_host import AGENT, _bundle
from ...target.support import RecordingSession


def test_landed_agent_is_visible_to_the_lifecycle_view(app, tmp_path: Path) -> None:  # noqa: F811
    app._start_teleport_target(RecordingSession())
    host = app._teleport_target.service.host
    assert app.agents.get(AGENT) is None
    landed = host.land(_bundle(tmp_path, owner=OWNER), name=AGENT, harness="claude")
    visible = app.agents.get(AGENT)
    assert visible is not None
    assert visible.entity_token == landed.entity_token
    assert app.agents.projection(AGENT) is not None
    assert host.agent_exists(AGENT) is True


def test_undo_landing_leaves_the_view_consistent(app, tmp_path: Path) -> None:  # noqa: F811
    app._start_teleport_target(RecordingSession())
    host = app._teleport_target.service.host
    landed = host.land(_bundle(tmp_path, owner=OWNER), name=AGENT, harness="claude")
    host.undo_landing(landed)
    assert app.agents.get(AGENT) is None
    assert host.agent_exists(AGENT) is False


def test_activation_config_binding_reaches_the_view(app, tmp_path: Path) -> None:  # noqa: F811
    app._start_teleport_target(RecordingSession())
    host = app._teleport_target.service.host
    landed = host.land(_bundle(tmp_path, owner=OWNER), name=AGENT, harness="claude")
    host.activate(landed)
    visible = app.agents.get(AGENT)
    assert visible is not None
    assert visible.config is not None
