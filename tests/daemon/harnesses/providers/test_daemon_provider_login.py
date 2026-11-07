"""Daemon-level tests for ``agent.provider.login``.

The identity writer is unit-tested where it lives; these tests drive the real
combination application (``DaemonApplication`` plus ``agent.create``) so the
assertions cover the seam the CLI really uses: actor name -> registry home
receipt -> canonical ``secrets/native/<harness>/`` tree.  A stub that answered
the home path itself would prove nothing about the authority the handler
consumes.
"""

from __future__ import annotations

import base64
import stat
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _daemon_dependencies,
)
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services

PAYLOAD = b'{"claudeAiOauth": {"accessToken": "test-only"}}\n'
ACTOR = "worker"


class _Harnesses:
    @staticmethod
    def status() -> tuple[object, ...]:
        return ()


class _Presence:
    @staticmethod
    def actor_online(_actor: str) -> bool:
        return False

    @staticmethod
    def online_actors() -> tuple[str, ...]:
        return ()

    @staticmethod
    def online_mailboxes() -> tuple[str, ...]:
        return ()


@pytest.fixture
def provider_app(tmp_path: Path) -> DaemonApplication:
    """A real application with one provisioned agent home and nothing else."""

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state = tmp_path / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="owner",
        hyprial_home=home,
        **_daemon_dependencies(),
    )
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]

    config = tmp_path / "personality"
    (config / "native" / "codex").mkdir(parents=True)
    (config / "native" / "codex" / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n', encoding="utf-8"
    )
    app.handle(
        "agent.create",
        {
            "name": ACTOR,
            "cwd": str(tmp_path / ACTOR),
            "config": {
                "sources": [{"path": str(config), "required": True}],
                "discovery": "explicit-only",
            },
        },
    )
    return app


def _agent_home(app: DaemonApplication) -> Path:
    """Resolve the actor's own home through the registry, not through a stub."""

    agent = app.agents.require(app.agents.native_actor(ACTOR))
    receipt = app._agent_registry.home_receipt(agent.actor, require_ready=False)
    return Path(receipt.path)


def _target(app: DaemonApplication, provider: str, filename: str) -> Path:
    return _agent_home(app) / "secrets" / "native" / provider / filename


def _files(*documents: tuple[str, bytes]) -> list[dict[str, str]]:
    return [
        {"filename": name, "contentBase64": base64.b64encode(body).decode("ascii")}
        for name, body in documents
    ]


def _login(app: DaemonApplication, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "actor": ACTOR,
        "provider": "claude",
        "files": _files((".credentials.json", PAYLOAD)),
        "force": False,
    }
    params.update(overrides)
    return app.handle("agent.provider.login", params)


def test_success_writes_the_canonical_private_file(
    provider_app: DaemonApplication,
) -> None:
    result = _login(provider_app)

    target = _target(provider_app, "claude", ".credentials.json")
    assert result["ok"] is True
    assert result["provider"] == "claude"
    assert result["actor"] == ACTOR
    [written] = result["files"]
    assert written["path"] == str(target)
    assert written["bytes"] == len(PAYLOAD)
    assert written["replaced"] is False
    assert target.read_bytes() == PAYLOAD
    metadata = target.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert stat.S_IMODE(target.parent.lstat().st_mode) == 0o700


