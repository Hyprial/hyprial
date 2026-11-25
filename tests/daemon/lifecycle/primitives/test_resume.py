# Decision record (file header; no inline comments by repository rule):
# - Agent teleport slice 3: strict resume and the resumable-session pre-check
#   become units callable with plain arguments, so the teleport target can run
#   them without a ``transfer.receive`` params dict or a daemon application.
# - ``strict_resume`` reads only ``wait_ready`` and ``session_refs`` from the
#   harness runtime.  It never stops, removes or undoes anything: compensation
#   stays with each caller (receive removes, lifecycle start deactivates).
# - Its refusal is the existing ``STRICT_RESUME_FAILED`` ``DaemonRequestError``
#   carrying the data shape ``_verify_started_resume`` already publishes:
#   harness, name, sessionRef (expected) and established (None when the worker
#   never became ready or established no ref).
# - ``require_resumable_session`` takes an already-resolved cwd and optional
#   runtime context; it does not read the process environment or the real
#   home.  The daemon passes ``Path.home()`` and ``PI_CODING_AGENT_DIR``
#   explicitly, which keeps these tests isolated from the operator's machine.
"""Direct tests for the extracted strict-resume and resume pre-check units."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.harnesses.pi.session import pi_session_id
from hyprial.daemon.impl.transfer.archive.session_files import pi_session_dir_name


class _Harnesses:
    """Scripted readiness and established refs; records every read."""

    def __init__(
        self,
        *,
        ready: bool,
        refs: Mapping[tuple[str, str, str], str] | Mapping[tuple[str, str], str],
    ) -> None:
        self._ready = ready
        self._refs = dict(refs)
        self.waits: list[tuple[str, str, float]] = []
        self.ref_reads = 0

    def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
        self.waits.append((harness, name, timeout))
        return self._ready

    def session_refs(self) -> dict[tuple[str, str, str], str] | dict[tuple[str, str], str]:
        self.ref_reads += 1
        return dict(self._refs)


def _strict(harnesses: _Harnesses, session_ref: str = "session-1") -> str:
    from hyprial.daemon.impl.lifecycle.primitives import strict_resume

    return strict_resume(
        harnesses, harness="pi", name="wk", session_ref=session_ref, timeout=5.0
    )


def test_strict_resume_returns_the_established_ref_when_it_holds() -> None:
    harnesses = _Harnesses(ready=True, refs={("pi", "wk", "wk"): "session-1"})

    assert _strict(harnesses) == "session-1"
    assert harnesses.waits == [("pi", "wk", 5.0)]


def test_strict_resume_refuses_a_different_established_ref() -> None:
    harnesses = _Harnesses(
        ready=True, refs={("pi", "wk", "wk"): "fresh-cold-start-id"}
    )

    with pytest.raises(DaemonRequestError) as refusal:
        _strict(harnesses)

    assert refusal.value.code == ipc_errors.STRICT_RESUME_FAILED
    assert refusal.value.data == {
        "harness": "pi",
        "name": "wk",
        "sessionRef": "session-1",
        "established": "fresh-cold-start-id",
    }
    assert "session-1" in str(refusal.value)
    assert "fresh-cold-start-id" in str(refusal.value)


def test_strict_resume_refuses_a_worker_that_established_no_ref() -> None:
    harnesses = _Harnesses(ready=True, refs={("pi", "other", "other"): "session-1"})

    with pytest.raises(DaemonRequestError) as refusal:
        _strict(harnesses)

    assert refusal.value.code == ipc_errors.STRICT_RESUME_FAILED
    assert refusal.value.data["established"] is None


def test_strict_resume_refuses_a_worker_that_never_became_ready() -> None:
    harnesses = _Harnesses(ready=False, refs={("pi", "wk", "wk"): "session-1"})

    with pytest.raises(DaemonRequestError) as refusal:
        _strict(harnesses)

    assert refusal.value.code == ipc_errors.STRICT_RESUME_FAILED
    assert refusal.value.data["established"] is None
    assert harnesses.ref_reads == 0


def test_strict_resume_selects_the_requested_member() -> None:
    harnesses = _Harnesses(
        ready=True,
        refs={
            ("pi", "wk", "member-a"): "session-a",
            ("pi", "wk", "member-b"): "session-b",
        },
    )

    from hyprial.daemon.impl.lifecycle.primitives import strict_resume

    assert (
        strict_resume(
            harnesses,
            harness="pi",
            name="wk",
            member="member-b",
            session_ref="session-b",
            timeout=5.0,
        )
        == "session-b"
    )


def test_strict_resume_adapts_legacy_name_scoped_refs() -> None:
    harnesses = _Harnesses(ready=True, refs={("pi", "wk"): "session-1"})

    assert _strict(harnesses) == "session-1"


def _require(tmp_path: Path, **overrides: object) -> Path:
    from hyprial.daemon.impl.lifecycle.primitives import require_resumable_session

    arguments: dict[str, object] = {
        "harness": "pi",
        "name": "wk",
        "headless": True,
        "cwd": str(tmp_path / "work"),
        "session_ref": "session-1",
        "home": tmp_path / "home",
        "pi_agent_dir": None,
        "runtime_context": None,
    }
    arguments.update(overrides)
    return require_resumable_session(**arguments)


def _pi_transcript(agent_dir: Path, cwd: Path, session_ref: str, header_id: str | None = None) -> Path:
    sanitized = pi_session_id(session_ref)
    session_dir = agent_dir / "sessions" / pi_session_dir_name(str(cwd))
    session_dir.mkdir(parents=True)
    path = session_dir / f"2026-11-12_{sanitized}.jsonl"
    header = {"type": "session", "id": header_id if header_id is not None else sanitized}
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    (tmp_path / "home").mkdir()
    return tmp_path / "work"


def test_precheck_finds_a_pi_transcript_under_the_given_agent_dir(
    tmp_path: Path, workspace: Path
) -> None:
    agent_dir = tmp_path / "pi-agent"
    expected = _pi_transcript(agent_dir, workspace, "session-1")

    located = _require(tmp_path, pi_agent_dir=agent_dir)

    assert located == expected


def test_precheck_finds_a_pi_transcript_under_the_given_home(
    tmp_path: Path, workspace: Path
) -> None:
    expected = _pi_transcript(tmp_path / "home" / ".pi" / "agent", workspace, "session-1")

    located = _require(tmp_path)

    assert located == expected


def test_precheck_refuses_a_missing_pi_transcript(tmp_path: Path, workspace: Path) -> None:
    with pytest.raises(DaemonRequestError) as refusal:
        _require(tmp_path, pi_agent_dir=tmp_path / "pi-agent")

    assert refusal.value.code == ipc_errors.RESUME_SESSION_NOT_FOUND
    assert refusal.value.data == {
        "harness": "pi",
        "name": "wk",
        "sessionRef": "session-1",
        "cwd": str(workspace),
        "reason": "not-found",
    }
    assert "nothing was started" in str(refusal.value)


def test_precheck_refuses_a_missing_claude_transcript(tmp_path: Path, workspace: Path) -> None:
    with pytest.raises(DaemonRequestError) as refusal:
        _require(tmp_path, harness="claude")

    assert refusal.value.code == ipc_errors.RESUME_SESSION_NOT_FOUND
    assert refusal.value.data["harness"] == "claude"
    assert refusal.value.data["reason"] == "not-found"


def test_precheck_reports_an_unusable_transcript(tmp_path: Path, workspace: Path) -> None:
    agent_dir = tmp_path / "pi-agent"
    _pi_transcript(agent_dir, workspace, "session-1", header_id="someone-else")

    with pytest.raises(DaemonRequestError) as refusal:
        _require(tmp_path, pi_agent_dir=agent_dir)

    assert refusal.value.code == ipc_errors.RESUME_SESSION_NOT_FOUND
    assert refusal.value.data["reason"] == "unusable"


def test_precheck_does_not_read_the_ambient_pi_agent_dir(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient = tmp_path / "ambient-pi-agent"
    _pi_transcript(ambient, workspace, "session-1")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(ambient))

    with pytest.raises(DaemonRequestError) as refusal:
        _require(tmp_path)

    assert refusal.value.code == ipc_errors.RESUME_SESSION_NOT_FOUND


@pytest.mark.parametrize(
    ("harness", "headless"),
    [("jev", True), ("pi", False)],
)
def test_precheck_refuses_a_harness_that_cannot_resume(
    tmp_path: Path, workspace: Path, harness: str, headless: bool
) -> None:
    with pytest.raises(DaemonRequestError) as refusal:
        _require(tmp_path, harness=harness, headless=headless)

    assert refusal.value.code == ipc_errors.INVALID_ARGUMENT
