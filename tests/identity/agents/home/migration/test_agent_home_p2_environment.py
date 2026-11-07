"""P22: resolved roots/tool profiles through the final environment boundary."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from urllib.parse import urlsplit

from hyprial.daemon.impl.updates import DEFAULT_GIT_URL as _DEFAULT_GIT_URL

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    apply_runtime_environment_profile,
    compose_worker_child_launch,
)
from hyprial.identity.impl.agents.runtime.context import (
    AgentToolProfile,
    SshToolAuthorization,
    resolve_agent_runtime_context,
)
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

_FORGEJO_HOST = urlsplit(_DEFAULT_GIT_URL).hostname


def test_cli_runtime_context_environment_preserves_validated_projection_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projection = {
        "ok": True,
        "mode": "agent-home-p2",
        "environment": {"HOME": str(tmp_path / "tool-home")},
        "projectionRoot": str(tmp_path / "projection"),
        "nativeRoot": str(tmp_path / "native"),
        "sessionRoot": str(tmp_path / "sessions"),
    }
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_args, **_kwargs: projection)

    environment = _cli_get_services()._runtime_context_environment(
        name="worker", harness="pi", cwd=tmp_path
    )
    full_projection = _cli_get_services()._runtime_context_environment(
        name="worker", harness="pi", cwd=tmp_path, include_projection=True
    )

    assert environment == projection["environment"]
    assert full_projection == projection
    assert full_projection is not projection


def test_cli_runtime_context_environment_rejects_relative_projection_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projection = {
        "ok": True,
        "mode": "agent-home-p2",
        "environment": {"HOME": str(tmp_path / "tool-home")},
        "projectionRoot": "relative-projection",
        "nativeRoot": str(tmp_path / "native"),
        "sessionRoot": str(tmp_path / "sessions"),
    }
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_args, **_kwargs: projection)

    with pytest.raises(_cli_get_services().CliError, match="invalid agent runtime projectionRoot"):
        _cli_get_services()._runtime_context_environment(name="worker", harness="pi", cwd=tmp_path)


def _config(root: Path) -> Path:
    config = root / "personality"
    for harness, filename, body in (
        ("claude", "settings.json", "{}\n"),
        ("codex", "config.toml", 'model = "p22"\n'),
        ("pi", "settings.json", "{}\n"),
    ):
        path = config / "native" / harness / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    skill = config / "shared" / "skills" / "p22" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# P22\n", encoding="utf-8")
    return config


def _registry(tmp_path: Path, *, configured: bool = True) -> tuple[AgentRegistry, Path]:
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir(parents=True)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="p22-owner",
        machine="p22-node",
        hyprial_home=hyprial_home,
    )
    config = _config(tmp_path) if configured else None
    registry.create(
        "worker",
        cwd=str(tmp_path / "work"),
        config=(
            {
                "sources": [{"path": str(config), "required": True}],
                "discovery": "explicit-only",
            }
            if config is not None
            else None
        ),
    )
    registry.ensure_home("worker")
    return registry, hyprial_home


def _context(
    registry: AgentRegistry,
    harness: str,
    *,
    tool_profile: AgentToolProfile = TEST_TOOL_PROFILE,
):
    return resolve_agent_runtime_context(
        registry=registry,
        agent_name="worker",
        harness=harness,
        cwd=registry.require("worker").cwd,
        tool_profile=tool_profile,
    )


@pytest.mark.parametrize(
    ("harness", "native_name"),
    (
        ("claude", "CLAUDE_CONFIG_DIR"),
        ("codex", "CODEX_HOME"),
        ("pi", "PI_CODING_AGENT_DIR"),
    ),
)
def test_t09_shared_runtime_context_flows_to_complete_launch(
    tmp_path: Path, harness: str, native_name: str
) -> None:
    registry, hyprial_home = _registry(tmp_path)
    try:
        context = _context(registry, harness)
        assert context is not None
        channel = build_worker_channel(
            actor=registry.require("worker").uri,
            session_ref="p22-session",
            hyprial_home=hyprial_home,
            state_dir=tmp_path / "daemon-state",
            node_id="p22-node",
            owner="p22-owner",
            runtime_context=context,
        )
        before = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/ambient/home",
            "XDG_CONFIG_HOME": "/ambient/xdg",
            "SSH_AUTH_SOCK": "/ambient/agent.sock",
            "CLAUDE_CONFIG_DIR": "/ambient/claude",
            "CODEX_HOME": "/ambient/codex",
            "PI_CODING_AGENT_DIR": "/ambient/pi",
        }
        snapshot = dict(before)
        launch = compose_worker_child_launch(
            registry=registry,
            hyprial_home=hyprial_home,
            channel=channel,
            environ=before,
            agent_name="worker",
        )
        assert isinstance(launch, ChildEnvironmentLaunch)
        assert launch.runtime_context is context
        environment = launch.environment.for_exec()
        assert environment[native_name] == str(context.roots.native_root)
        assert {
            "CLAUDE_CONFIG_DIR",
            "CODEX_HOME",
            "PI_CODING_AGENT_DIR",
        }.intersection(environment) == {native_name}
        assert environment["HOME"] == str(context.roots.tool_home)
        assert environment["XDG_CONFIG_HOME"] == str(
            context.roots.xdg_config_home
        )
        assert "SSH_AUTH_SOCK" not in environment
        assert before == snapshot, "daemon environment input must not be mutated"
    finally:
        registry.close()


def test_t10_legacy_and_p2_modes_are_distinct_and_dsh_is_untouched(
    tmp_path: Path,
) -> None:
    legacy, _home = _registry(tmp_path / "legacy", configured=False)
    configured, _home2 = _registry(tmp_path / "configured")
    try:
        assert _context(legacy, "codex") is None
        assert _context(configured, "dsh") is None
        context = _context(configured, "codex")
        assert context is not None
        p2 = apply_runtime_environment_profile(
            {"HOME": "/ambient", "SSH_AUTH_SOCK": "/ambient/sock", "PATH": "/bin"},
            context.environment(),
        )
        p1 = apply_runtime_environment_profile(
            {"HOME": "/ambient", "SSH_AUTH_SOCK": "/ambient/sock", "PATH": "/bin"},
            None,
        )
        assert p1["HOME"] == "/ambient"
        assert p1["SSH_AUTH_SOCK"] == "/ambient/sock"
        assert p2["HOME"] == str(context.roots.tool_home)
        assert "SSH_AUTH_SOCK" not in p2
    finally:
        legacy.close()
        configured.close()


def test_t11_tool_xdg_is_per_agent_and_public_context_contains_no_authority(
    tmp_path: Path,
) -> None:
    registry, _home = _registry(tmp_path)
    try:
        context = _context(registry, "codex")
        assert context is not None
        first = registry.require("worker")
        registry.create(
            "worker-b",
            cwd=first.cwd,
            config=first.config,
        )
        registry.ensure_home("worker-b")
        other = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker-b",
            harness="codex",
            cwd=first.cwd,
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert other is not None
        assert context.roots.xdg_config_home != other.roots.xdg_config_home
        public = context.public_projection()
        serialized = json.dumps(public, sort_keys=True)
        assert public["environment"]["XDG_CONFIG_HOME"] == str(
            context.roots.xdg_config_home
        )
        assert public["environment"]["HYPRIAL_TEA_LOGIN"] == "code.hyprial.com"
        assert "entityToken" not in serialized
        assert "projectionReceipt" not in serialized
        assert "grant" not in serialized.lower()
        assert "P20_SENTINEL_NOT_A_SECRET" not in serialized
    finally:
        registry.close()


def test_t12_real_git_and_ssh_read_the_generated_nonsecret_profile(
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    ssh = shutil.which("ssh")
    if git is None or ssh is None:
        pytest.skip("git and ssh are required for the P22 native profile proof")
    identity = tmp_path / "id_ed25519.pub"
    known_hosts = tmp_path / "known_hosts"
    socket = tmp_path / "agent.sock"
    identity.write_text("P22_SENTINEL_PUBLIC_KEY_NOT_REAL\n", encoding="utf-8")
    known_hosts.write_text("P22_SENTINEL_KNOWN_HOST_NOT_REAL\n", encoding="utf-8")
    profile = AgentToolProfile(
        profile_id="p22-authorized-sentinel",
        git_author_name="Test Author",
        git_author_email="test-author@users.noreply.example.com",
        ssh=SshToolAuthorization(
            auth_sock=str(socket),
            identity_file=str(identity),
            known_hosts_file=str(known_hosts),
        ),
    )
    registry, _home = _registry(tmp_path / "agent")
    try:
        context = _context(registry, "codex", tool_profile=profile)
        assert context is not None
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **context.environment(),
        }
        assert environment["GIT_CONFIG_COUNT"] == "1"
        assert environment["GIT_CONFIG_KEY_0"].endswith(".insteadOf")
        assert environment["GIT_CONFIG_VALUE_0"] == "https://code.hyprial.com/"
        assert environment["GIT_CONFIG_GLOBAL"].startswith(
            str(context.roots.tool_profile_root)
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run([git, "init", "-q"], cwd=repo, env=environment, check=True)
        (repo / "note.txt").write_text("p22\n", encoding="utf-8")
        subprocess.run([git, "add", "note.txt"], cwd=repo, env=environment, check=True)
        subprocess.run(
            [git, "commit", "-q", "-m", "p22 profile"],
            cwd=repo,
            env=environment,
            check=True,
        )
        author = subprocess.run(
            [git, "show", "-s", "--format=%an <%ae>"],
            cwd=repo,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert author == "Test Author <test-author@users.noreply.example.com>"
        rewritten = subprocess.run(
            [
                git,
                "config",
                "--global",
                "--get-regexp",
                r"^url\..*\.insteadof$",
            ],
            cwd=repo,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert (
            f"url.ssh://git@{_FORGEJO_HOST}/.insteadof "
            "https://code.hyprial.com/"
        ) in rewritten.lower()

        ssh_argv = shlex.split(environment["GIT_SSH_COMMAND"])
        rendered = subprocess.run(
            [*ssh_argv, "-G", _FORGEJO_HOST],
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.lower()
        assert f"identityagent {socket}".lower() in rendered
        assert f"identityfile {identity}".lower() in rendered
        assert "stricthostkeychecking true" in rendered
        assert "passwordauthentication no" in rendered
    finally:
        registry.close()


def test_m12a_incomplete_generated_git_group_is_rejected() -> None:
    with pytest.raises(ValueError, match="Git one-shot"):
        apply_runtime_environment_profile(
            {"PATH": "/bin"},
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.ssh://git@git.internal/.insteadOf",
            },
        )


def test_t09_pty_complete_environment_does_not_remerge_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.harnesses.protocol.common import HarnessStartError, PtyHarnessProcess

    dump = tmp_path / "pty.json"
    monkeypatch.setenv("HOME", "/ambient/home")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/sock")
    script = f"import json,os;json.dump(dict(os.environ),open({str(dump)!r},'w'))"
    try:
        PtyHarnessProcess.spawn(
            "codex",
            (sys.executable, "-c", script),
            env={"PATH": os.environ.get("PATH", "/bin"), "HOME": "/p22/home"},
            complete_environment=True,
        )
    except HarnessStartError:
        pass
    observed = json.loads(dump.read_text(encoding="utf-8"))
    assert observed["HOME"] == "/p22/home"
    assert "SSH_AUTH_SOCK" not in observed


def test_m09_broker_complete_handoff_does_not_remerge_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.harnesses.python_worker import _exec_env_socket

    monkeypatch.setenv("HOME", "/ambient/home")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/sock")
    observed: dict[str, str] = {}

    def execvpe(_argv0: str, _argv: list[str], environment: dict[str, str]) -> None:
        observed.update(environment)
        raise SystemExit(0)

    class Socket:
        sent = False

        def connect(self, _path: str) -> None:
            return None

        def recv(self, _size: int) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b'{"HOME":"/p22/home","PATH":"/bin"}\n'

        def close(self) -> None:
            return None

    monkeypatch.setattr(_exec_env_socket.os, "execvpe", execvpe)
    monkeypatch.setattr(_exec_env_socket.socket, "socket", lambda *_a, **_kw: Socket())
    monkeypatch.setattr(
        _exec_env_socket.sys,
        "argv",
        ["broker", "/socket", "--complete-environment", "--", "true"],
    )
    with pytest.raises(SystemExit):
        _exec_env_socket.main()
    assert observed == {"HOME": "/p22/home", "PATH": "/bin"}


def test_t16_daemon_runtime_context_rpc_is_nonsecret_and_legacy_is_explicit(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.application import DaemonApplication

    class Harnesses:
        def status(self) -> tuple[object, ...]:
            return ()

    class Presence:
        def actor_online(self, _actor: str) -> bool:
            return False

        def online_actors(self) -> tuple[str, ...]:
            return ()

        def online_mailboxes(self) -> tuple[str, ...]:
            return ()

    config = _config(tmp_path)
    app = DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon.sock",
        node_id="p22-node",
        owner="p22-owner",
        hyprial_home=tmp_path / "daemon-home",
    **_deep_module_daemon_dependencies())
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = Harnesses()  # type: ignore[assignment]
    app._presence = Presence()  # type: ignore[assignment]
    try:
        app.handle(
            "agent.create",
            {
                "name": "configured",
                "cwd": str(tmp_path / "work"),
                "config": {
                    "sources": [{"path": str(config), "required": True}],
                    "discovery": "explicit-only",
                },
            },
        )
        configured = app.handle(
            "agent.runtime-context",
            {
                "name": "configured",
                "harness": "codex",
                "cwd": str(tmp_path / "work"),
            },
        )
        assert configured["mode"] == "agent-home-p2"
        serialized = json.dumps(configured, sort_keys=True)
        assert "entityToken" not in serialized
        assert "projectionReceipt" not in serialized
        assert "privateEnv" not in serialized

        claude = app.handle(
            "agent.runtime-context",
            {
                "name": "configured",
                "harness": "claude",
                "cwd": str(tmp_path / "work"),
            },
        )
        native_root = Path(str(claude["nativeRoot"]))
        session_root = Path(str(claude["sessionRoot"]))
        assert (native_root / "settings.json").read_text(encoding="utf-8") == "{}\n"
        assert (native_root / "projects").is_symlink()
        assert (native_root / "projects").readlink() == session_root

        app.agents.create("legacy")
        legacy = app.handle(
            "agent.runtime-context",
            {"name": "legacy", "harness": "codex"},
        )
        assert legacy == {"ok": True, "mode": "legacy", "environment": {}}
    finally:
        app.agents.close()
