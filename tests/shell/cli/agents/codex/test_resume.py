"""Strict interactive Codex resume contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.harnesses import codex

from .support import THREAD_ID, candidate
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _thread(cwd: Path, *, ready: bool = True) -> dict[str, object]:
    return {
        **candidate(cwd),
        "canAcceptDirectInput": ready,
        "turns": [{"id": "prior-turn", "items": [{"text": "before move"}]}],
    }


def test_codex_resume_rpc_uses_exact_id_and_new_cwd_without_history_injection(
    tmp_path: Path,
) -> None:
    server = codex.CodexInteractiveAppServer(tmp_path / "app.sock", cwd=tmp_path)
    calls: list[tuple[str, object]] = []

    def request(method: str, params: object = None) -> dict[str, object]:
        calls.append((method, params))
        return {"thread": _thread(tmp_path)}

    server.request = request  # type: ignore[method-assign]
    assert server.resume_thread(
        THREAD_ID, runtime_args=("--sandbox", "workspace-write"), model="gpt-6-astra"
    ) == (THREAD_ID, "thread/resume + thread/read")
    assert [method for method, _ in calls] == ["thread/resume", "thread/read"]
    resume = calls[0][1]
    assert isinstance(resume, dict)
    assert resume["threadId"] == THREAD_ID
    assert resume["cwd"] == str(tmp_path.resolve())
    assert resume["model"] == "gpt-6-astra"
    assert "history" not in resume
    assert "path" not in resume
    assert calls[1][1] == {"threadId": THREAD_ID, "includeTurns": True}


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("no rollout found for thread", "CODEX_RESUME_THREAD_UNKNOWN"),
        ("rollout path is outside the permitted session root", "CODEX_RESUME_OUTSIDE_SCOPE"),
        ("native resume transport failed", "CODEX_RESUME_RPC_FAILED"),
    ],
)
def test_codex_resume_rpc_maps_distinct_failures(
    tmp_path: Path, message: str, code: str
) -> None:
    server = codex.CodexInteractiveAppServer(tmp_path / "app.sock", cwd=tmp_path)

    def request(_method: str, _params: object = None) -> dict[str, object]:
        raise codex.CodexAppServerRpcError(message)

    server.request = request  # type: ignore[method-assign]
    error_type = getattr(codex, "CodexInteractiveResumeError", RuntimeError)
    with pytest.raises(error_type) as raised:
        server.resume_thread(THREAD_ID)
    assert raised.value.code == code


def test_codex_resume_confirmation_waits_for_input_and_rejects_wrong_identity(
    tmp_path: Path,
) -> None:
    server = codex.CodexInteractiveAppServer(tmp_path / "app.sock", cwd=tmp_path)
    results = iter(
        [
            {"thread": _thread(tmp_path, ready=False)},
            {"thread": {**_thread(tmp_path), "id": "wrong-thread"}},
        ]
    )
    server.request = lambda *_args, **_kwargs: next(results)  # type: ignore[method-assign]
    pending_type = getattr(codex, "CodexInteractiveResumePending", RuntimeError)
    error_type = getattr(codex, "CodexInteractiveResumeError", RuntimeError)
    with pytest.raises(pending_type):
        server.confirm_resumed_thread(THREAD_ID)
    with pytest.raises(error_type) as raised:
        server.confirm_resumed_thread(THREAD_ID)
    assert raised.value.code == "CODEX_RESUME_TUI_ATTACH_FAILED"


def test_start_routes_interactive_codex_resume_and_uses_registered_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def daemon_request(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if method == "agent.get":
            return {"agent": {"cwd": str(tmp_path)}}
        raise AssertionError(method)

    def start_codex(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_get_services(), "_start_interactive_codex", start_codex)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "codex", "--name", "codex-tui", "--resume", THREAD_ID, "--json"],
    )
    assert result.exit_code == 0, result.output
    assert captured["resume"] == THREAD_ID
    assert captured["cwd"] == tmp_path.resolve()


def test_start_rejects_invalid_interactive_codex_resume_cwd_before_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    def start_codex(**_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(_cli_get_services(), "_start_interactive_codex", start_codex)
    missing = tmp_path / "missing"
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
            str(missing),
            "--json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_CWD"
    assert called is False


@pytest.mark.parametrize("guard_result", ["confirmed", "missing-method", "missing-echo"])
def test_interactive_codex_resume_uses_guarded_commit_without_plain_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    guard_result: str,
) -> None:
    daemon_calls: list[tuple[str, dict[str, object]]] = []
    events: list[str] = []
    processes: list[Process] = []

    def daemon_request(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        payload = params or {}
        daemon_calls.append((method, payload))
        if method == "agent.start.preflight":
            return {"ok": True, "resumeCommitGuardVersion": 1}
        if method == "agent.runtime-context":
            return {"ok": True, "mode": "legacy", "environment": {}}
        if method == "ps":
            return {"daemon": {"owner": "op", "nodeId": "node-1"}}
        if method == "session.register_guarded":
            if guard_result == "missing-method":
                raise _cli_get_services().CliError("METHOD_NOT_FOUND", "old daemon")
            return {
                "ok": True,
                **(
                    {"liveIncumbentGuardApplied": True}
                    if guard_result == "confirmed"
                    else {}
                ),
            }
        return {"ok": True}

    class Server:
        pid = None

        def __init__(self, _socket_path: Path, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            events.append("server.start")

        def resume_thread(self, thread_id: str, **_kwargs: object):  # type: ignore[no-untyped-def]
            assert thread_id == THREAD_ID
            events.append("thread.resume")
            return thread_id, "thread/resume + thread/read"

        def confirm_resumed_thread(self, thread_id: str):  # type: ignore[no-untyped-def]
            assert thread_id == THREAD_ID
            return thread_id, "requested thread/read"

        def stop(self) -> None:
            events.append("server.stop")

    class Process:
        @classmethod
        def __class_getitem__(cls, _item: object) -> type["Process"]:
            return cls

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self.argv = argv
            self.kwargs = kwargs
            self.returncode: int | None = None
            self.terminated = False
            processes.append(self)
            events.append("tui.start")

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    class Carrier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            events.append("carrier.start")

        def stop(self) -> None:
            events.append("carrier.stop")

    monkeypatch.setenv("HARNESS_CODEX_BIN", "/test/bin/codex")
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr("hyprial.daemon.CodexInteractiveAppServer", Server)
    monkeypatch.setattr("hyprial.daemon.CodexInteractiveCarrier", Carrier)
    monkeypatch.setattr(_cli_subprocess, "Popen", Process)
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
    if guard_result != "confirmed":
        assert result.exit_code == 1
        expected = (
            "CODEX_RESUME_GUARD_UNSUPPORTED"
            if guard_result == "missing-method"
            else "CODEX_RESUME_GUARD_UNCONFIRMED"
        )
        assert json.loads(result.stdout)["code"] == expected
    else:
        assert result.exit_code == 0, result.output
    methods = [method for method, _ in daemon_calls]
    assert methods[0] == "agent.start.preflight"
    assert "agent.create" not in methods
    assert "session.register" not in methods
    assert methods.count("session.register_guarded") == 1
    registration = next(
        payload for method, payload in daemon_calls if method == "session.register_guarded"
    )
    assert registration["sessionRef"] == THREAD_ID
    assert registration["cwd"] == str(tmp_path.resolve())
    process = processes[0]
    assert process.argv[:3] == ["/test/bin/codex", "resume", THREAD_ID]
    assert events.index("thread.resume") < events.index("tui.start")
    if guard_result != "confirmed":
        assert process.terminated is True
    if guard_result == "missing-echo":
        assert "session.unregister" in methods


def test_interactive_codex_resume_requires_guard_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = False

    def daemon_request(method: str, _params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if method == "agent.start.preflight":
            return {"ok": True}
        raise AssertionError(method)

    def popen(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_subprocess, "Popen", popen)
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
    assert json.loads(result.stdout)["code"] == "CODEX_RESUME_GUARD_UNSUPPORTED"
    assert spawned is False
