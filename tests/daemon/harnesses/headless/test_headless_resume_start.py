"""``hyprial agent start <claude|pi|codex> --headless --resume <id>``: that session, or a refusal.

Two gates, two codes, never the shape of any other failure:

* before anything starts -- the harness's transcript for the id must exist
  in the HOME the daemon hands its children, else ``RESUME_SESSION_NOT_FOUND``
  and nothing is created.  This gate is the one that holds for pi: given an
  unknown ``--session-id`` pi starts a FRESH session under that very id, so the
  id the worker reports afterwards matches and proves nothing;
* after the start -- the worker must be ready on the requested id, else
  ``STRICT_RESUME_FAILED`` and the worker is stopped (no fresh session left
  running under the name).

Without ``sessionRef`` the default is unchanged: a fresh session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.runtime.context import (
    resolve_agent_runtime_context,
)
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.transfer.archive.session_files import (
    claude_project_dir_name,
    pi_session_dir_name,
)
from tests.integration.transfer.lifecycle.test_transfer_daemon import _application, _StubLauncher
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

NODE = "server"
CWD = "/work"


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The transfer tests' application: real runtime + desired state, stub launcher."""

    apps = []
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.setattr(
        "tests.daemon.harnesses.headless.test_headless_resume_start.CWD",
        str(workspace),
    )

    def factory(node_id: str, launcher: _StubLauncher | None = None):
        app = _application(tmp_path / "state", node_id, launcher)
        apps.append(app)
        return app

    (tmp_path / "state").mkdir()
    yield factory
    for app in apps:
        app.agents.close()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The daemon's HOME (where harnesses keep transcripts), isolated."""

    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    return path


def _pi_transcript(home: Path, session_id: str) -> Path:
    directory = home / ".pi" / "agent" / "sessions" / pi_session_dir_name(CWD)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"2026-09-23T00-00-00-000Z_{session_id}.jsonl"
    path.write_text(json.dumps({"type": "session", "id": session_id}) + "\n", encoding="utf-8")
    return path


def _claude_transcript(home: Path, session_id: str) -> Path:
    directory = home / ".claude" / "projects" / claude_project_dir_name(CWD)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def _start(app, harness: str = "pi", **extra: object) -> dict[str, object]:
    return app.handle(
        "lifecycle.start",
        {"provider": harness, "name": "wk", "headless": True, "cwd": CWD, **extra},
    )


def test_codex_start_returns_nonfatal_skill_warnings(make_app, home):
    warning = "Codex skills/list reported loader errors: missing YAML frontmatter delimited by ---"

    class Launcher(_StubLauncher):
        def start(self, spec):
            process = super().start(spec)
            process.startup_warnings = (warning,)
            return process

    launcher = Launcher()
    app = make_app(NODE, launcher)
    result = _start(app, harness="codex")
    assert result["ok"] is True
    assert result["warnings"] == [warning]
    assert launcher.processes[("codex", "wk")].running
    assert "error" not in result


def test_a_found_session_is_resumed_and_reported(make_app, home: Path) -> None:
    _pi_transcript(home, "session-1")
    launcher = _StubLauncher()
    app = make_app(NODE, launcher)

    result = _start(app, sessionRef="session-1")

    assert result["sessionRef"] == "session-1"
    assert app.desired_state.load().harnesses[0].session_ref == "session-1"
    assert launcher.processes[("pi", "wk")].stopped is False


def test_resume_preflight_is_read_only(make_app, home: Path) -> None:
    launcher = _StubLauncher()
    app = make_app(NODE, launcher)
    app.handle(
        "agent.create",
        {
            "name": "wk",
            "harness": "pi",
            "runtime": "headless",
            "cwd": CWD,
        },
    )
    before = app.agents.require("wk")
    transcript_dir = home.parent / "agents" / "wk" / "state" / "pi"
    transcript_dir.mkdir(parents=True)
    transcript_dir.chmod(0o700)
    (transcript_dir / "2026-10-06T00-00-00-000Z_session-1.jsonl").write_text(
        json.dumps({"type": "session", "id": "session-1"}) + "\n",
        encoding="utf-8",
    )

    result = app.handle(
        "lifecycle.resume.preflight",
        {
            "provider": "pi",
            "name": "wk",
            "headless": True,
            "cwd": CWD,
            "sessionRef": "session-1",
        },
    )

    assert result == {
        "ok": True,
        "resumable": True,
        "harness": "pi",
        "name": "wk",
        "sessionRef": "session-1",
        "agentSnapshot": before.to_json(),
    }
    assert app.agents.require("wk") == before
    assert launcher.started == []
    assert app.desired_state.load().harnesses == ()


def test_a_missing_pi_session_is_refused_before_anything_starts(
    make_app, home: Path
) -> None:
    """The discriminating case: pi would 'resume' an unknown id by making it."""

    launcher = _StubLauncher()
    app = make_app(NODE, launcher)

    with pytest.raises(DaemonRequestError) as failure:
        _start(app, sessionRef="session-1")

    assert failure.value.code == "RESUME_SESSION_NOT_FOUND"
    assert failure.value.data["reason"] == "not-found"
    assert launcher.started == []
    assert app.desired_state.load().harnesses == ()


