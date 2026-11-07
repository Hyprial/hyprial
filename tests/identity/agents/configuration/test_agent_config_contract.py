"""P21 T01/T02: public config shape, persistence, and identity orthogonality."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import hashlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.identity.impl.agents import (
    AgentConfigError,
    AgentRegistry,
    normalize_agent_config,
    require_agent_config,
    resolve_agent_runtime_context,
    validate_agent_config_location,
)
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def _config(source: Path, *extra_sources: Path, **extra: object) -> dict[str, object]:
    return {
        "sources": [
            {"path": str(path), "required": True}
            for path in (source, *extra_sources)
        ],
        "discovery": "explicit-only",
        **extra,
    }


@pytest.fixture
def app(tmp_path: Path) -> DaemonApplication:
    instance = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        hyprial_home=tmp_path / "hyprial-home",
    **_deep_module_daemon_dependencies())
    instance._inbox = object()  # type: ignore[assignment]
    instance._harnesses = _NoHarnesses()  # type: ignore[assignment]
    instance._presence = _NoPresence()  # type: ignore[assignment]
    try:
        yield instance
    finally:
        instance.agents.close()


class _NoHarnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _NoPresence:
    def actor_online(self, _actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def test_t01_rejects_a_second_source_and_names_the_violating_item(
    app: DaemonApplication, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with pytest.raises(DaemonRequestError) as caught:
        app.handle(
            "agent.create",
            {"name": "multi", "config": _config(first, second)},
        )

    assert caught.value.code == AgentConfigError.code
    assert "config.sources[1]" in str(caught.value)
    assert "exactly one source" in str(caught.value)
    assert app.agents.get("multi") is None


def test_t01_rejects_top_level_list_shape_and_names_config(
    app: DaemonApplication, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(DaemonRequestError) as caught:
        app.handle(
            "agent.create",
            {"name": "list-shape", "config": [_config(source)]},
        )

    assert caught.value.code == AgentConfigError.code
    assert "config" in str(caught.value)
    assert "object" in str(caught.value)
    assert "list" in str(caught.value)
    assert app.agents.get("list-shape") is None


def test_t01_rejects_resolution_chain_shape_and_names_precedence(
    app: DaemonApplication, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(DaemonRequestError) as caught:
        app.handle(
            "agent.create",
            {
                "name": "chain",
                "config": _config(source, precedence="later-wins"),
            },
        )

    assert caught.value.code == AgentConfigError.code
    assert "config.precedence" in str(caught.value)
    assert "resolution chains are not supported" in str(caught.value)
    assert app.agents.get("chain") is None


def test_t01_single_explicit_c_round_trips_through_ports_and_sqlite(
    app: DaemonApplication, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = _config(source)

    created = app.handle(
        "agent.create",
        {"name": "single", "cwd": str(tmp_path / "work"), "config": config},
    )["agent"]

    assert created["config"] == config
    assert "entityToken" not in created
    assert app.agents.require("single").to_json()["config"] == config
    second = AgentRegistry(
        app.state_dir / "agents.sqlite3",
        owner="owner",
        machine="node-1",
        hyprial_home=tmp_path / "hyprial-home",
    )
    try:
        assert second.require("single").to_json()["config"] == config
    finally:
        second.close()


def test_t01_agent_create_cli_emits_only_the_single_explicit_source_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    observed: dict[str, object] = {}

    def request(method: str, params: object = None) -> dict[str, object]:
        observed.update({"method": method, "params": params})
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "create", "--name", "single", "--config", str(source), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert observed == {
        "method": "agent.create",
        "params": {"name": "single", "config": _config(source.resolve())},
    }


def test_agent_create_help_describes_home_defaults() -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "create", "--help"],
        terminal_width=200,
    )
    help_text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "home's minimal config/." in help_text
    assert "home's workspace/)." in help_text


def test_agent_create_without_config_defaults_to_home_p2_for_all_harnesses(
    app: DaemonApplication, tmp_path: Path
) -> None:
    enclosing = tmp_path / "hyprial-home"
    enclosing.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(enclosing)], check=True)

    created = app.handle("agent.create", {"name": "default-p2"})["agent"]
    agent = app.agents.require("default-p2")
    agent_home = enclosing / "agents" / "default-p2"
    workspace = agent_home / "workspace"
    expected_config = _config(agent_home / "config")

    assert created["config"] == expected_config
    assert created["cwd"] == str(workspace)
    top = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(top).resolve() == workspace.resolve()
    assert agent.config is not None
    assert agent.config.to_json() == expected_config
    assert (agent_home / "config").is_dir()
    validate_agent_config_location(
        agent.config,
        agent_home=agent_home,
        cwd=agent.cwd,
    )

    for harness in ("claude", "codex", "pi"):
        context = resolve_agent_runtime_context(
            registry=app._agent_registry,
            agent_name=agent.actor,
            harness=harness,
            cwd=agent.cwd,
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        assert context.roots.agent_home == agent_home


def test_agent_create_reuse_preserves_default_config_and_workspace(
    app: DaemonApplication,
) -> None:
    first = app.handle("agent.create", {"name": "reused-default"})
    original = app.agents.require("reused-default")

    second = app.handle(
        "agent.create",
        {"name": "reused-default", "existing": "reuse"},
    )
    current = app.agents.require("reused-default")

    assert first["created"] is True
    assert second["created"] is False
    assert current.entity_token == original.entity_token
    assert current.config == original.config
    assert current.cwd == original.cwd


def test_transfer_received_identity_keeps_legacy_config_and_cwd(
    app: DaemonApplication,
) -> None:
    agent = app.agents.create_transfer_hosted(
        "received",
        pinned_owner="remote-owner",
    )

    assert agent.hosted_by == "transfer-receive"
    assert agent.config is None
    assert agent.cwd is None
    for harness in ("claude", "codex", "pi"):
        assert resolve_agent_runtime_context(
            registry=app._agent_registry,
            agent_name=agent.actor,
            harness=harness,
            cwd=agent.cwd,
            tool_profile=TEST_TOOL_PROFILE,
        ) is None


def test_agent_create_with_user_cwd_never_initializes_it(
    app: DaemonApplication, tmp_path: Path
) -> None:
    cwd = tmp_path / "user-workspace"
    cwd.mkdir()

    app.handle("agent.create", {"name": "explicit-cwd", "cwd": str(cwd)})

    assert not (cwd / ".git").exists()


@pytest.mark.parametrize(
    "variable",
    ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"),
)
def test_default_workspace_git_init_ignores_repository_environment(
    app: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    outside = tmp_path / f"outside-{variable.lower()}"
    monkeypatch.setenv(variable, str(outside))

    created = app.handle("agent.create", {"name": f"isolated-{variable.lower()}"})[
        "agent"
    ]

    workspace = Path(created["cwd"])
    assert (workspace / ".git").is_dir()
    assert not outside.exists()


def test_non_pi_start_does_not_initialize_a_legacy_default_workspace(
    app: DaemonApplication,
) -> None:
    created = app.handle("agent.create", {"name": "legacy-non-pi"})["agent"]
    workspace = Path(created["cwd"])
    import shutil

    shutil.rmtree(workspace / ".git")

    app.handle(
        "agent.create",
        {
            "name": "legacy-non-pi",
            "existing": "reuse",
            "harness": "codex",
            "runtime": "headless",
            "cwd": str(workspace),
        },
    )

    assert not (workspace / ".git").exists()


def test_pi_start_initializes_only_a_legacy_default_workspace(
    app: DaemonApplication, tmp_path: Path
) -> None:
    created = app.handle("agent.create", {"name": "legacy-default"})["agent"]
    workspace = Path(created["cwd"])
    subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
    )
    # Simulate an agent created before default workspaces became repositories.
    import shutil

    shutil.rmtree(workspace / ".git")
    assert not (workspace / ".git").exists()

    app.handle(
        "agent.create",
        {
            "name": "legacy-default",
            "existing": "reuse",
            "harness": "pi",
            "runtime": "headless",
            "cwd": str(workspace),
        },
    )

    top = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(top).resolve() == workspace.resolve()


def test_t01_manifest_fixes_loaded_items_and_content_digests(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    nested = source / "shared" / "skills" / "review"
    nested.mkdir(parents=True)
    skill = nested / "SKILL.md"
    skill.write_text("review-v1\n", encoding="utf-8")
    settings = source / "native" / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"enabled":true}\n', encoding="utf-8")
    config = normalize_agent_config(_config(source))
    assert config is not None

    manifest = config.freeze_manifest()

    assert [item.path for item in manifest.items] == [
        "native/claude/settings.json",
        "shared/skills/review/SKILL.md",
    ]
    assert [item.digest for item in manifest.items] == [
        hashlib.sha256(settings.read_bytes()).hexdigest(),
        hashlib.sha256(skill.read_bytes()).hexdigest(),
    ]
    assert manifest.source == str(source)
    assert type(manifest).from_json(manifest.to_json()) == manifest
    config.verify_manifest(manifest)

    skill.write_text("review-v2\n", encoding="utf-8")
    with pytest.raises(AgentConfigError, match="shared/skills/review/SKILL.md"):
        config.verify_manifest(manifest)


def test_t01_c_location_rejects_runtime_overlap_but_allows_agent_config(
    app: DaemonApplication, tmp_path: Path
) -> None:
    agent_home = tmp_path / "hyprial-home" / "agents" / "worker"
    cwd = tmp_path / "work"
    cwd.mkdir()

    created = app.handle(
        "agent.create",
        {"name": "worker", "cwd": str(cwd), "config": _config(agent_home / "config")},
    )["agent"]
    assert created["config"] == _config(agent_home / "config")

    for name, source in (
        ("whole-home", tmp_path / "hyprial-home" / "agents" / "whole-home"),
        ("state", tmp_path / "hyprial-home" / "agents" / "state" / "state"),
        ("secrets", tmp_path / "hyprial-home" / "agents" / "secrets" / "secrets"),
        ("cwd", cwd),
    ):
        with pytest.raises(DaemonRequestError, match="config C"):
            app.handle(
                "agent.create",
                {"name": name, "cwd": str(cwd), "config": _config(source)},
            )


def test_t01_default_workspace_does_not_overlap_config_but_home_stays_rejected(
    app: DaemonApplication, tmp_path: Path
) -> None:
    home_root = tmp_path / "hyprial-home" / "agents"
    agent_home = home_root / "worker"
    created = app.handle(
        "agent.create",
        {
            "name": "worker",
            "harness": "codex",
            "runtime": "headless",
            "config": _config(agent_home / "config"),
        },
    )["agent"]

    assert created["cwd"] == str(agent_home / "workspace")
    assert created["config"] == _config(agent_home / "config")

    home_cwd = home_root / "home-cwd"
    with pytest.raises(DaemonRequestError, match="agent cwd"):
        app.handle(
            "agent.create",
            {
                "name": "home-cwd",
                "cwd": str(home_cwd),
                "config": _config(home_cwd / "config"),
            },
        )

    rejected_home = home_root / "whole-home"
    with pytest.raises(DaemonRequestError, match="whole agent home"):
        app.handle(
            "agent.create",
            {
                "name": "whole-home",
                "harness": "codex",
                "runtime": "headless",
                "config": _config(rejected_home),
            },
        )


def test_t02_harness_and_cwd_selection_cannot_rewrite_identity_c(
    app: DaemonApplication, tmp_path: Path
) -> None:
    source = tmp_path / "personality"
    (source / "shared" / "skills" / "identity").mkdir(parents=True)
    (source / "shared" / "skills" / "identity" / "SKILL.md").write_text("v1\n")
    first_cwd = tmp_path / "repo-a"
    second_cwd = tmp_path / "repo-b"
    first_cwd.mkdir()
    second_cwd.mkdir()
    app.handle(
        "agent.create",
        {"name": "switcher", "cwd": str(first_cwd), "config": _config(source)},
    )
    original = app.agents.require("switcher")
    original_manifest = require_agent_config(
        original.config, actor=original.actor
    ).freeze_manifest()

    for harness in ("claude", "codex", "pi"):
        result = app.handle(
            "agent.create",
            {
                "name": "switcher",
                "existing": "reuse",
                "harness": harness,
                "runtime": "headless",
                "cwd": str(second_cwd),
            },
        )
        current = app.agents.require("switcher")
        assert result["agent"]["config"] == _config(source)
        assert current.entity_token == original.entity_token
        assert current.cwd == str(second_cwd)
        assert require_agent_config(
            current.config, actor=current.actor
        ).freeze_manifest() == original_manifest


def test_t02_legacy_missing_config_is_not_an_approved_empty_c(
    app: DaemonApplication,
) -> None:
    app.agents.create("legacy")
    agent = app.agents.require("legacy")
    assert agent.config is None
    assert agent.to_json()["config"] is None
    with pytest.raises(AgentConfigError, match="no explicit personality config C"):
        require_agent_config(agent.config, actor=agent.actor)
