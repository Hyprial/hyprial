"""The actual daemon composition publishes native homes through real preparers."""

import time

import pytest

from hyprial.daemon.impl.composition.domains import AgentSessionDomains
from hyprial.daemon.impl.desired_state import DesiredStateStore
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


@pytest.mark.parametrize("harness,filename,content", [
    ("claude", "settings.json", "{}\n"),
    ("codex", "config.toml", 'cli_auth_credentials_store = "file"\n'),
    ("pi", "settings.json", "{}\n"),
])
def test_daemon_domains_publish_real_native_home(tmp_path, harness, filename, content):
    source = tmp_path / "personality"
    config_file = source / "native" / harness / filename
    config_file.parent.mkdir(parents=True)
    config_file.write_text(content)
    home = tmp_path / "home"
    home.mkdir()
    desired = DesiredStateStore(tmp_path / "state" / "desired-state.json")
    domains = AgentSessionDomains(
        database=tmp_path / "state" / "agents.sqlite3",
        desired_state=desired, owner="owner", node_id="node",
        daemon_epoch="test-epoch", hyprial_home=home,
        worker_running=lambda *_args, **_kwargs: False, clock=time.time,
    )
    try:
        domains.agents.create(
            "worker", cwd=str(tmp_path / "work"),
            config={"sources": [{"path": str(source), "required": True}], "discovery": "explicit-only"},
        )
        context = domains.agent.prepare_runtime_context(
            "worker", harness, str(tmp_path / "work"), TEST_TOOL_PROFILE
        )
        assert context is not None and context.authority_prepared
        assert context.roots.native_root.is_relative_to(home)
        if harness != "pi":
            assert (context.roots.native_root / filename).read_text() == content
            session_name = "projects" if harness == "claude" else "sessions"
            link = context.roots.native_root / session_name
            assert link.is_symlink()
            assert link.resolve() == context.roots.session_root.resolve()
        else:
            # Pi has no native publication provider; its optional port remains
            # absent rather than borrowing a Claude/Codex native view.
            assert not (context.roots.native_root / filename).exists()
    finally:
        assert domains.close()