def test_the_pre_check_reads_pi_agent_dir_like_pi_does(
    make_app, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_dir = tmp_path / "pi-agent"
    directory = agent_dir / "sessions" / pi_session_dir_name(CWD)
    directory.mkdir(parents=True)
    (directory / "x_session-1.jsonl").write_text(
        json.dumps({"type": "session", "id": "session-1"}) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    app = make_app(NODE, _StubLauncher())

    assert _start(app, sessionRef="session-1")["sessionRef"] == "session-1"


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_p2_resume_reads_the_authorized_session_root_not_host_home(
    make_app, home: Path, tmp_path: Path, harness: str
) -> None:
    config = tmp_path / f"{harness}-config"
    config.mkdir()
    launcher = _StubLauncher()
    app = make_app(NODE, launcher)
    app.agents.create(
        "wk",
        cwd=CWD,
        config={
            "sources": [{"path": str(config), "required": True}],
            "discovery": "explicit-only",
        },
    )
    context = resolve_agent_runtime_context(
        registry=app._agent_registry,
        agent_name="wk",
        harness=harness,
        cwd=CWD,
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    session_ref = "p2-session"
    if harness == "claude":
        transcript = (
            context.roots.session_root
            / claude_project_dir_name(CWD)
            / f"{session_ref}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("{}\n", encoding="utf-8")
    elif harness == "codex":
        transcript = (
            context.roots.session_root
            / "2026"
            / "09"
            / "24"
            / f"rollout-2026-09-24T00-00-00-{session_ref}.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    else:
        transcript = (
            context.roots.session_root
            / f"2026-09-24T00-00-00-000Z_{session_ref}.jsonl"
        )
        transcript.write_text(
            json.dumps({"type": "session", "id": session_ref}) + "\n",
            encoding="utf-8",
        )

    result = _start(app, harness, sessionRef=session_ref)

    assert result["sessionRef"] == session_ref
    assert transcript.is_file()
    assert not transcript.is_relative_to(home)


def test_a_claude_session_that_did_not_hold_stops_the_worker(
    make_app, home: Path
) -> None:
    """claude's dead-target fallback: a fresh uuid.  Refused and stopped."""

    _claude_transcript(home, "6b0c2f7e-0000-4000-8000-000000000001")
    launcher = _StubLauncher()
    launcher.ref_override = "fresh-cold-start-id"
    app = make_app(NODE, launcher)

    with pytest.raises(DaemonRequestError) as failure:
        _start(app, "claude", sessionRef="6b0c2f7e-0000-4000-8000-000000000001")

    assert failure.value.code == "STRICT_RESUME_FAILED"
    assert failure.value.data["established"] == "fresh-cold-start-id"
    assert launcher.processes[("claude", "wk")].stopped is True
    assert app.desired_state.load().harnesses == ()


def test_a_resumed_worker_that_never_becomes_ready_is_stopped(
    make_app, home: Path
) -> None:
    _pi_transcript(home, "session-1")
    launcher = _StubLauncher()
    launcher.not_ready = True
    app = make_app(NODE, launcher)

    with pytest.raises(DaemonRequestError) as failure:
        _start(app, sessionRef="session-1")

    assert failure.value.code == "STRICT_RESUME_FAILED"
    assert launcher.processes[("pi", "wk")].stopped is True


def test_a_start_failure_carries_the_workers_own_last_error(
    make_app, home: Path
) -> None:
    """D3: the real launch refusal must reach the caller, additively.

    A harness can refuse on its own connect thread (pi without ``--approve``
    is the measured case), so ``lifecycle.start`` answers a generic
    ``STRICT_RESUME_FAILED`` while the concrete reason sits on the worker's
    ``last_error``.  The refusal travels as ``data.lastError`` -- the outer
    code is untouched -- instead of being hidden behind the generic code.
    """

    _pi_transcript(home, "session-1")
    launcher = _StubLauncher()
    launcher.not_ready = True
    launcher.start_error = (
        "Pi P2 headless launch requires explicit --approve trust"
    )
    app = make_app(NODE, launcher)

    with pytest.raises(DaemonRequestError) as failure:
        _start(app, sessionRef="session-1")

    assert failure.value.code == "STRICT_RESUME_FAILED"
    assert failure.value.data["lastError"] == (
        "Pi P2 headless launch requires explicit --approve trust"
    )


def test_a_start_failure_last_error_is_redacted(make_app, home: Path) -> None:
    """D3: only the static reason travels; credential-shaped text does not."""

    _pi_transcript(home, "session-1")
    launcher = _StubLauncher()
    launcher.not_ready = True
    launcher.start_error = (
        "launch refused; retry via https://alice:s3cret@example.test/run?token=abc"
    )
    app = make_app(NODE, launcher)

    with pytest.raises(DaemonRequestError) as failure:
        _start(app, sessionRef="session-1")

    detail = failure.value.data["lastError"]
    assert "s3cret" not in detail
    assert "token=abc" not in detail
    assert "[REDACTED_URL]" in detail


def test_without_a_session_ref_the_default_is_still_a_fresh_session(
    make_app, home: Path
) -> None:
    """Control: no transcript, no ref -> starts, no resume checks at all."""

    launcher = _StubLauncher()
    launcher.ref_override = "whatever-the-harness-minted"
    app = make_app(NODE, launcher)

    result = _start(app)

    assert "sessionRef" not in result
    assert launcher.started == [("pi", "wk")]


def test_resume_is_refused_for_a_harness_without_resumable_sessions(
    make_app, home: Path
) -> None:
    """dsh has no resume by design (#190); interactive specs never reach here."""

    launcher = _StubLauncher()
    app = make_app(NODE, launcher)
    with pytest.raises(DaemonRequestError) as failure:
        _start(app, "dsh", sessionRef="s")
    assert failure.value.code == "INVALID_ARGUMENT"
    assert launcher.started == []
