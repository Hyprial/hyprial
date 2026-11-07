"""Reviewed Codex resume matrix retained in the current split test layout."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.harnesses import codex

from ..support import ACTOR, THREAD_ID
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class StatefulResumeDaemon:
    """Small registry model that makes every launcher write observable."""

    def __init__(self, cwd: Path, *, exists: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.agent: dict[str, object] | None = (
            {
                "uri": ACTOR,
                "cwd": str(cwd),
                "provider": "openai",
                "model": "gpt-6-astra",
                "pinnedAdapters": ["isolated-test-adapter"],
                "capabilities": {
                    "harness": "codex",
                    "provider": "openai",
                    "model": "gpt-6-astra",
                    "tier": "super",
                },
            }
            if exists
            else None
        )
        self.desired_sessions: list[dict[str, object]] = []

    def snapshot(self) -> dict[str, object]:
        return {
            "agent": deepcopy(self.agent),
            "interactiveSessions": deepcopy(self.desired_sessions),
        }

    def __call__(
        self,
        method: str,
        params: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method in {"agent.get", "agent.start.preflight"}:
            if self.agent is None:
                raise _cli_get_services().CliError("AGENT_NOT_FOUND", "no such agent")
            return {
                "ok": True,
                "agent": deepcopy(self.agent),
                **(
                    {"resumeCommitGuardVersion": 1}
                    if method == "agent.start.preflight"
                    else {}
                ),
            }
        if method == "agent.runtime-context":
            return {"ok": True, "mode": "legacy", "environment": {}}
        if method == "ps":
            return {"daemon": {"owner": "op", "nodeId": "node-1"}}
        if method == "session.register_guarded":
            assert self.agent is not None
            self.agent["cwd"] = payload["cwd"]
            self.desired_sessions = [deepcopy(payload)]
            return {"ok": True, "liveIncumbentGuardApplied": True}
        if method == "session.unregister":
            self.desired_sessions = []
            return {"ok": True, "unregistered": True}
        raise AssertionError(method)


class LiveProcess:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _launch(monkeypatch: pytest.MonkeyPatch, daemon: StatefulResumeDaemon, server: type) -> list[LiveProcess]:
    processes: list[LiveProcess] = []

    class RecordingProcess(LiveProcess):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            processes.append(self)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr("hyprial.daemon.CodexInteractiveAppServer", server)
    monkeypatch.setattr(_cli_subprocess, "Popen", RecordingProcess)
    return processes


@pytest.mark.parametrize("phase", ["resume", "history"])
@pytest.mark.parametrize(
    "failure",
    [ConnectionError("socket closed"), TimeoutError("request timed out"), None],
    ids=["connection", "timeout", "non-object"],
)
def test_codex_resume_rpc_normalizes_transport_and_non_object_failures(
    tmp_path: Path, phase: str, failure: BaseException | None
) -> None:
    server = codex.CodexInteractiveAppServer(tmp_path / "app.sock", cwd=tmp_path)
    calls = 0

    def request(_method: str, _params: object = None) -> object:
        nonlocal calls
        calls += 1
        if phase == "history" and calls == 1:
            return {
                "thread": {
                    "id": THREAD_ID,
                    "cwd": str(tmp_path.resolve()),
                    "canAcceptDirectInput": True,
                    "turns": [],
                }
            }
        if failure is not None:
            raise failure
        return None

    server.request = request  # type: ignore[method-assign]
    with pytest.raises(codex.CodexInteractiveResumeError) as raised:
        server.resume_thread(THREAD_ID)
    assert raised.value.code == "CODEX_RESUME_RPC_FAILED"
    assert calls == (1 if phase == "resume" else 2)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"thread": None},
        {"thread": {"id": THREAD_ID, "cwd": "{cwd}", "canAcceptDirectInput": True}},
        {
            "thread": {
                "id": THREAD_ID,
                "cwd": "{cwd}",
                "canAcceptDirectInput": True,
                "turns": None,
            }
        },
    ],
    ids=[
        "non-object",
        "missing-thread",
        "non-object-thread",
        "missing-turns",
        "non-list-turns",
    ],
)
def test_codex_resume_attachment_malformed_results_use_attach_failure_code(
    tmp_path: Path, result: object
) -> None:
    server = codex.CodexInteractiveAppServer(tmp_path / "app.sock", cwd=tmp_path)
    if isinstance(result, dict):
        thread = result.get("thread")
        if isinstance(thread, dict) and thread.get("cwd") == "{cwd}":
            thread["cwd"] = str(tmp_path.resolve())
    server.request = lambda _method, _params=None: result  # type: ignore[method-assign,return-value]

    with pytest.raises(codex.CodexInteractiveResumeError) as raised:
        server.confirm_resumed_thread(THREAD_ID)
    assert raised.value.code == "CODEX_RESUME_TUI_ATTACH_FAILED"


def test_interactive_codex_resume_waits_for_direct_input_before_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = StatefulResumeDaemon(tmp_path / "old")

    class Server:
        pid = None

        def __init__(self, _socket_path: Path, **_kwargs: object) -> None:
            self.confirmations = 0

        def start(self) -> None:
            pass

        def resume_thread(self, thread_id: str, **_kwargs: object) -> tuple[str, str]:
            return thread_id, "thread/resume + thread/read"

        def confirm_resumed_thread(self, thread_id: str) -> tuple[str, str]:
            self.confirmations += 1
            if self.confirmations == 1:
                raise codex.CodexInteractiveResumePending("not ready")
            return thread_id, "requested thread/read"

        def stop(self) -> None:
            pass

    _launch(monkeypatch, daemon, Server)
    monkeypatch.setattr(_cli_get_services().time, "sleep", lambda _delay: None)
    result = _cli_get_services()._start_interactive_codex(
        name="codex-tui",
        nickname=None,
        cwd=tmp_path,
        resume=THREAD_ID,
        runtime_args=(),
        model_provider=None,
        model=None,
        json_output=True,
    )

    assert result["sessionRef"] == THREAD_ID
    methods = [method for method, _params in daemon.calls]
    assert methods.count("session.register_guarded") == 1


def test_interactive_codex_resume_never_ready_times_out_and_kills_tui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = StatefulResumeDaemon(tmp_path / "old")
    before = daemon.snapshot()

    class Server:
        pid = None

        def __init__(self, _socket_path: Path, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def resume_thread(self, thread_id: str, **_kwargs: object) -> tuple[str, str]:
            return thread_id, "thread/resume + thread/read"

        def confirm_resumed_thread(self, _thread_id: str) -> tuple[str, str]:
            raise codex.CodexInteractiveResumePending("not ready")

        def stop(self) -> None:
            pass

    processes = _launch(monkeypatch, daemon, Server)
    with pytest.raises(_cli_get_services().CliError) as raised:
        _cli_get_services()._start_interactive_codex(
            name="codex-tui",
            nickname=None,
            cwd=tmp_path,
            resume=THREAD_ID,
            runtime_args=(),
            model_provider=None,
            model=None,
            json_output=True,
            _attach_timeout_seconds=0.01,
        )

    assert raised.value.code == "CODEX_RESUME_TUI_ATTACH_FAILED"
    assert daemon.snapshot() == before
    assert processes[0].terminated is True
    assert "session.register_guarded" not in [method for method, _ in daemon.calls]


@pytest.mark.parametrize(
    "error_code", ["CODEX_RESUME_THREAD_UNKNOWN", "CODEX_RESUME_RPC_FAILED"]
)
def test_interactive_codex_resume_failure_never_spawns_or_changes_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_code: str
) -> None:
    daemon = StatefulResumeDaemon(tmp_path / "old")
    before = daemon.snapshot()

    class Server:
        pid = None

        def __init__(self, _socket_path: Path, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def resume_thread(self, _thread_id: str, **_kwargs: object) -> tuple[str, str]:
            raise codex.CodexInteractiveResumeError(error_code, "injected")

        def stop(self) -> None:
            pass

    processes = _launch(monkeypatch, daemon, Server)
    with pytest.raises(_cli_get_services().CliError) as raised:
        _cli_get_services()._start_interactive_codex(
            name="codex-tui",
            nickname=None,
            cwd=tmp_path,
            resume=THREAD_ID,
            runtime_args=(),
            model_provider=None,
            model=None,
            json_output=True,
        )

    assert raised.value.code == error_code
    assert daemon.snapshot() == before
    assert processes == []


def test_interactive_codex_wrong_tui_attachment_is_killed_without_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = StatefulResumeDaemon(tmp_path / "old")
    before = daemon.snapshot()

    class Server:
        pid = None

        def __init__(self, _socket_path: Path, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def resume_thread(self, thread_id: str, **_kwargs: object) -> tuple[str, str]:
            return thread_id, "thread/resume + thread/read"

        def confirm_resumed_thread(self, _thread_id: str) -> tuple[str, str]:
            raise codex.CodexInteractiveResumeError(
                "CODEX_RESUME_TUI_ATTACH_FAILED", "wrong thread"
            )

        def stop(self) -> None:
            pass

    processes = _launch(monkeypatch, daemon, Server)
    with pytest.raises(_cli_get_services().CliError) as raised:
        _cli_get_services()._start_interactive_codex(
            name="codex-tui",
            nickname=None,
            cwd=tmp_path,
            resume=THREAD_ID,
            runtime_args=(),
            model_provider=None,
            model=None,
            json_output=True,
        )

    assert raised.value.code == "CODEX_RESUME_TUI_ATTACH_FAILED"
    assert daemon.snapshot() == before
    assert processes[0].terminated is True
    assert "session.register_guarded" not in [method for method, _ in daemon.calls]


@pytest.mark.parametrize("failure", ["malformed", "early-exit", "wrong-cwd"])
def test_interactive_codex_resume_precommit_failures_preserve_all_persistent_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    daemon = StatefulResumeDaemon(tmp_path / "old")
    before = daemon.snapshot()

    class Server:
        pid = None

        def __init__(self, _socket_path: Path, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def resume_thread(self, thread_id: str, **_kwargs: object) -> tuple[str, str]:
            return thread_id, "thread/resume + thread/read"

        def confirm_resumed_thread(self, thread_id: str) -> tuple[str, str]:
            if failure == "malformed":
                raise codex.CodexInteractiveResumeError(
                    "CODEX_RESUME_TUI_ATTACH_FAILED", "malformed result"
                )
            if failure == "wrong-cwd":
                raise codex.CodexInteractiveResumeError(
                    "CODEX_RESUME_TUI_ATTACH_FAILED", "wrong cwd"
                )
            return thread_id, "requested thread/read"

        def stop(self) -> None:
            pass

    processes = _launch(monkeypatch, daemon, Server)
    if failure == "early-exit":
        original = _cli_subprocess.Popen

        def dead(*args: object, **kwargs: object) -> LiveProcess:
            process = original(*args, **kwargs)
            process.returncode = 9
            return process

        monkeypatch.setattr(_cli_subprocess, "Popen", dead)

    with pytest.raises(_cli_get_services().CliError) as raised:
        _cli_get_services()._start_interactive_codex(
            name="codex-tui",
            nickname=None,
            cwd=tmp_path,
            resume=THREAD_ID,
            runtime_args=(),
            model_provider=None,
            model=None,
            json_output=True,
        )

    assert raised.value.code == "CODEX_RESUME_TUI_ATTACH_FAILED"
    assert daemon.snapshot() == before
    assert before["agent"]["pinnedAdapters"] == ["isolated-test-adapter"]  # type: ignore[index]
    assert "session.register_guarded" not in [method for method, _ in daemon.calls]
    assert len(processes) == 1


def test_interactive_codex_resume_atomic_refusal_kills_owned_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = StatefulResumeDaemon(tmp_path / "old")
    before = daemon.snapshot()

    def reject_guard(method: str, params=None, **kwargs):  # type: ignore[no-untyped-def]
        if method == "session.register_guarded":
            daemon.calls.append((method, dict(params or {})))
            raise _cli_get_services().CliError("AGENT_ALREADY_RUNNING", "incumbent")
        return daemon(method, params, **kwargs)

    class Server:
        pid = None

        def __init__(self, _socket_path: Path, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def resume_thread(self, thread_id: str, **_kwargs: object) -> tuple[str, str]:
            return thread_id, "thread/resume + thread/read"

        def confirm_resumed_thread(self, thread_id: str) -> tuple[str, str]:
            return thread_id, "requested thread/read"

        def stop(self) -> None:
            pass

    processes = _launch(monkeypatch, daemon, Server)
    monkeypatch.setattr(_cli_support, "_daemon_request", reject_guard)
    with pytest.raises(_cli_get_services().CliError) as raised:
        _cli_get_services()._start_interactive_codex(
            name="codex-tui",
            nickname=None,
            cwd=tmp_path,
            resume=THREAD_ID,
            runtime_args=(),
            model_provider=None,
            model=None,
            json_output=True,
        )

    assert raised.value.code == "AGENT_ALREADY_RUNNING"
    assert daemon.snapshot() == before
    assert processes[0].terminated is True
    methods = [method for method, _ in daemon.calls]
    assert methods.count("session.register_guarded") == 1
    assert "agent.create" not in methods


def test_interactive_codex_resume_requires_existing_agent_without_creation_or_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = StatefulResumeDaemon(tmp_path / "old", exists=False)

    class ForbiddenServer:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("missing Agent must be rejected before server construction")

    processes = _launch(monkeypatch, daemon, ForbiddenServer)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "codex",
            "--name",
            "codex-tui",
            "--resume",
            THREAD_ID,
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "AGENT_NOT_FOUND"
    assert daemon.snapshot()["agent"] is None
    assert processes == []
    assert "agent.create" not in [method for method, _ in daemon.calls]


def test_start_help_says_interactive_codex_resume_requires_existing_agent() -> None:
    import click

    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--help"])

    assert result.exit_code == 0, result.output
    assert "interactive codex requires an existing agent" in " ".join(
        click.unstyle(result.stdout).lower().replace("│", " ").split()
    )
