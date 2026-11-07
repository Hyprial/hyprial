"""Defect 3773296: real P2 publication/resolution and read-only readiness."""

from pathlib import Path
import os
import stat

import pytest

from hyprial.identity.impl.agents import (
    AgentConfig,
    AgentConfigError,
    AgentRegistry,
    resolve_agent_runtime_context,
)
from hyprial.identity.impl.agents.home.readiness_checks.checks import _config_check
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX mode regression; Windows uses ACLs")


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "process-home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "process-state"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    source = tmp_path / "C"
    for name, body in (("settings.json", "{}\n"), ("AGENTS.md", "Instructions\n")):
        path = source / "native" / "pi" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o644)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="test-owner", machine="test-node", hyprial_home=tmp_path / "hyprial",
    )
    try:
        registry.create("worker", config=AgentConfig(str(source)))
        registry.ensure_home("worker")
        yield registry
    finally:
        registry.close()


def _resolve(registry):
    return resolve_agent_runtime_context(
        registry=registry, agent_name="worker", harness="pi", cwd=None,
        tool_profile=TEST_TOOL_PROFILE,
    )


def _readiness(registry, *, running=False):
    worker = registry.require("worker")
    return _config_check(
        mode="agent-home-p2", config=worker.config,
        agent_home=Path(registry.home_receipt("worker").path),
        entity_token=worker.entity_token, harness="pi", running=running,
    )[0]


def test_fresh_pi_publish_and_resolve_are_private(agent):
    first = _resolve(agent)
    assert first is not None
    assert _resolve(agent) == first
    for item in first.projection.items:
        path = first.roots.projection_root / item.native_path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The report's alleged mode-bearing manifest is not this schema.
    assert all("mode" not in item for item in first.manifest.to_json()["items"])
    assert _readiness(agent).status == "pass"


def test_mode_only_change_does_not_cause_content_drift_after_chmod(agent):
    context = _resolve(agent)
    path = context.roots.projection_root / "settings.json"
    path.chmod(0o644)
    with pytest.raises(AgentConfigError, match="settings.json mode must be 0600"):
        _resolve(agent)
    agent.ensure_home("worker")
    with pytest.raises(AgentConfigError, match="settings.json mode must be 0600"):
        _resolve(agent)
    path.chmod(0o600)
    assert _resolve(agent) == context


def test_both_reported_errors_require_content_drift_as_well_as_mode(agent):
    context = _resolve(agent)
    path = context.roots.projection_root / "settings.json"
    path.write_text('{"changed":true}\n')
    path.chmod(0o644)
    with pytest.raises(AgentConfigError, match="settings.json mode must be 0600") as mode:
        _resolve(agent)
    print(str(mode.value))
    path.chmod(0o600)
    with pytest.raises(AgentConfigError, match="drifted after publication: settings.json") as drift:
        _resolve(agent)
    print(str(drift.value))
    agent.ensure_home("worker")
    with pytest.raises(AgentConfigError, match="drifted after publication: settings.json"):
        _resolve(agent)


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("change", ["mode", "content", "missing", "unexpected", "symlink"])
def test_readiness_refuses_existing_invalid_projection_without_writing(agent, running, change):
    context = _resolve(agent)
    path = context.roots.projection_root / "settings.json"
    if change == "mode":
        path.chmod(0o644)
    elif change == "content":
        path.write_text('{"changed":true}\n')
    elif change == "missing":
        path.unlink()
    elif change == "unexpected":
        extra = path.parent / "extra.json"
        extra.write_text("{}")
        extra.chmod(0o600)
    else:
        path.unlink()
        path.symlink_to(path.parent / "AGENTS.md")
    def snapshot():
        rows = []
        for item in sorted(path.parent.iterdir()):
            metadata = item.lstat()
            body = item.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
            rows.append((item.name, metadata.st_mode, metadata.st_mtime_ns, body))
        return rows
    before = snapshot()
    result = _readiness(agent, running=running)
    assert result.status == "blocked"
    assert "native projection" in result.detail
    expected = {
        "mode": "mode must be 0600", "symlink": "must not be a symbolic link",
    }.get(change, "drifted after publication")
    assert expected in result.detail
    assert snapshot() == before


def test_rebind_preview_lists_all_changes_and_preserves_mixed_modes(agent):
    from hyprial.identity.impl.agents.config_rebind.projection_diff import projection_changes

    context = _resolve(agent)
    root = context.roots.projection_root
    (root / "AGENTS.md").chmod(0o644)
    (root / "settings.json").write_text('{"changed":true}\n')
    extra = root / "extra.json"
    extra.write_text("{}")
    extra.chmod(0o600)
    worker = agent.require("worker")
    changes = projection_changes(worker, context.roots.agent_home, Path(worker.config.source))
    assert changes == [
        {"harness": "pi", "path": "AGENTS.md", "changes": ["mode"], "action": "publish-from-staging"},
        {"harness": "pi", "path": "extra.json", "changes": ["unexpected"], "action": "omit-from-new-projection"},
        {"harness": "pi", "path": "settings.json", "changes": ["content"], "action": "publish-from-staging"},
    ]
    assert stat.S_IMODE((root / "AGENTS.md").stat().st_mode) == 0o644
    assert extra.exists()


def test_rebind_preview_does_not_follow_linked_projection(agent, tmp_path):
    from hyprial.identity.impl.agents.config_rebind.projection_diff import projection_changes

    context = _resolve(agent)
    root = context.roots.projection_root
    parked = root.with_name("parked")
    root.rename(parked)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("NOT_A_REAL_SECRET")
    root.symlink_to(outside, target_is_directory=True)
    worker = agent.require("worker")
    changes = projection_changes(worker, context.roots.agent_home, Path(worker.config.source))
    assert changes == [{"harness": "pi", "path": ".", "changes": ["uninspectable"],
                        "action": "publish-from-staging"}]
    assert "private.txt" not in str(changes)


def test_readiness_refuses_linked_projection_ancestor(agent):
    context = _resolve(agent)
    native = context.roots.projection_root.parent
    parked = native.with_name("parked-native")
    native.rename(parked)
    native.symlink_to(parked, target_is_directory=True)
    result = _readiness(agent)
    assert result.status == "blocked"
    assert "config directory native must be a real directory" in result.detail
