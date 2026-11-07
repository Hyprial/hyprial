from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import HarnessLaunchSpec
from hyprial.identity import Agent, AgentConfig
from hyprial.daemon.impl.operations.management import RegistryManagementHandler
from hyprial.daemon.impl.squire import SetupIdentity, SquireSetup
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

IDENTITY = SetupIdentity(
    owner="allen",
    owner_key="owner-key-1",
    login_name="alicesmith",
    machine="mac-studio",
    machine_key="machine-key-1",
)


@dataclass
class FakeAgentRegistry:
    agent: Any | None = None
    owner = "allen"
    machine = "mac-studio"

    def __post_init__(self) -> None:
        self.creates: list[dict[str, Any]] = []
        self.pin_calls: list[tuple[str, str]] = []
        self._pins: dict[str, str] = {}

    def get(self, actor: str) -> Any | None:
        assert actor == "squire"
        return self.agent

    def resolve_workspace_path(self, actor: str) -> str:
        assert actor == "squire"
        return str(self.agents_root / actor / "workspace")

    def create(self, actor: str, **configuration: Any) -> Any:
        self.creates.append({"actor": actor, **configuration})
        self.agent = Agent(
            actor=actor,
            uri="agent:allen:mac-studio:squire",
            owner=self.owner,
            machine=self.machine,
            **configuration,
        )
        return self.agent

    def save(self, agent: Agent) -> Agent:
        self.agent = agent
        return agent

    def pins(self) -> dict[str, str]:
        return dict(self._pins)

    def pin(self, adapter: str, actor: str) -> str | None:
        self.pin_calls.append((adapter, actor))
        assert self.agent is not None
        previous = self._pins.get(adapter)
        self._pins[adapter] = self.agent.uri
        return previous

    def restore_pin_if_absent(self, adapter: str, actor: str) -> str | None:
        previous = self._pins.get(adapter)
        if previous is None:
            return self.pin(adapter, actor)
        assert self.agent is not None
        if previous != self.agent.uri:
            raise ValueError("pin replacement blocks rollback")
        return previous

    def unpin_if(self, adapter: str, expected_actor: str | None) -> str | None:
        previous = self._pins.get(adapter)
        if expected_actor is None or previous == expected_actor:
            self._pins.pop(adapter, None)
        return previous


class FakeHarnessManagement:
    def __init__(self) -> None:
        self.specs: dict[tuple[str, str], HarnessLaunchSpec] = {}

    def stage_harness_desired(self, spec: HarnessLaunchSpec) -> bool:
        key = (spec.harness, spec.name)
        previous = self.specs.get(key)
        self.specs[key] = spec
        return previous != spec

    def snapshot_adapter_registration(self, name: str) -> object:
        raise AssertionError(name)

    def remove_adapter_registration(self, snapshot: object) -> bool:
        raise AssertionError(snapshot)

    def restore_adapter_registration(self, snapshot: object) -> bool:
        raise AssertionError(snapshot)


class FakeStarter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def start(self, **configuration: Any) -> dict[str, Any]:
        self.calls.append(configuration)
        return {"ok": True, "changed": True, "id": "pi:squire"}

    def start_spec(self, spec: HarnessLaunchSpec) -> dict[str, Any]:
        return self.start(
            harness=spec.harness,
            name=spec.name,
            cwd=Path(spec.cwd) if spec.cwd is not None else None,
            args=spec.args,
            session_ref=spec.session_ref,
        )


def new_setup(
    tmp_path: Path,
    registry: FakeAgentRegistry,
    *,
    configured_adapters: tuple[str, ...] = (),
    starter: FakeStarter | None = None,
) -> SquireSetup:
    registry.agents_root = tmp_path / "hyprial-home" / "agents"
    harnesses = FakeHarnessManagement()
    management = RegistryManagementHandler(
        harnesses,
        registry,
        start_harness=(None if starter is None else starter.start_spec),
    )
    return SquireSetup(
        hyprial_home=tmp_path / "hyprial-home",
        state_dir=tmp_path / "hyprial-home" / "state",
        management_port=management,
        configured_adapters=lambda: configured_adapters,
        claude_available=lambda: False,
    )


def test_setup_scaffolds_template_and_preserves_every_file_byte_for_byte(
    tmp_path: Path,
) -> None:
    registry = FakeAgentRegistry()
    setup = new_setup(tmp_path, registry, configured_adapters=("allen-squire",))
    home = tmp_path / "personal-squire"
    agent_home = tmp_path / "hyprial-home" / "agents" / "squire"

    first = setup.run(
        IDENTITY,
        squire_home=home,
        display_name="Allen Woods",
        adapter="allen-squire",
        dm_route="owner-dm",
    )

    skill_path = agent_home / "config" / "shared" / "skills" / "squire" / "SKILL.md"
    scaffold = (skill_path, agent_home / "workspace" / "backlog.md", agent_home / "workspace" / "profile.md")
    skill = skill_path.read_text(encoding="utf-8")
    assert "Allen Woods" in skill
    assert "用户名 allen" in skill
    assert "`allen-squire`" in skill
    assert "route:allen-squire:owner-dm" in skill
    for placeholder in (
        "<用户显示名>",
        "<用户名>",
        "<用户>",
        "<owner>",
        "<adapter>",
        "<dm-route>",
    ):
        assert placeholder not in skill
    assert (agent_home / "workspace" / "backlog.md").read_bytes() == b""
    assert (agent_home / "workspace" / "profile.md").read_bytes() == b""
    before = {path: path.read_bytes() for path in scaffold}

    second = setup.run(
        IDENTITY,
        squire_home=home,
        display_name="Allen Woods",
        adapter="allen-squire",
        dm_route="owner-dm",
    )

    assert {path: path.read_bytes() for path in scaffold} == before
    assert second["changed"] == []
    assert first["squire"]["home"] == str(agent_home)


