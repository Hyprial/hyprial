"""P1a T06/T11: explicit secret grants and complete env value types."""

from __future__ import annotations

import os
import time
import threading
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import (
    AgentRegistry,
    SecretCustody,
    SecretResolver,
    SecretResolutionError,
    SecretSource,
    build_complete_child_environment,
)
from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.identity.impl.agents.actor.ports import AgentSecretGrantCompleted, GrantAgentSecretCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "process-home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "process-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


@pytest.fixture
def custody(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=home,
    )
    registry.create("alpha")
    registry.create("beta")
    try:
        yield registry, SecretResolver(home, registry)
    finally:
        registry.close()


def test_t06_catalog_keeps_login_adapter_user_provider_and_agent_custody_distinct(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    _registry, resolver = custody
    login = resolver.catalog_entry(SecretCustody.USER_LOGIN, "login")
    adapter = resolver.catalog_entry(SecretCustody.ADAPTER, "work")
    provider = resolver.catalog_entry(SecretCustody.USER_PROVIDER, "deepseek")
    private = resolver.catalog_entry(
        SecretCustody.AGENT_PRIVATE, "tool-forge", actor="alpha"
    )

    assert login.path == resolver.hyprial_home / "secrets" / "login.json"
    assert adapter.path == resolver.hyprial_home / "secrets" / "lark-work.json"
    assert provider.path == (
        resolver.hyprial_home / "secrets" / "providers" / "deepseek.json"
    )
    assert private.path == (
        resolver.hyprial_home / "agents" / "alpha" / "secrets.sqlite"
    )
    assert not login.distributable
    assert not adapter.distributable
    assert provider.distributable
    assert private.distributable
    assert {source.value for source in SecretSource} == {
        "user-provider",
        "agent-private",
        "agent-shared",
    }


def test_t06_resolves_only_the_named_entry_granted_to_one_incarnation(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    registry, resolver = custody
    resolver.write_user_provider("deepseek", {"apiKey": "shared-sentinel"})
    registry.grant_secret(
        "alpha",
        grant_id="deepseek",
        source=SecretSource.USER_PROVIDER,
        entry_id="deepseek",
        field_name="apiKey",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )

    resolved = resolver.resolve("alpha", "deepseek")
    assert resolved.value == "shared-sentinel"
    assert resolved.environment() == {"DEEPSEEK_API_KEY": "shared-sentinel"}
    assert "shared-sentinel" not in repr(resolved)
    with pytest.raises(SecretResolutionError) as denied:
        resolver.resolve("beta", "deepseek")
    assert denied.value.category == "grant-missing"

    inventory = registry.secret_inventory()
    assert inventory == (resolved.grant,)
    assert "shared-sentinel" not in repr(inventory)


def test_t06_agent_private_entry_stays_inside_the_granted_agent_home(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    registry, resolver = custody
    # Legacy file fixture: product writes now go only through AgentSecretStore.
    path = Path(registry.home_receipt("alpha").path) / "secrets" / "provider-openai.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text('{"apiKey":"alpha-sentinel"}', encoding="utf-8")
    path.chmod(0o600)
    registry.grant_secret(
        "alpha",
        grant_id="openai",
        source=SecretSource.AGENT_PRIVATE,
        entry_id="provider-openai.json",
        field_name="apiKey",
        environment_names=("OPENAI_API_KEY",),
        revision=1,
    )

    assert resolver.resolve("alpha", "openai").value == "alpha-sentinel"
    with pytest.raises(SecretResolutionError):
        resolver.resolve("beta", "openai")
    assert not (Path(registry.home_receipt("beta").path) / "secrets" / "provider-openai.json").exists()


def test_private_grant_uses_prevalidated_home_receipt_off_actor(
    custody: tuple[AgentRegistry, SecretResolver],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _resolver = custody
    threads: list[str] = []
    original_home_read = registry.home_receipt

    def observed_home_read(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return original_home_read(*args, **kwargs)

    monkeypatch.setattr(registry, "home_receipt", observed_home_read)
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append)
    receipt = actor.prevalidate_home("alpha")
    assert threads and threads[0] != threading.current_thread().name

    def forbidden_home_read(*_args, **_kwargs):
        raise AssertionError("Agent mailbox performed filesystem home validation")

    monkeypatch.setattr(registry, "home_receipt", forbidden_home_read)
    try:
        assert actor.submit(GrantAgentSecretCommand(
            "private-metadata", registry.uri_for("alpha"), "grant-private",
            SecretSource.AGENT_PRIVATE, "synthetic-entry", None,
            ("OPENAI_API_KEY",), 1, receipt.entity_token,
        )) is PortAdmission.ACCEPTED
        deadline = time.monotonic() + 3.0
        while not any(
            isinstance(event, AgentSecretGrantCompleted)
            and event.correlation_id == "private-metadata"
            for event in events
        ):
            assert time.monotonic() < deadline, events
            time.sleep(0.01)
        assert registry.secret_grant("alpha", "grant-private") is not None
    finally:
        assert actor.drain(1.0).complete


def test_t06_complete_environment_uses_only_explicit_inputs_not_parent_env(
    custody: tuple[AgentRegistry, SecretResolver],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, resolver = custody
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-wrong-agent")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-cloud")
    resolver.write_user_provider("deepseek", {"apiKey": "granted-sentinel"})
    registry.grant_secret(
        "alpha",
        grant_id="deepseek",
        source=SecretSource.USER_PROVIDER,
        entry_id="deepseek",
        field_name="apiKey",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )

    environment = build_complete_child_environment(
        base={"PATH": "/approved/bin", "HOME": "/real/home"},
        generated={
            "HYPRIAL_HOME": "/isolated/hyprial",
            "HYPRIAL_AGENT_DIR": "/isolated/hyprial/agents/alpha",
        },
        secrets=(resolver.resolve("alpha", "deepseek"),),
    )

    assert environment.for_exec() == {
        "DEEPSEEK_API_KEY": "granted-sentinel",
        "HOME": "/real/home",
        "HYPRIAL_AGENT_DIR": "/isolated/hyprial/agents/alpha",
        "HYPRIAL_HOME": "/isolated/hyprial",
        "PATH": "/approved/bin",
    }
    assert "OPENAI_API_KEY" not in environment.names
    assert "AWS_ACCESS_KEY_ID" not in environment.names
    assert "granted-sentinel" not in repr(environment)
    assert os.environ["OPENAI_API_KEY"] == "ambient-wrong-agent"


def test_t06_typesafe_api_key_is_a_valid_secret_environment_name(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    """TYPESAFE_API_KEY remains deliverable for the jev harness."""

    registry, resolver = custody
    registry.create("jev")
    resolver.write_user_provider("typesafe", {"key": "ts-sentinel"})
    registry.grant_secret(
        "jev",
        grant_id="typesafe",
        source=SecretSource.USER_PROVIDER,
        entry_id="typesafe",
        field_name="key",
        environment_names=("TYPESAFE_API_KEY",),
        revision=1,
    )

    resolved = resolver.resolve("jev", "typesafe")
    assert resolved.environment() == {"TYPESAFE_API_KEY": "ts-sentinel"}


def test_t06_one_claude_oauth_entry_is_granted_to_two_agents(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    registry, resolver = custody
    token = "fake-shared-claude-oauth-token"
    path = resolver.write_user_provider("claude-shared", {"token": token})

    for actor in ("alpha", "beta"):
        registry.grant_secret(
            actor,
            grant_id="claude-shared",
            source=SecretSource.USER_PROVIDER,
            entry_id="claude-shared",
            field_name="token",
            environment_names=("CLAUDE_CODE_OAUTH_TOKEN",),
            revision=1,
        )

    assert path == (
        resolver.hyprial_home
        / "secrets"
        / "providers"
        / "claude-shared.json"
    )
    assert tuple(path.parent.iterdir()) == (path,)
    resolved = tuple(
        resolver.resolve(actor, "claude-shared") for actor in ("alpha", "beta")
    )
    assert [secret.environment() for secret in resolved] == [
        {"CLAUDE_CODE_OAUTH_TOKEN": token},
        {"CLAUDE_CODE_OAUTH_TOKEN": token},
    ]
    for secret in resolved:
        environment = build_complete_child_environment(
            base={"PATH": "/approved/bin"},
            generated={},
            secrets=(secret,),
        )
        assert environment.for_exec()["CLAUDE_CODE_OAUTH_TOKEN"] == token
        assert token not in repr(environment)
        assert token not in repr(secret)


def test_t06_zai_coding_cn_api_key_is_a_valid_secret_environment_name(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    registry, resolver = custody
    registry.create("pi-worker")
    resolver.write_user_provider("zai-coding-cn", {"key": "fake-zai-key"})
    registry.grant_secret(
        "pi-worker",
        grant_id="zai-coding-cn",
        source=SecretSource.USER_PROVIDER,
        entry_id="zai-coding-cn",
        field_name="key",
        environment_names=("ZAI_CODING_CN_API_KEY",),
        revision=1,
    )

    resolved = resolver.resolve("pi-worker", "zai-coding-cn")
    assert resolved.environment() == {"ZAI_CODING_CN_API_KEY": "fake-zai-key"}


def test_t06_dead_claude_oauth_grant_names_its_replacement(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    registry, _resolver = custody
    value = "fake-dead-oauth-token"

    with pytest.raises(ValueError, match="CLAUDE_CODE_OAUTH_TOKEN") as error:
        registry.grant_secret(
            "alpha",
            grant_id="dead-claude-oauth",
            source=SecretSource.USER_PROVIDER,
            entry_id="dead-claude-oauth",
            field_name="token",
            environment_names=("ANTHROPIC_OAUTH_TOKEN",),
            revision=1,
        )
    assert value not in str(error.value)


def test_t06_an_invalid_environment_name_is_still_rejected(
    custody: tuple[AgentRegistry, SecretResolver],
) -> None:
    """Negative control: accepting new providers does not loosen env syntax."""

    registry, _resolver = custody
    with pytest.raises(ValueError, match="valid env names"):
        registry.grant_secret(
            "alpha",
            grant_id="rogue",
            source=SecretSource.USER_PROVIDER,
            entry_id="rogue",
            field_name=None,
            environment_names=("some-unapproved-key",),
            revision=1,
        )


def test_t06_environment_rejects_unapproved_and_partial_git_mappings() -> None:
    with pytest.raises(ValueError):
        build_complete_child_environment(
            base={"PYTHONPATH": "/ambient"}, generated={}
        )
    with pytest.raises(ValueError):
        build_complete_child_environment(
            base={"GIT_CONFIG_COUNT": "1"}, generated={}
        )
    complete = build_complete_child_environment(
        base={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.ssh://git.internal/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://code.hyprial.com/",
            "SSH_AUTH_SOCK": "/approved/agent.sock",
        },
        generated={},
    )
    assert complete.names == {
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "SSH_AUTH_SOCK",
    }


def test_t11_missing_invalid_symlink_and_mode_are_distinct(
    custody: tuple[AgentRegistry, SecretResolver],
    tmp_path: Path,
) -> None:
    registry, resolver = custody
    registry.grant_secret(
        "alpha",
        grant_id="probe",
        source=SecretSource.USER_PROVIDER,
        entry_id="probe",
        field_name="apiKey",
        environment_names=("OPENAI_API_KEY",),
        revision=1,
    )
    with pytest.raises(SecretResolutionError) as missing:
        resolver.resolve("alpha", "probe")
    assert (missing.value.category, missing.value.entry_id, missing.value.phase) == (
        "missing",
        "probe",
        "ancestor",
    )

    path = resolver.write_user_provider("probe", {"wrong": "not-selected"})
    with pytest.raises(SecretResolutionError) as invalid:
        resolver.resolve("alpha", "probe")
    assert invalid.value.category == "invalid"
    assert "not-selected" not in str(invalid.value)

    path.unlink()
    target = tmp_path / "target"
    target.write_text('{"apiKey":"linked"}')
    path.symlink_to(target)
    with pytest.raises(SecretResolutionError) as linked:
        resolver.resolve("alpha", "probe")
    assert linked.value.category == "unsafe-type"

    path.unlink()
    path = resolver.write_user_provider("probe", {"apiKey": "mode-sentinel"})
    path.chmod(0o640)
    with pytest.raises(SecretResolutionError) as mode:
        resolver.resolve("alpha", "probe")
    assert mode.value.category == "unsafe-mode"
    assert "mode-sentinel" not in str(mode.value)


def test_t11_permission_and_io_failures_are_not_folded_into_missing(
    custody: tuple[AgentRegistry, SecretResolver],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, resolver = custody
    path = resolver.write_user_provider("probe", {"apiKey": "never-in-error"})
    registry.grant_secret(
        "alpha",
        grant_id="probe",
        source=SecretSource.USER_PROVIDER,
        entry_id="probe",
        field_name="apiKey",
        environment_names=("OPENAI_API_KEY",),
        revision=1,
    )
    original = Path.read_text

    def fail_permission(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError("private operating-system detail")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_permission)
    with pytest.raises(SecretResolutionError) as permission:
        resolver.resolve("alpha", "probe")
    assert permission.value.category == "permission"
    assert str(permission.value) == (
        "secret resolution permission: entry='probe' phase='read'"
    )

    def fail_io(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise OSError("private operating-system detail")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_io)
    with pytest.raises(SecretResolutionError) as io_failure:
        resolver.resolve("alpha", "probe")
    assert io_failure.value.category == "io"
    assert str(io_failure.value) == "secret resolution io: entry='probe' phase='read'"
    assert "never-in-error" not in str(io_failure.value)
