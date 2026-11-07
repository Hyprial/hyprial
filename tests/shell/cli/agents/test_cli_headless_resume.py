"""The CLI side of headless resume: ``--resume`` becomes ``sessionRef``, nothing else."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from typer.testing import CliRunner

from hyprial.kernel import (
    LIFECYCLE_IPC_MARGIN_SECONDS,
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import shutil as _cli_shutil
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

PLAIN_START_WAIT = (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS
    + LIFECYCLE_WAIT_MARGIN_SECONDS
    + LIFECYCLE_IPC_MARGIN_SECONDS
)


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, dict[str, object], float | None]]:
    seen: list[tuple[str, dict[str, object], float | None]] = []

    def daemon(method: str, params: dict[str, object] | None = None, **kwargs: object) -> dict[str, object]:
        seen.append((method, dict(params or {}), kwargs.get("timeout")))  # type: ignore[arg-type]
        if method == "lifecycle.resume.preflight":
            return {"ok": True, "resumable": True, "agentSnapshot": {"before": True}}
        if method == "agent.create":
            return {
                "ok": True,
                "created": False,
                "agent": {"name": (params or {})["name"]},
                "resumePreviousSnapshot": {"before": True},
                "resumeSnapshot": {"after": True},
            }
        return {"ok": True, "id": "x", "sessionRef": (params or {}).get("sessionRef")}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_shutil, "which", lambda name: f"/usr/bin/{name}")
    return seen


@pytest.mark.parametrize("harness", ["claude", "pi", "codex"])
def test_headless_resume_sends_the_ref_and_waits_long_enough(calls, harness: str) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", harness, "--name", "wk", "--headless", "--resume", " sess-1 ", "--json"],
    )
    assert result.exit_code == 0, result.output
    method, params, timeout = calls[-1]
    assert method == "lifecycle.start"
    assert params["sessionRef"] == "sess-1"
    # The resume adds a readiness check and a possible undo operation; the IPC
    # wait must outlast both or a correct refusal arrives as IPC_TIMEOUT.
    assert timeout is not None and timeout > PLAIN_START_WAIT


def test_replace_interactive_switch_keeps_the_resume_ref(calls) -> None:
    """The explicit switch path sends replaceInteractive and the exact resume id."""

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "wk",
            "--headless",
            "--resume",
            "sess-switch",
            "--replace-interactive",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    method, params, _timeout = calls[-1]
    assert method == "lifecycle.start"
    assert params["replaceInteractive"] is True
    assert params["sessionRef"] == "sess-switch"


def test_resume_handoff_rejects_a_different_actual_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start result that claims a different actual session is not success.

    The check compares the caller's explicit --resume value with the daemon's
    actual session, never with the harness row's current ref (which 708 keeps
    equal and would make the check vacuous).
    """

    seen: list[str] = []

    def daemon(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(method)
        if method == "lifecycle.resume.preflight":
            return {"ok": True, "resumable": True, "agentSnapshot": {"before": True}}
        if method == "agent.create":
            return {
                "ok": True,
                "created": False,
                "agent": {"name": "wk"},
                "resumePreviousSnapshot": {"before": True},
                "resumeSnapshot": {"after": True},
            }
        if method == "lifecycle.start":
            return {"ok": True, "actor": "agent:op:node:wk", "sessionRef": "different"}
        if method == "identity.whoami":
            return {"ok": True, "sessionRegistered": False}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_shutil, "which", lambda name: f"/usr/bin/{name}")

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "pi", "--name", "wk", "--headless", "--resume", "wanted", "--json"],
    )

    assert result.exit_code != 0
    assert "STRICT_RESUME_FAILED" in result.output
    assert "identity.whoami" not in seen or "lifecycle.start" in seen