def test_setup_does_not_overwrite_a_custom_skill(tmp_path: Path) -> None:
    registry = FakeAgentRegistry()
    setup = new_setup(tmp_path, registry)
    home = tmp_path / "personal-squire"
    skill_path = home / "skills" / "squire" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_bytes(b"my custom squire rules\n")

    result = setup.run(IDENTITY, squire_home=home, display_name="Allen")

    target = tmp_path / "hyprial-home" / "agents" / "squire" / "config" / "shared" / "skills" / "squire" / "SKILL.md"
    assert target.read_bytes() == b"my custom squire rules\n"
    assert skill_path.read_bytes() == b"my custom squire rules\n"
    assert "scaffold.skill" not in result["changed"]


def test_packaged_skill_template_matches_the_documented_source() -> None:
    root = Path(__file__).resolve().parents[4]
    assert (root / "src" / "hyprial" / "daemon" / "impl" / "squire" / "SKILL-template.md").read_bytes() == (
        root / "docs" / "domains" / "squire" / "SKILL-template.md"
    ).read_bytes()


def test_setup_creates_agent_and_pins_only_once_through_registry_port(
    tmp_path: Path,
) -> None:
    registry = FakeAgentRegistry()
    setup = new_setup(tmp_path, registry, configured_adapters=("allen-squire",))
    home = tmp_path / "personal-squire"
    agent_home = tmp_path / "hyprial-home" / "agents" / "squire"

    first = setup.run(IDENTITY, squire_home=home, adapter="allen-squire")
    second = setup.run(IDENTITY, squire_home=home, adapter="allen-squire")

    assert registry.creates == [
        {
            "actor": "squire",
            "cwd": str(agent_home / "workspace"),
            "config": AgentConfig(str(agent_home / "config")).to_json(),
            "provider": "deepseek",
            "model": "deepseek-flash",
            "preferred_harness": "pi",
        }
    ]
    assert registry.pin_calls == [("allen-squire", "squire")]
    assert "agent" in first["changed"]
    assert "adapter.pin" in first["changed"]
    assert second["changed"] == []


def test_setup_without_adapter_returns_explicit_deferred_onboarding(
    tmp_path: Path,
) -> None:
    setup = new_setup(tmp_path, FakeAgentRegistry())

    result = setup.run(IDENTITY, squire_home=tmp_path / "personal-squire")

    app_step = result["steps"][1]
    assert app_step["status"] == "deferred"
    assert app_step["code"] == "APP_CREATION_DEFERRED"
    assert "hyprial adapter add allen-squire --new" in app_step["detail"]
    assert "hyprial squire setup --adapter allen-squire" in app_step["detail"]


def test_explicit_missing_adapter_rejects_before_any_local_write(
    tmp_path: Path,
) -> None:
    setup = new_setup(tmp_path, FakeAgentRegistry())
    home = tmp_path / "personal-squire"

    try:
        setup.run(IDENTITY, squire_home=home, adapter="missing-squire")
    except ValueError as error:
        assert "hyprial adapter add missing-squire --new" in str(error)
    else:
        raise AssertionError("missing explicit adapter was accepted")

    assert not home.exists()
    assert not (tmp_path / "hyprial-home" / "state" / "desired-state.json.v1").exists()
    assert not (tmp_path / "hyprial-home" / "state" / "users.json").exists()


def test_setup_start_uses_configured_harness_model_provider_and_skill(
    tmp_path: Path,
) -> None:
    registry = FakeAgentRegistry()
    starter = FakeStarter()
    home = tmp_path / "personal-squire"
    agent_home = tmp_path / "hyprial-home" / "agents" / "squire"
    setup = new_setup(tmp_path, registry, starter=starter)

    result = setup.run(
        IDENTITY,
        squire_home=home,
        provider="deepseek",
        model="deepseek-flash",
        preferred_harness="pi",
        start=True,
    )

    assert starter.calls == [
        {
            "harness": "pi",
            "name": "squire",
            "cwd": agent_home / "workspace",
            "args": (
                "--provider",
                "deepseek",
                "--model",
                "deepseek-flash",
                "--skill",
                str(agent_home / "config" / "shared" / "skills" / "squire"),
                "--approve",
            ),
            "session_ref": "squire:owner-key-1:machine-key-1",
        }
    ]
    assert result["worker"]["started"] is True


