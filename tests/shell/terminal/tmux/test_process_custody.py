"""tui_tmux: process custody scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)
import json
import os
import shlex
import socket
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
import pytest
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import AgentSecretStore
from hyprial.daemon.impl.harnesses.runtime._launch_cleanup import skill_plugin_path
from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_mod
from hyprial.daemon.impl.mcp.channel.ownership import _resolve_tmux_pane_owner
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

from .support import (
    _FakeFactory,
    _adapter,
    _fake_run_recorder,
    _legacy_runtime_context as _legacy_runtime_context,
)


def _r2_secret_environment_from_ipc(
    tmp_path: Path, canary: str
) -> tuple[DaemonApplication, dict[str, object]]:
    class Harnesses:
        @staticmethod
        def status() -> tuple[object, ...]:
            return ()

    class Presence:
        @staticmethod
        def actor_online(_actor: str) -> bool:
            return False

        @staticmethod
        def online_actors() -> tuple[str, ...]:
            return ()

        @staticmethod
        def online_mailboxes() -> tuple[str, ...]:
            return ()

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="owner",
        hyprial_home=home,
        **_deep_module_daemon_dependencies(),
    )
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = Harnesses()  # type: ignore[assignment]
    app._presence = Presence()  # type: ignore[assignment]
    personality = tmp_path / "personality"
    (personality / "native" / "claude").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    app.handle(
        "agent.create",
        {
            "name": "tmux-secret",
            "cwd": str(workspace),
            "config": {
                "sources": [{"path": str(personality), "required": True}],
                "discovery": "explicit-only",
            },
        },
    )
    app.handle(
        "agent.secret.provider-write",
        {"entryId": "deepseek", "fieldName": "apiKey", "value": canary},
    )
    app.handle(
        "agent.secret.grant",
        {
            "actor": "tmux-secret",
            "grantId": "deepseek",
            "source": "user-provider",
            "entryId": "deepseek",
            "fieldName": "apiKey",
            "environmentNames": ["DEEPSEEK_API_KEY"],
            "revision": 1,
        },
    )
    # The launch contract materializes the agent's sqlite store; the
    # user-provider grant only authorizes that value. Seed the store exactly as
    # the interactive launch regression does.
    AgentSecretStore(Path(app._agent_registry.home_receipt("tmux-secret").path)).set(
        service="deepseek",
        field="DEEPSEEK_API_KEY",
        value=canary,
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    context = app.handle(
        "agent.runtime-context",
        {"name": "tmux-secret", "harness": "claude", "cwd": str(workspace)},
    )
    acquired = app.handle(
        "agent.runtime-launch.acquire",
        {"launchToken": context["launchToken"], "operationId": "tmux-r2"},
    )
    return app, acquired


# ---------------------------------------------------------------------------
# tmux session-name derivation
# ---------------------------------------------------------------------------


def test_tmux_session_name_derives_from_actor() -> None:
    assert (
        tmux_mod.session_name_for_actor("agent:op:node-1:worker-node")
        == "hyprial-agent-op-node-1-worker-node"
    )


def test_tmux_session_name_sanitizes_tmux_target_separators() -> None:
    name = tmux_mod.session_name_for_actor("agent:o.p:n.1:weird actor/x")
    assert ":" not in name and "." not in name and " " not in name
    assert name.startswith("hyprial-")


def test_tmux_session_name_truncates_with_unique_digest() -> None:
    long_actor = "agent:op:node-1:" + "x" * 100
    name = tmux_mod.session_name_for_actor(long_actor)
    assert len(name) <= 64
    other = tmux_mod.session_name_for_actor(long_actor + "y")
    assert name != other


def test_tmux_attach_hints_cover_iterm2_cc_and_plain_attach() -> None:
    hints = tmux_mod.attach_hints("hyprial-agent-op-node-1-alice")
    assert hints["macOS-iTerm2"] == "tmux -CC attach -t hyprial-agent-op-node-1-alice"
    assert hints["wsl-linux-terminal"] == "tmux attach -t hyprial-agent-op-node-1-alice"


def test_tmux_has_session_and_kill_use_exact_session_target(
    monkeypatch: Any,
) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    monkeypatch.setattr(tmux_mod, "_run", _fake_run_recorder(calls))

    assert tmux_mod.has_session("/fake/tmux", "s1") is True
    tmux_mod.kill_session("/fake/tmux", "s1")

    assert calls == [
        (("has-session", "-t", "s1"), False),
        (("kill-session", "-t", "s1"), False),
    ]


def test_tmux_new_detached_session_hands_off_environment_without_argv_values(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    received: dict[str, str] = {}

    receiver: threading.Thread | None = None

    def fake_run(
        _tmux_bin: str, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        nonlocal receiver
        calls.append((tuple(args), check))
        command = args[-1]
        assert "/iso/home" not in command
        assert "a b'c" not in command
        parsed = shlex.split(command)
        module = parsed.index(
            "hyprial.daemon.impl.harnesses.python_worker._exec_env_socket"
        )

        def receive() -> None:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(parsed[module + 1])
                payload = bytearray()
                while b"\n" not in payload:
                    payload.extend(client.recv(64 * 1024))
            finally:
                client.close()
            received.update(json.loads(payload.partition(b"\n")[0]))

        receiver = threading.Thread(target=receive)
        receiver.start()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_mod, "_run", fake_run)

    tmux_mod.new_detached_session(
        "/fake/tmux",
        "s1",
        ["/bin/claude", "--session-id", "abc"],
        cwd=tmp_path,
        env={"HYPRIAL_HOME": "/iso/home", "WEIRD": "a b'c"},
    )
    assert receiver is not None
    receiver.join(timeout=1)

    ((call, check),) = calls
    assert check is True
    assert call[:6] == (
        "new-session",
        "-d",
        "-s",
        "s1",
        "-c",
        str(tmp_path),
    )
    command = call[-1]
    assert "_exec_env_socket" in command
    assert command.endswith("-- /bin/claude --session-id abc")
    assert received == {"HYPRIAL_HOME": "/iso/home", "WEIRD": "a b'c"}


def test_r2_real_tmux_private_server_keeps_secret_out_of_tmux_environment(
    monkeypatch: Any, tmp_path: Path
) -> None:
    tmux_bin = tmux_mod.find_tmux()
    if tmux_bin is None:
        pytest.skip("NOT_RUN: tmux is absent")

    server = f"hyprial-r2-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    wrapper = tmp_path / "private-tmux"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(tmux_bin)} -L {shlex.quote(server)} -f /dev/null "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    name = "hyprial-r2-secret"
    canary = "HYPRIAL-CANARY-R2-TMUX-9fb4d0d4"
    app, acquired = _r2_secret_environment_from_ipc(tmp_path, canary)
    secret_environment = acquired["secretEnvironment"]
    assert secret_environment == {"DEEPSEEK_API_KEY": canary}
    output = tmp_path / "child.env"
    child = tmp_path / "child"
    child.write_text(
        f"#!/bin/sh\numask 077\nenv > {shlex.quote(str(output))}\nsleep 30\n",
        encoding="utf-8",
    )
    child.chmod(0o700)

    handoff_path: Path | None = None
    handoff_mode: int | None = None
    real_handoff = tmux_mod._EnvironmentHandoff

    class ObservedHandoff(real_handoff):
        def release(self) -> None:
            nonlocal handoff_path, handoff_mode
            handoff_path = self.path
            handoff_mode = stat.S_IMODE(self.path.stat().st_mode)
            super().release()

    monkeypatch.setattr(tmux_mod, "_EnvironmentHandoff", ObservedHandoff)
    try:
        tmux_mod.new_detached_session(
            str(wrapper),
            name,
            [str(child)],
            cwd=tmp_path,
            env=secret_environment,
        )
        deadline = time.monotonic() + 3.0
        while not output.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)

        global_environment = subprocess.run(
            [tmux_bin, "-L", server, "show-environment", "-g"],
            capture_output=True,
            text=True,
            check=True,
        )
        session_environment = subprocess.run(
            [tmux_bin, "-L", server, "show-environment", "-t", name],
            capture_output=True,
            text=True,
            check=True,
        )
        assert canary not in global_environment.stdout + global_environment.stderr
        assert canary not in session_environment.stdout + session_environment.stderr
        assert f"DEEPSEEK_API_KEY={canary}" in output.read_text(encoding="utf-8")
        assert handoff_mode == 0o600
        assert handoff_path is not None
        assert not handoff_path.exists()
    finally:
        app.handle(
            "agent.runtime-launch.release",
            {
                "leaseToken": acquired["leaseToken"],
                "operationId": acquired["operationId"],
            },
        )
        subprocess.run(
            [tmux_bin, "-L", server, "kill-server"],
            capture_output=True,
            text=True,
            check=False,
        )


def test_tmux_establishes_cleanup_owner_before_releasing_tui_environment(
    monkeypatch: Any, tmp_path: Path
) -> None:
    events: list[str] = []

    class Handoff:
        path = tmp_path / "environment.sock"

        def __init__(self, _environment: dict[str, str]) -> None:
            pass

        def release(self) -> None:
            events.append("release")

        def wait(self) -> None:
            events.append("wait")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(tmux_mod, "_EnvironmentHandoff", Handoff)

    recorded_args: list[str] = []

    def fake_run(
        _tmux_bin: str, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        recorded_args.extend(args)
        events.append("new-session")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_mod, "_run", fake_run)
    tmux_mod.new_detached_session(
        "/fake/tmux",
        "s1",
        ["/bin/claude"],
        cwd=tmp_path,
        env={},
        cleanup=tmux_mod.LaunchCleanupResources(tmp_path / "launch.json"),
    )

    assert events == ["new-session", "release", "wait"]
    assert recorded_args[0] == "new-session"
    command = recorded_args[-1]
    assert "--cleanup-config" in command
    assert str(tmp_path / "launch.json") in command
    assert command.index("--cleanup-config") < command.index(" -- /bin/claude")


def test_tmux_spawn_failure_releases_resources_before_tui_can_register(
    monkeypatch: Any, tmp_path: Path
) -> None:
    events: list[str] = []

    class Handoff:
        path = tmp_path / "environment.sock"

        def __init__(self, _environment: dict[str, str]) -> None:
            pass

        def release(self) -> None:
            events.append("release")

        def wait(self) -> None:
            events.append("wait")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(tmux_mod, "_EnvironmentHandoff", Handoff)
    calls: list[tuple[list[str], bool]] = []

    def fake_run(
        _tmux_bin: str, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, check))
        if check:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(tmux_mod, "_run", fake_run)
    config = tmp_path / "launch.json"
    recovery = tmp_path / "launch.recover"
    session_ref = config.with_suffix(".session-ref")
    session_ref.write_text("opaque-session", encoding="utf-8")
    skills = skill_plugin_path(config)
    config.write_text("{}", encoding="utf-8")
    recovery.write_text("recovery", encoding="utf-8")
    skills.mkdir()

    with pytest.raises(subprocess.CalledProcessError):
        tmux_mod.new_detached_session(
            "/fake/tmux",
            "s1",
            ["/bin/claude"],
            cwd=tmp_path,
            env={},
            cleanup=tmux_mod.LaunchCleanupResources(config, recovery),
        )

    assert events == ["close"]
    assert calls[-1] == (["kill-session", "-t", "s1"], False)
    assert not config.exists()
    assert not recovery.exists()
    assert not session_ref.exists()
    assert not skills.exists()


def test_tmux_pane_pid_parses_display_message(monkeypatch: Any) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    monkeypatch.setattr(tmux_mod, "_run", _fake_run_recorder(calls, stdout="4321\n"))
    assert tmux_mod.pane_pid("/fake/tmux", "s1") == 4321


# ---------------------------------------------------------------------------
# channel owner fence: tmux pane resolution
# ---------------------------------------------------------------------------


def test_resolve_tmux_pane_owner_returns_pid_and_identity() -> None:
    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="777\n", stderr="")

    resolved = _resolve_tmux_pane_owner(
        "hyprial-s",
        run=fake_run,
        read_identity=lambda pid: f"birth-{pid}",
        which=lambda name: "/fake/tmux",
    )
    assert resolved == (777, "birth-777")


def test_resolve_tmux_pane_owner_degrades_on_any_lookup_failure() -> None:
    bad_results = [
        subprocess.CompletedProcess([], 1, stdout="", stderr="no session"),
        subprocess.CompletedProcess([], 0, stdout="not-a-pid", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
    ]
    for bad in bad_results:
        assert (
            _resolve_tmux_pane_owner(
                "hyprial-s",
                run=lambda *a, **k: bad,
                read_identity=lambda pid: "birth",
                which=lambda name: "/fake/tmux",
            )
            is None
        )
    # No tmux binary at all.
    assert (
        _resolve_tmux_pane_owner(
            "hyprial-s",
            run=lambda *a, **k: subprocess.CompletedProcess(
                [], 0, stdout="1\n", stderr=""
            ),
            read_identity=lambda pid: "birth",
            which=lambda name: None,
        )
        is None
    )
    # Pane found but its birth marker unreadable.
    assert (
        _resolve_tmux_pane_owner(
            "hyprial-s",
            run=lambda *a, **k: subprocess.CompletedProcess(
                [], 0, stdout="1\n", stderr=""
            ),
            read_identity=lambda pid: None,
            which=lambda name: "/fake/tmux",
        )
        is None
    )


@pytest.mark.anyio
async def test_channel_adapter_register_includes_tmux_session() -> None:
    factory = _FakeFactory([{"ok": True}, {"ok": True}, {"ok": True, "messages": []}])

    await _adapter(factory, "hyprial-agent-op-node-1-alice").start()

    registration = factory.calls[0][1]
    assert registration["tmuxSession"] == "hyprial-agent-op-node-1-alice"


@pytest.mark.anyio
async def test_channel_adapter_register_omits_tmux_session_when_detached_off() -> None:
    factory = _FakeFactory([{"ok": True}, {"ok": True}, {"ok": True, "messages": []}])

    await _adapter(factory, None).start()

    assert "tmuxSession" not in factory.calls[0][1]


def test_real_environment_handoff_is_private_and_removed_when_aborted() -> None:
    """The abort path (spawn failure) removes the one-shot socket too."""

    canary = f"HYPRIAL-CANARY-{uuid.uuid4().hex}"
    handoff = tmux_mod._EnvironmentHandoff({"DEEPSEEK_API_KEY": canary})
    path = Path(handoff.path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        handoff.close()
    assert not path.exists()
