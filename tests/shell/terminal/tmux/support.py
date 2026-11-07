"""Detached-tmux interactive TUI launches (`hyprial agent start claude --tmux`)."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import subprocess
import uuid as uuid_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_mod
from hyprial.daemon.impl.inbox import AckResult, InboxMessage, SubmissionResult
from hyprial.daemon.impl.mcp import (
    ClaudeChannelAdapter,
    ClaudeChannelDriver,
    StatelessDaemonProxy,
    WakeCoordinator,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture(autouse=True)
def _legacy_runtime_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing tmux cases exercise the explicit legacy launch mode."""

    monkeypatch.setattr(_cli_get_services(), "_runtime_context_environment", lambda **_kwargs: None)


# ---------------------------------------------------------------------------
# tmux module process wrappers
# ---------------------------------------------------------------------------


def _fake_run_recorder(
    calls: list[tuple[tuple[str, ...], bool]],
    *,
    returncode: int = 0,
    stdout: str = "",
) -> Any:
    def fake_run(
        tmux_bin: str, args: tuple[str, ...], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(args), check))
        return subprocess.CompletedProcess(
            list(args), returncode, stdout=stdout, stderr=""
        )

    return fake_run


# ---------------------------------------------------------------------------
# registration plumbing: adapter -> desired state -> ps
# ---------------------------------------------------------------------------


@dataclass
class _FakeStore:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class _FakeFactory:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def connect(self):  # type: ignore[no-untyped-def]
        factory = self

        class Connection:
            async def request(
                self,
                method: str,
                params: dict[str, Any],
                *,
                request_id: str | None = None,
            ) -> dict[str, Any]:
                factory.calls.append((method, params))
                return factory.responses.pop(0)

        class ConnectionContext:
            async def __aenter__(self) -> Connection:
                return Connection()

            async def __aexit__(self, *args: Any) -> None:
                return None

        return ConnectionContext()


class _RecordingChannel:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))


def _adapter(factory: _FakeFactory, tmux_session: str | None) -> ClaudeChannelAdapter:
    session = InteractiveSession(
        actor="alice",
        cwd="/work/alice",
        command=("claude",),
        source="claude-channel",
    )
    return ClaudeChannelAdapter(
        StatelessDaemonProxy(factory),
        WakeCoordinator(
            _FakeStore(DesiredState(interactive_sessions=(session,))),
            ClaudeChannelDriver(_RecordingChannel()),
        ),
        actor="alice",
        session_ref="session-1",
        cwd="/work/alice",
        command=("claude",),
        tmux_session=tmux_session,
    )


class _FakeInbox:
    def pending_messages(self, actor: str) -> tuple[()]:
        return ()

    def pending_all(self) -> tuple[()]:
        return ()

    def fetch_pending(self, actor: str) -> tuple[()]:
        return ()

    def drain_system_notices(self, actor: str) -> tuple[()]:
        return ()

    def submit(self, message: InboxMessage) -> SubmissionResult:
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class _StubPresence:
    def actor_online(self, actor: str) -> bool:
        return True

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


# ---------------------------------------------------------------------------
# CLI: hyprial agent start claude --tmux
# ---------------------------------------------------------------------------


def _install_fake_daemon(
    monkeypatch: Any,
    observed: dict[str, Any],
    *,
    registers: bool = True,
    source: str | None = None,
) -> None:
    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"}
        }
        if registers and observed.get("spawned"):
            entry: dict[str, Any] = {
                "actor": "agent:op:node-1:worker-node",
                "sessionRef": observed["session_ref"],
                "channelConfirmed": True,
            }
            if source is not None:
                entry["source"] = source
            response["interactiveSessions"] = [entry]
        return response

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)


def _install_fake_tmux(
    monkeypatch: Any,
    observed: dict[str, Any],
    *,
    session_dies: bool = False,
    capture_config: bool = True,
    spawn_fails: bool = False,
) -> dict[str, Any]:
    """Fake the tmux module boundary the CLI drives; returns the kill record."""

    killed: list[str] = []
    cleanup_owners: list[tuple[str, Path, Path | None]] = []

    monkeypatch.setattr(tmux_mod, "find_tmux", lambda: "/fake/tmux")

    def has_session(tmux_bin: str, name: str) -> bool:
        if not observed.get("spawned"):
            return False
        return not session_dies

    def new_session(
        tmux_bin: str,
        name: str,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        cleanup: tmux_mod.LaunchCleanupResources | None = None,
    ) -> None:
        observed["tmux_session_name"] = name
        observed["argv"] = list(argv)
        observed["cwd"] = cwd
        observed["env"] = dict(env)
        observed["session_ref"] = argv[argv.index("--session-id") + 1]
        if capture_config:
            config_path = Path(argv[argv.index("--mcp-config") + 1])
            observed["config_bytes"] = config_path.read_bytes()
            observed["config_path"] = config_path
        if spawn_fails:
            raise subprocess.CalledProcessError(1, ["tmux", "new-session"])
        observed["spawned"] = True
        if cleanup is not None:
            cleanup_owners.append(
                (name, cleanup.config_path, cleanup.recovery_path)
            )

    monkeypatch.setattr(tmux_mod, "has_session", has_session)
    monkeypatch.setattr(tmux_mod, "new_detached_session", new_session)

    monkeypatch.setattr(
        tmux_mod, "kill_session", lambda _bin, name: killed.append(name)
    )
    return {"killed": killed, "cleanupOwners": cleanup_owners}


def _invoke_tmux_start(monkeypatch: Any, tmp_path: Path) -> Any:
    (tmp_path / "home").mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/test/bin/claude")
    uuids = iter(
        [
            uuid_module.UUID("11111111-2222-3333-4444-555555555555"),
            uuid_module.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        ]
    )
    monkeypatch.setattr(_cli_get_services(), "uuid4", lambda: next(uuids))
    return CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "claude",
            "--name",
            "worker-node",
            "--cwd",
            str(tmp_path),
            "--tmux",
            "--json",
        ],
    )


# ---------------------------------------------------------------------------
# CLI: hyprial agent start pi --tmux
# ---------------------------------------------------------------------------


def _invoke_pi_tmux_start(monkeypatch: Any, tmp_path: Path) -> Any:
    (tmp_path / "home").mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_PI_BIN", "/test/bin/pi")
    monkeypatch.setattr(
        _cli_get_services(),
        "uuid4",
        lambda: uuid_module.UUID("11111111-2222-3333-4444-555555555555"),
    )
    return CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "worker-node",
            "--cwd",
            str(tmp_path),
            "--tmux",
            "--json",
        ],
    )


# --- Claude Code's start-up confirmations (CC-owned; observed live 2026-09-23) --

# Verbatim pane captures from claude 2.1.280 in tmux (blank lines dropped).
_DEV_CHANNEL_PROMPT = (
    "  WARNING: Loading development channels\n"
    "  --dangerously-load-development-channels is for local channel development\n"
    "  Channels: server:probe\n"
    "  \u276f 1. I am using this for local development\n"
    "    2. Exit\n"
)
_FOLDER_TRUST_PROMPT = (
    " Quick safety check: Is this a project you created or one you trust? (Like your\n"
    " \u276f No, exit\n"
    "   Yes, I trust this folder\n"
)
