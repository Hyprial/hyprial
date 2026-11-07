"""The claude protocol double must be consumable by the product's own seam.

``tests/fixtures/harnesses/fake_claude_sdk.py`` is the repository's JSONL
double for the claude Agent SDK worker wire.  These tests prove three things
the first-proof e2e depends on, without importing claude-agent-sdk or Claude
Code: the double answers the exact wire the product owns (run under
``python -I -S -B`` so it cannot lean on site-packages), the product's own
``IsolatedAgentSdkClient`` drives it end to end, and the daemon's
``HYPRIAL_BUNDLED_SDK_BOOTSTRAP`` injection seam resolves to it unchanged.

What this does NOT prove is stated in the fixture's header: the real vendor
binary, the real SDK and the real credential remain NOT_RUN (TC012 BLOCKED).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import (
    IsolatedAgentSdkClient,
    sdk_worker_command,
)

REPO = Path(__file__).resolve().parents[5]
FAKE = REPO / "tests/fixtures/harnesses/fake_claude_sdk.py"


def _run_wire(
    requests: list[dict[str, object]],
    *,
    mode: str = "settled",
    extra_env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    """Run the double exactly as the product would: isolated, model-free."""

    environment = dict(os.environ)
    environment["FAKE_CLAUDE_MODE"] = mode
    environment.pop("FAKE_CLAUDE_LOG", None)
    environment.pop("HYPRIAL_AGENT_SDK_OPTIONS", None)
    environment.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(FAKE)],
        env=environment,
        input="".join(json.dumps(row) + "\n" for row in requests),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _frames(done: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines()]


def test_the_double_answers_the_worker_wire_without_site_packages() -> None:
    frames = _frames(_run_wire([{"op": "query", "prompt": "hello"}, {"op": "stop"}]))
    assert frames[0]["type"] == "ready"
    assert isinstance(frames[0]["sessionId"], str) and frames[0]["sessionId"]
    assert frames[1] == {"type": "result", "result": "hello done", "isError": False}


def test_the_double_echoes_a_stored_resume_session() -> None:
    options = json.dumps({"resume": "stored-session"})
    frames = _frames(
        _run_wire(
            [{"op": "stop"}],
            extra_env={"HYPRIAL_AGENT_SDK_OPTIONS": options},
        )
    )
    assert frames[0] == {"type": "ready", "sessionId": "stored-session"}


def test_the_double_pins_a_session_id_when_asked() -> None:
    frames = _frames(
        _run_wire([{"op": "stop"}], extra_env={"FAKE_CLAUDE_SESSION_ID": "pinned-1"})
    )
    assert frames[0] == {"type": "ready", "sessionId": "pinned-1"}


def test_progress_frames_precede_the_terminal_result() -> None:
    frames = _frames(
        _run_wire([{"op": "query", "prompt": "x"}, {"op": "stop"}], mode="progress")
    )
    assert [frame["type"] for frame in frames] == [
        "ready",
        "progress",
        "progress",
        "progress",
        "result",
    ]
    assert frames[-1] == {"type": "result", "result": "x done", "isError": False}
    assert frames[-2]["terminal"] is True


def test_error_mode_reports_is_error() -> None:
    frames = _frames(
        _run_wire([{"op": "query", "prompt": "x"}, {"op": "stop"}], mode="error")
    )
    result = next(frame for frame in frames if frame["type"] == "result")
    assert result["isError"] is True


def test_the_command_log_preserves_order(tmp_path: Path) -> None:
    log = tmp_path / "commands.jsonl"
    _frames(
        _run_wire(
            [{"op": "query", "prompt": "a"}, {"op": "interrupt"}, {"op": "stop"}],
            extra_env={"FAKE_CLAUDE_LOG": str(log)},
        )
    )
    operations = [json.loads(line)["op"] for line in log.read_text().splitlines()]
    assert operations == ["query", "interrupt", "stop"]


def test_probe_mode_answers_model_free() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(FAKE), "--probe"],
        env={**os.environ, "FAKE_CLAUDE_MODE": "settled"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip()) == {
        "type": "probe",
        "client": "fake-claude-sdk",
    }


def test_no_ready_mode_fails_closed() -> None:
    done = _run_wire([], mode="no-ready")
    assert done.returncode == 1
    assert done.stdout.strip() == ""


@pytest.mark.anyio
async def test_the_product_client_drives_the_double_end_to_end() -> None:
    """The real parent process consumes the double, session ref included."""

    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    established: list[str] = []
    client = IsolatedAgentSdkClient(
        spec,
        worker_command=(sys.executable, "-I", "-S", "-B", str(FAKE)),
        env={"FAKE_CLAUDE_MODE": "progress", "FAKE_CLAUDE_SESSION_ID": "wire-1"},
        on_session_established=established.append,
        startup_timeout_seconds=15.0,
    )

    async with client:
        await client.query("work")
        messages = [message async for message in client.receive_response()]

    assert established == ["wire-1"]
    assert [type(message).__name__ for message in messages] == [
        "ProgressObservation",
        "ProgressObservation",
        "ProgressObservation",
        "_WireResult",
    ]
    assert messages[-1].result == "work done"
    assert not messages[-1].is_error


def test_the_bundled_sdk_bootstrap_seam_resolves_to_the_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon-side injection seam the first-proof e2e will use."""

    monkeypatch.setenv("HYPRIAL_BUNDLED_SDK_BOOTSTRAP", str(FAKE))
    assert sdk_worker_command() == (sys.executable, "-I", "-S", "-B", str(FAKE))
