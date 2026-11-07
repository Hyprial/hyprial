"""Granted secrets remain confined to one interactive child launch."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.harnesses.model_provider import (
    AgentProviderCredentialMissing,
)
from hyprial.identity import AgentSecretStore
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


CANARY = "HYPRIAL-CANARY-c81e32a1-430a-42d4-a097-41b1c4d1a6be"
SESSION = "11111111-2222-3333-4444-555555555555"


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


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    calls: list[str],
    launch_name: str = "worker",
    old_daemon: bool = False,
) -> DaemonApplication:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(_cli_get_services(), "uuid4", lambda: uuid.UUID(SESSION))
    monkeypatch.setattr(_cli_get_services(), "_create_agent_for_start", lambda **_kwargs: {})
    monkeypatch.setattr(
        "hyprial.daemon.impl.mcp.channel.ownership._read_process_identity",
        lambda _pid: None,
    )
    app = DaemonApplication(
        state_dir=state,
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="op",
        hyprial_home=home,
        **_deep_module_daemon_dependencies(),
    )
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    personality = tmp_path / "personality"
    (personality / "native" / "claude").mkdir(parents=True)
    for actor_name in ("worker", "empty"):
        actor_cwd = tmp_path / "workspaces" / actor_name
        app.handle(
            "agent.create",
            {
                "name": actor_name,
                "cwd": str(actor_cwd),
                "config": {
                    "sources": [{"path": str(personality), "required": True}],
                    "discovery": "explicit-only",
                },
            },
        )
    app.handle(
        "agent.secret.provider-write",
        {"entryId": "deepseek", "fieldName": "apiKey", "value": CANARY},
    )
    app.handle(
        "agent.secret.grant",
        {
            "actor": "worker",
            "grantId": "deepseek",
            "source": "user-provider",
            "entryId": "deepseek",
            "fieldName": "apiKey",
            "environmentNames": ["DEEPSEEK_API_KEY"],
            "revision": 1,
        },
    )
    AgentSecretStore(Path(app._agent_registry.home_receipt("worker").path)).set(
        service="deepseek",
        field="DEEPSEEK_API_KEY",
        value=CANARY,
        actor="user:op",
        via="cli",
        at_ms=1,
    )
    runtime_context = app.handle(
        "agent.runtime-context",
        {
            "name": launch_name,
            "harness": "claude",
            "cwd": str(tmp_path / "workspaces" / launch_name),
        },
    )
    monkeypatch.setattr(
        _cli_get_services(), "_runtime_context_environment", lambda **_kwargs: runtime_context
    )

    def request(
        method: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        calls.append(method)
        if method in {"agent.runtime-launch.acquire", "agent.runtime-launch.release"}:
            result = app.handle(method, params or {})
            if method == "agent.runtime-launch.acquire" and old_daemon:
                result.pop("secretEnvironment", None)
                result.pop("secretGrants", None)
            return result
        if method == "ps":
            return {
                "daemon": {"nodeId": "node", "owner": "op"},
                "interactiveSessions": [
                    {
                        "actor": f"agent:op:node:{launch_name}",
                        "sessionRef": SESSION,
                        "channelConfirmed": True,
                    }
                ],
            }
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return app


@pytest.mark.parametrize(
    "old_daemon", [False, True], ids=["empty-map", "missing-field"]
)
def test_r2_no_grant_refuses_interactive_claude_with_named_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old_daemon: bool
) -> None:
    calls: list[str] = []
    _prepare(
        monkeypatch,
        tmp_path,
        calls=calls,
        launch_name="empty",
        old_daemon=old_daemon,
    )
    monkeypatch.setattr(
        _cli_subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("credential refusal must precede spawn"),
    )

    with pytest.raises(
        AgentProviderCredentialMissing,
        match="requires an agent-owned DEEPSEEK_API_KEY",
    ):
        _cli_get_services()._start_interactive_claude(
            name="empty",
            nickname=None,
            cwd=tmp_path,
            resume=None,
            runtime_args=(),
            model_provider="deepseek",
            model="deepseek-flash",
            json_output=True,
        )

    assert calls.count("agent.runtime-launch.acquire") == 1
    assert calls.count("agent.runtime-launch.release") == 1
    assert CANARY not in os.environ.values()
    source = tmp_path / "home" / "secrets" / "providers" / "deepseek.json"
    sqlite_store = tmp_path / "home" / "agents" / "worker" / "secrets.sqlite"
    contaminated = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
        and path not in {source, sqlite_store}
        and CANARY.encode() in path.read_bytes()
    ]
    assert contaminated == []


@pytest.mark.parametrize("tmux", [False, True], ids=["popen", "tmux"])
def test_t5_secret_exists_only_in_spawn_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmux: bool
) -> None:
    calls: list[str] = []
    _prepare(monkeypatch, tmp_path, calls=calls)
    observed: dict[str, object] = {}
    ambient = dict(os.environ)

    if tmux:
        from hyprial.daemon import harnesses_tmux as tmux_mod

        monkeypatch.setattr(tmux_mod, "find_tmux", lambda: "/test/bin/tmux")
        monkeypatch.setattr(tmux_mod, "has_session", lambda *_args: False)
        monkeypatch.setattr(
            tmux_mod,
            "attach_hints",
            lambda _name: {"wsl-linux-terminal": "attach", "macOS-iTerm2": "attach"},
        )

        def new_session(
            _tmux: str,
            _name: str,
            argv: list[str],
            *,
            env: dict[str, str],
            **_kwargs: object,
        ) -> None:
            assert dict(os.environ) == ambient
            observed.update(argv=list(argv), env=dict(env))

        monkeypatch.setattr(tmux_mod, "new_detached_session", new_session)
    else:

        class Process:
            @classmethod
            def __class_getitem__(cls, _item: object) -> type[Process]:
                return cls

            def __init__(self, argv: list[str], **kwargs: object) -> None:
                assert dict(os.environ) == ambient
                observed.update(argv=list(argv), env=dict(kwargs["env"]))  # type: ignore[arg-type]
                self.returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                assert timeout is None
                self.returncode = 0
                return 0

        monkeypatch.setattr(_cli_subprocess, "Popen", Process)

    result = _cli_get_services()._start_interactive_claude(
        name="worker",
        nickname=None,
        cwd=tmp_path,
        resume=None,
        runtime_args=(),
        model_provider="deepseek",
        model="deepseek-flash",
        json_output=True,
        tmux=tmux,
    )

    environment = observed["env"]
    assert isinstance(environment, dict)
    assert environment["DEEPSEEK_API_KEY"] == CANARY
    assert environment["ANTHROPIC_AUTH_TOKEN"] == CANARY
    assert all(CANARY not in item for item in observed["argv"])  # type: ignore[union-attr]
    assert dict(os.environ) == ambient
    assert result["ok"] is True
    assert calls.count("agent.runtime-launch.acquire") == 1
    assert calls.count("agent.runtime-launch.release") == 1


@pytest.mark.parametrize("tmux", [False, True], ids=["popen", "tmux"])
def test_t6_real_child_receives_grant_and_provider_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmux: bool
) -> None:
    calls: list[str] = []
    _prepare(monkeypatch, tmp_path, calls=calls)
    output = tmp_path / ("tmux.env" if tmux else "popen.env")
    stub = tmp_path / "claude-stub"
    stub.write_text(
        f"#!/bin/sh\numask 077\nenv > {shlex.quote(str(output))}\nsleep 0.2\n",
        encoding="utf-8",
    )
    stub.chmod(0o700)
    monkeypatch.setenv("HARNESS_CLAUDE_BIN", str(stub))
    if tmux:
        from hyprial.daemon import harnesses_tmux as tmux_mod

        monkeypatch.setattr(tmux_mod, "find_tmux", lambda: "/test/bin/tmux")
        monkeypatch.setattr(tmux_mod, "has_session", lambda *_args: False)
        monkeypatch.setattr(
            tmux_mod,
            "attach_hints",
            lambda _name: {"wsl-linux-terminal": "attach", "macOS-iTerm2": "attach"},
        )

        def real_child(
            _tmux: str,
            _name: str,
            argv: list[str],
            *,
            cwd: Path,
            env: dict[str, str],
            **_kwargs: object,
        ) -> None:
            subprocess.run(argv, cwd=cwd, env=env, check=True)

        monkeypatch.setattr(tmux_mod, "new_detached_session", real_child)

    _cli_get_services()._start_interactive_claude(
        name="worker",
        nickname=None,
        cwd=tmp_path,
        resume=None,
        runtime_args=(),
        model_provider="deepseek",
        model="deepseek-flash",
        json_output=True,
        tmux=tmux,
    )

    deadline = time.monotonic() + 3.0
    while not output.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    child_environment = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert child_environment["DEEPSEEK_API_KEY"] == CANARY
    assert child_environment["ANTHROPIC_AUTH_TOKEN"] == CANARY
    assert child_environment["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_t7_spawn_failure_text_json_and_state_do_not_contain_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    _prepare(monkeypatch, tmp_path, calls=calls)
    monkeypatch.setattr(
        _cli_subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "claude",
            "--name",
            "worker",
            "--cwd",
            str(tmp_path),
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert "spawn failed" in result.output
    assert CANARY not in result.output
    assert calls.count("agent.runtime-launch.release") == 1
    contaminated = [
        path
        for path in (tmp_path / "state").rglob("*")
        if path.is_file() and CANARY.encode() in path.read_bytes()
    ]
    assert contaminated == []