def test_unknown_provider_is_refused_and_writes_nothing(
    provider_app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _login(
            provider_app,
            provider="not-a-harness",
            files=_files(("auth.json", PAYLOAD)),
        )

    assert refused.value.code == ipc_errors.PROVIDER_UNSUPPORTED
    assert not (_agent_home(provider_app) / "secrets" / "native").exists()


def test_existing_credential_is_refused_without_force(
    provider_app: DaemonApplication,
) -> None:
    _login(provider_app)

    with pytest.raises(DaemonRequestError) as refused:
        _login(provider_app, files=_files((".credentials.json", b"second")))

    assert refused.value.code == ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    target = _target(provider_app, "claude", ".credentials.json")
    assert target.read_bytes() == PAYLOAD

    replaced = _login(
        provider_app,
        files=_files((".credentials.json", b"second")),
        force=True,
    )
    assert replaced["files"][0]["replaced"] is True
    assert target.read_bytes() == b"second"
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600


def test_invalid_base64_is_refused_before_any_write(
    provider_app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _login(
            provider_app,
            files=[{"filename": ".credentials.json", "contentBase64": "not base64!!"}],
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert not (_agent_home(provider_app) / "secrets" / "native").exists()


def test_unknown_actor_keeps_the_registry_refusal(
    provider_app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        _login(provider_app, actor="ghost")

    # The handler delegates to ``agents.require``; the registry's existing
    # typed refusal (``AgentNotFoundError``) crosses the IPC boundary as this
    # code, unchanged.
    assert refused.value.code == ipc_errors.AGENT_NOT_FOUND
    assert not (_agent_home(provider_app) / "secrets" / "native").exists()


def test_pi_is_seeded_as_one_set_or_not_at_all(
    provider_app: DaemonApplication,
) -> None:
    root = _agent_home(provider_app) / "secrets" / "native" / "pi"
    root.mkdir(parents=True, mode=0o700)
    for level in (root.parent.parent, root.parent):
        level.chmod(0o700)
    (root / "models-store.json").write_bytes(b"older store")
    (root / "models-store.json").chmod(0o600)

    with pytest.raises(DaemonRequestError) as refused:
        _login(
            provider_app,
            provider="pi",
            files=_files(("auth.json", b'{"auth": 1}'), ("models-store.json", b"new")),
        )

    assert refused.value.code == ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    assert not (root / "auth.json").exists()
    assert (root / "models-store.json").read_bytes() == b"older store"


def test_files_must_be_a_non_empty_list(provider_app: DaemonApplication) -> None:
    for files in (None, [], "auth.json", [{"filename": ".credentials.json"}]):
        with pytest.raises(DaemonRequestError) as refused:
            _login(provider_app, files=files)
        assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert not (_agent_home(provider_app) / "secrets" / "native").exists()


def test_logout_removes_the_seeded_files(provider_app: DaemonApplication) -> None:
    _login(provider_app)
    target = _target(provider_app, "claude", ".credentials.json")

    result = provider_app.handle(
        "agent.provider.logout", {"actor": ACTOR, "provider": "claude"}
    )

    assert result["ok"] is True
    assert result["actor"] == ACTOR
    assert result["removed"] == [str(target)]
    assert not target.exists()


def test_logout_of_an_unknown_actor_keeps_the_registry_refusal(
    provider_app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        provider_app.handle(
            "agent.provider.logout", {"actor": "ghost", "provider": "claude"}
        )

    assert refused.value.code == ipc_errors.AGENT_NOT_FOUND


def test_the_default_is_unset_then_set_then_replaced_then_cleared(
    provider_app: DaemonApplication,
) -> None:
    import json

    store = provider_app.state_dir / "provider-default.json"
    assert provider_app.handle("agent.provider.default", {}) == {
        "ok": True,
        "provider": None,
        "previous": None,
    }

    first = provider_app.handle("agent.provider.default", {"provider": "claude"})
    assert (first["provider"], first["previous"]) == ("claude", None)
    assert json.loads(store.read_text()) == {"provider": "claude", "schemaVersion": 1}
    assert stat.S_IMODE(store.lstat().st_mode) == 0o600

    second = provider_app.handle("agent.provider.default", {"provider": "codex"})
    assert (second["provider"], second["previous"]) == ("codex", "claude")
    assert provider_app.handle("agent.provider.default", {})["provider"] == "codex"

    cleared = provider_app.handle("agent.provider.default", {"clear": True})
    assert (cleared["provider"], cleared["previous"]) == (None, "codex")
    assert not store.exists()


def test_the_default_refuses_an_unknown_provider(
    provider_app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        provider_app.handle("agent.provider.default", {"provider": "not-a-harness"})

    assert refused.value.code == ipc_errors.PROVIDER_UNSUPPORTED
    assert not (provider_app.state_dir / "provider-default.json").exists()


def test_the_default_does_not_seed_an_existing_agent(
    provider_app: DaemonApplication,
) -> None:
    provider_app.handle("agent.provider.default", {"provider": "claude"})

    assert not (_agent_home(provider_app) / "secrets" / "native").exists()


def test_agent_create_reports_the_default_without_seeding(
    provider_app: DaemonApplication, tmp_path: Path
) -> None:
    before = provider_app.handle(
        "agent.create", {"name": "early", "cwd": str(tmp_path / "early")}
    )
    provider_app.handle("agent.provider.default", {"provider": "claude"})
    after = provider_app.handle(
        "agent.create", {"name": "late", "cwd": str(tmp_path / "late")}
    )

    assert before["providerDefault"] is None
    assert after["providerDefault"] == "claude"
    late = provider_app.agents.require(provider_app.agents.native_actor("late"))
    late_home = Path(
        provider_app._agent_registry.home_receipt(late.actor, require_ready=False).path
    )
    assert not (late_home / "secrets" / "native" / "claude").exists()


def test_cli_to_daemon_login_create_and_logout_round_trip(
    provider_app: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    operator = tmp_path / "operator-home"
    (operator / ".claude").mkdir(parents=True)
    (operator / ".claude" / ".credentials.json").write_bytes(PAYLOAD)
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)

    def request(method: str, params: object = None, **_kwargs: object) -> object:
        try:
            return provider_app.handle(method, params or {})
        except DaemonRequestError as error:
            raise _cli_support.CliError(error.code, str(error)) from error

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()

    seeded = runner.invoke(
        cli.app,
        ["agent", "provider", "login", "claude", "--actor", ACTOR, "--default", "--json"],
    )
    assert seeded.exit_code == 0, seeded.output
    assert _target(provider_app, "claude", ".credentials.json").read_bytes() == PAYLOAD
    default = json.loads(seeded.stdout)["default"]
    assert default["provider"] == "claude" and default["previous"] is None
    assert default["credentialSource"] == "native"
    assert default["defaultCredential"]["ownerActor"] == ACTOR
    # Creation must use the captured owner, not re-read the operator login.
    (operator / ".claude" / ".credentials.json").unlink()

    created = runner.invoke(
        cli.app,
        ["agent", "create", "--name", "fresh", "--preferred-harness", "claude", "--cwd", str(tmp_path / "fresh"), "--json"],
    )
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["providerSeed"]["state"] == "seeded"
    fresh = provider_app.agents.require(provider_app.agents.native_actor("fresh"))
    fresh_home = Path(
        provider_app._agent_registry.home_receipt(fresh.actor, require_ready=False).path
    )
    fresh_file = fresh_home / "secrets" / "native" / "claude" / ".credentials.json"
    assert fresh_file.read_bytes() == PAYLOAD
    assert stat.S_IMODE(fresh_file.lstat().st_mode) == 0o600

    revoked = runner.invoke(
        cli.app, ["agent", "provider", "logout", "claude", "--actor", "fresh", "--json"]
    )
    assert revoked.exit_code == 0, revoked.output
    assert not fresh_file.exists()
    assert _target(provider_app, "claude", ".credentials.json").exists()
    for output in (seeded, created, revoked):
        assert "test-only" not in output.stdout + output.stderr


def test_a_worker_caller_cannot_change_the_machine_default(
    provider_app: DaemonApplication,
) -> None:
    for params in ({"provider": "claude"}, {"clear": True}):
        with pytest.raises(DaemonRequestError) as refused:
            provider_app.handle(
                "agent.provider.default", params, _trusted_message_origin="worker"
            )
        assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert not (provider_app.state_dir / "provider-default.json").exists()
    read = provider_app.handle(
        "agent.provider.default", {}, _trusted_message_origin="worker"
    )
    assert read["provider"] is None


def test_a_hosted_visitor_is_not_seeded_with_this_users_login(
    provider_app: DaemonApplication, tmp_path: Path
) -> None:
    provider_app.handle(
        "agent.host-invite",
        {"name": "guest", "owner": "visitor", "cwd": str(tmp_path / "guest")},
    )

    with pytest.raises(DaemonRequestError) as refused:
        _login(provider_app, actor="guest")

    assert refused.value.code == ipc_errors.TARGET_NOT_AUTHORIZED
    guest = provider_app.agents.require(provider_app.agents.native_actor("guest"))
    guest_home = Path(
        provider_app._agent_registry.home_receipt(guest.actor, require_ready=False).path
    )
    assert not (guest_home / "secrets" / "native").exists()


def test_unowned_provider_methods_are_left_to_the_agent_handler(
    provider_app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    def owned_only(method: str, *_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"{method} is not owned by the provider-login mixin")

    monkeypatch.setattr(provider_app, "_handle_agent_provider", owned_only)

    with pytest.raises(DaemonRequestError) as refused:
        provider_app.handle("agent.provider.inspect", {"name": ACTOR})

    assert refused.value.code == ipc_errors.METHOD_NOT_FOUND


def test_logout_refuses_a_hosted_visitor_like_login(
    provider_app: DaemonApplication, tmp_path: Path
) -> None:
    provider_app.handle(
        "agent.host-invite",
        {"name": "guest", "owner": "visitor", "cwd": str(tmp_path / "guest")},
    )

    with pytest.raises(DaemonRequestError) as refused:
        provider_app.handle(
            "agent.provider.logout", {"actor": "guest", "provider": "claude"}
        )

    assert refused.value.code == ipc_errors.TARGET_NOT_AUTHORIZED