def test_resume_handoff_uses_the_daemon_actual_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI trusts the daemon's established ref and never calls whoami."""

    def daemon(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if method == "lifecycle.resume.preflight":
            return {"ok": True, "resumable": True, "agentSnapshot": {"before": True}}
        if method == "agent.create":
            return {
                "ok": True,
                "created": False,
                "agent": {"name": "wk"},
                "resumePreviousSnapshot": {"before": True},
                "resumeSnapshot": {"after": True},
            }
        if method == "lifecycle.start":
            return {"ok": True, "actor": "agent:op:node:wk", "sessionRef": "wanted"}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_shutil, "which", lambda name: f"/usr/bin/{name}")

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "pi", "--name", "wk", "--headless", "--resume", "wanted", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["sessionRef"] == "wanted"


def test_replace_interactive_without_resume_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "pi", "--name", "wk", "--headless", "--replace-interactive", "--json"],
    )
    assert result.exit_code != 0
    assert "requires --resume" in result.output


@pytest.mark.parametrize("json_output", [True, False])
def test_headless_start_warning_is_in_json_or_stderr(calls, monkeypatch, json_output):
    warning = "Codex skills/list reported loader errors: missing YAML frontmatter delimited by ---"
    original = _cli_support._daemon_request

    def daemon(method, params=None, **kwargs):
        result = original(method, params, **kwargs)
        if method == "lifecycle.start":
            result["warnings"] = [warning]
        return result

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    argv = ["agent", "start", "codex", "--name", "squire", "--headless"]
    result = CliRunner().invoke(_cli_root_app, argv + (["--json"] if json_output else []))
    assert result.exit_code == 0, result.output
    if json_output:
        assert json.loads(result.stdout)["warnings"] == [warning]
    else:
        assert warning in result.stderr


def test_a_plain_headless_start_is_unchanged(calls) -> None:
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "pi", "--name", "wk", "--headless", "--json"])
    assert result.exit_code == 0, result.output
    method, params, timeout = calls[-1]
    assert "sessionRef" not in params
    assert timeout == PLAIN_START_WAIT


@pytest.mark.parametrize("harness", ["claude", "pi", "codex"])
def test_headless_unknown_resume_keeps_existing_agent_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path, harness: str
) -> None:
    existing = {
        "cwd": "/work/original",
        "provider": "openai",
        "model": "gpt-6-astra",
        "capabilities": {"harness": harness, "tier": "super"},
        "pinnedAdapters": ["adapter-a"],
    }
    before = deepcopy(existing)

    def daemon_request(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if method == "lifecycle.resume.preflight":
            raise _cli_get_services().CliError(
                "RESUME_SESSION_NOT_FOUND", "requested native session does not exist"
            )
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_shutil, "which", lambda _name: "/test/bin/harness")
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            harness,
            "--name",
            "alice",
            "--headless",
            "--resume",
            "unknown-session",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert existing == before


@pytest.mark.parametrize("harness", ["claude", "pi", "codex"])
def test_headless_resume_preflight_to_start_race_restores_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path, harness: str
) -> None:
    """A session can disappear after read-only preflight and before start."""

    existing = {
        "cwd": "/work/original",
        "provider": "openai",
        "model": "gpt-6-astra",
        "capabilities": {"harness": harness, "tier": "super"},
        "pinnedAdapters": ["adapter-a"],
    }
    concurrent = deepcopy(existing)
    concurrent["harnessArgs"] = {"codex": ["--legitimate-concurrent-write"]}

    def daemon_request(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        payload = dict(params or {})
        if method == "lifecycle.resume.preflight":
            return {
                "ok": True,
                "resumable": True,
                "agentSnapshot": deepcopy(existing),
            }
        if method == "agent.create":
            existing.clear()
            existing.update(deepcopy(concurrent))
            replaced = deepcopy(existing)
            existing["cwd"] = payload["cwd"]
            existing["provider"] = payload.get("provider")
            existing["model"] = payload.get("model")
            existing["capabilities"] = {
                "harness": payload["harness"],
                "tier": None,
            }
            return {
                "ok": True,
                "created": False,
                "agent": deepcopy(existing),
                "resumePreviousSnapshot": replaced,
                "resumeSnapshot": deepcopy(existing),
            }
        if method == "lifecycle.start":
            raise _cli_get_services().CliError(
                "RESUME_SESSION_NOT_FOUND",
                "native transcript disappeared after successful preflight",
            )
        if method == "agent.resume.restore":
            existing.clear()
            existing.update(deepcopy(payload["replacement"]))
            return {"ok": True, "restored": True}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_shutil, "which", lambda _name: "/test/bin/harness")

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            harness,
            "--name",
            "alice",
            "--headless",
            "--resume",
            "existing-then-removed",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert existing == concurrent