def test_cli_start_calls_the_worker_boundary_with_the_pi_launch_contract(
    tmp_path: Path, monkeypatch: Any
) -> None:
    hyprial_home = tmp_path / "hyprial-home"
    state_dir = hyprial_home / "state"
    squire_home = tmp_path / "squire"
    agent_home = hyprial_home / "agents" / "squire"
    calls: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        del timeout
        calls.append((method, params or {}))
        assert method == "management.squire.ensure"
        return {
            "ok": True,
            "changed": ["desiredState.providers", "agent"],
            "agentUri": "agent:allen:mac-studio:squire",
            "worker": {
                "started": True,
                "result": {"ok": True, "changed": True, "id": "pi:squire"},
            },
        }

    hyprial_home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(hyprial_home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HYPRIAL_NODE_ID", "mac-studio")
    # squire setup's owner is resolved from the home's identity (U5), not a
    # flag; pin it the way the daemon would resolve it for this home.
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "squire",
            "setup",
            "--owner-key",
            "owner-key-1",
            "--login-name",
            "alicesmith",
            "--machine",
            "mac-studio",
            "--machine-key",
            "machine-key-1",
            "--home",
            str(squire_home),
            "--start",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0][1]["start"] is False
    method, request = calls[1]
    assert method == "management.squire.ensure"
    assert request["owner"] == "allen"
    assert request["machine"] == "mac-studio"
    assert request["start"] is True
    spec = request["spec"]
    assert isinstance(spec, dict)
    assert spec["provider"] == "pi"
    assert spec["name"] == "squire"
    assert spec["args"] == [
        "--provider",
        "deepseek",
        "--model",
        "deepseek-flash",
        "--skill",
        str(agent_home / "config" / "shared" / "skills" / "squire"),
        "--approve",
    ]
    assert spec["sessionRef"] == "squire:owner-key-1:machine-key-1"


def test_cli_start_is_a_noop_when_the_same_squire_worker_is_online(
    tmp_path: Path, monkeypatch: Any
) -> None:
    hyprial_home = tmp_path / "hyprial-home"
    state_dir = hyprial_home / "state"
    squire_home = tmp_path / "squire"
    calls: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        del timeout
        calls.append((method, params or {}))
        assert method == "management.squire.ensure"
        return {
            "ok": True,
            "changed": [],
            "agentUri": "agent:allen:mac-studio:squire",
            "worker": {
                "started": True,
                "result": {
                    "ok": True,
                    "changed": False,
                    "id": "pi:squire",
                    "actor": "agent:allen:mac-studio:squire",
                    "alreadyRunning": True,
                },
            },
        }

    hyprial_home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(hyprial_home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HYPRIAL_NODE_ID", "mac-studio")
    # squire setup's owner is resolved from the home's identity (U5), not a
    # flag; pin it the way the daemon would resolve it for this home.
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "squire",
            "setup",
            "--owner-key",
            "owner-key-1",
            "--login-name",
            "alicesmith",
            "--machine",
            "mac-studio",
            "--machine-key",
            "machine-key-1",
            "--home",
            str(squire_home),
            "--start",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["worker"] == {
        "started": True,
        "result": {
            "ok": True,
            "changed": False,
            "id": "pi:squire",
            "actor": "agent:allen:mac-studio:squire",
            "alreadyRunning": True,
        },
    }
    assert len(calls) == 2
    assert calls[0][1]["start"] is False
    assert calls[1][1]["start"] is True
    assert calls[0][0] == "management.squire.ensure"


def test_squire_identity_mismatch_names_both_sides() -> None:
    """⭐ spec item 6③: the refusal must state BOTH identities.

    The check compares owner *and* machine and stays that way.  What changed
    is which segment a mismatch usually means, and therefore what the message
    has to say.

    Owner used to come from the host login, so two machines disagreed simply
    by being two machines and the differing segment was almost always the
    owner.  Owner is a user identity now — one value across every machine a
    user owns — so a mismatch is almost always ``<machine>`` instead.  A
    message naming neither side leaves the reader unable to tell which one
    moved, and the likely answer changed underneath them.
    """

    from hyprial.daemon.impl.operations.management import EnsureSquireRegistryCommand, ManagementError

    registry = FakeAgentRegistry()
    management = RegistryManagementHandler(FakeHarnessManagement(), registry)
    command = EnsureSquireRegistryCommand(
        owner=registry.owner,
        # Same owner (single identity), different machine — the shape a
        # mismatch now actually takes.
        machine="some-other-host",
        spec=HarnessLaunchSpec("claude", "squire", True),
        cwd="/tmp",
        model_provider="anthropic",
        model="sonnet",
        preferred_harness="claude",
    )

    with pytest.raises(ManagementError) as refusal:
        management.ensure_squire(command)

    message = str(refusal.value)
    # Both sides, both segments — four facts, so the reader never has to guess
    # which half of the comparison moved.
    assert "some-other-host" in message
    assert registry.machine in message
    assert registry.owner in message
    assert "requested" in message and "authority" in message