def test_headless_resume_rollback_failure_does_not_mask_the_start_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """D3: the restore is a compensating write, not a second start attempt.

    When the rollback itself fails its error must NOT replace the start
    refusal that made the rollback necessary.  The original code/message/data
    stay primary and the rollback failure rides along as additive context.
    """

    def daemon_request(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if method == "lifecycle.resume.preflight":
            return {"ok": True, "resumable": True, "agentSnapshot": {"old": True}}
        if method == "agent.create":
            return {
                "ok": True,
                "created": False,
                "agent": {"name": "alice"},
                "resumePreviousSnapshot": {"old": True},
                "resumeSnapshot": {"new": True},
            }
        if method == "lifecycle.start":
            raise _cli_get_services().CliError(
                "STRICT_RESUME_FAILED",
                "resume of pi session 'sess-1' did not hold",
                {"harness": "pi", "name": "alice", "sessionRef": "sess-1"},
            )
        if method == "agent.resume.restore":
            raise _cli_get_services().CliError(
                "RESUME_AGENT_RESTORE_FAILED",
                "Agent restore was refused because the Agent changed",
                {"reason": "stale-snapshot"},
            )
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_shutil, "which", lambda _name: "/test/bin/harness")

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start", "pi", "--name", "alice", "--headless", "--resume", "sess-1",
            "--cwd", str(tmp_path), "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    # Primary error is the original start refusal, code and data intact.
    assert payload["code"] == "STRICT_RESUME_FAILED"
    assert "did not hold" in payload["error"]
    assert payload["data"]["harness"] == "pi"
    assert payload["data"]["sessionRef"] == "sess-1"
    # The rollback failure is attached, not substituted.
    restore = payload["data"]["restoreError"]
    assert restore["code"] == "RESUME_AGENT_RESTORE_FAILED"
    assert "Agent changed" in restore["message"]


def test_headless_resume_reports_new_agent_without_rollback_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[str] = []

    def daemon_request(method: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(method)
        if method == "lifecycle.resume.preflight":
            return {"ok": True, "resumable": True, "agentSnapshot": {"old": True}}
        if method == "agent.create":
            return {
                "ok": True,
                "created": True,
                "agent": {"name": "alice"},
                "resumePreviousSnapshot": None,
                "resumeSnapshot": {"new": True},
            }
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_shutil, "which", lambda _name: "/test/bin/harness")

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "codex",
            "--name",
            "alice",
            "--headless",
            "--resume",
            "thread-1",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert "RESUME_AGENT_RESTORE_FAILED" in result.output
    assert calls == ["lifecycle.resume.preflight", "agent.create"]


@pytest.mark.parametrize(
    "argv",
    [
        ["agent", "start", "dsh", "--name", "wk", "--headless", "--resume", "s"],
        ["agent", "start", "claude", "--name", "wk", "--headless", "--resume", "  "],
    ],
)
def test_unsupported_resume_is_refused_before_contacting_the_daemon(calls, argv) -> None:
    result = CliRunner().invoke(_cli_root_app, [*argv, "--json"])
    assert result.exit_code != 0
    assert calls == []
