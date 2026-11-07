from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from tests.support.runtime._daemon_diagnostics import DAEMON_LOG_RECORD_LIMIT, DAEMON_TIMEOUT_EVIDENCE_LABEL, DIAGNOSTIC_FILE_MAX_BYTES, daemon_log_summary, thread_stack_locations, write
from hyprial.kernel import (
    DAEMON_STARTUP_PHASES,
    DaemonStartupPhase,
)
import hyprial.daemon.impl.processes.process_diagnostics as process_diagnostics
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.daemon_start as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _assert_envelope_shape(evidence: dict[str, object]) -> None:
    assert set(evidence) == {
        "lastStartupPhase",
        "phasesSeen",
        "elapsedSeconds",
        "child",
    }
    assert evidence["lastStartupPhase"] in {
        None,
        "unknown",
        *DAEMON_STARTUP_PHASES,
    }
    phases_seen = evidence["phasesSeen"]
    assert isinstance(phases_seen, int) and not isinstance(phases_seen, bool)
    assert 0 <= phases_seen <= len(DAEMON_STARTUP_PHASES) + 1
    elapsed = evidence["elapsedSeconds"]
    assert isinstance(elapsed, float) and elapsed >= 0
    child = evidence["child"]
    assert isinstance(child, dict)
    assert set(child) == {"alive", "exitCode", "cpuSeconds"}
    assert isinstance(child["alive"], bool)
    assert child["exitCode"] is None or (
        isinstance(child["exitCode"], int)
        and not isinstance(child["exitCode"], bool)
    )
    assert child["cpuSeconds"] is None or (
        isinstance(child["cpuSeconds"], float) and child["cpuSeconds"] >= 0
    )


def test_startup_phase_allowlist_is_the_non_empty_daemon_enum() -> None:
    assert DAEMON_STARTUP_PHASES
    assert DAEMON_STARTUP_PHASES == frozenset(
        phase.value for phase in DaemonStartupPhase
    )


def test_startup_phase_summary_is_current_bounded_and_closed(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    spawned_at = datetime.now(UTC)
    stale = spawned_at - timedelta(seconds=1)
    current = spawned_at + timedelta(milliseconds=1)
    (logs / "daemon.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": stale.isoformat(),
                        "event": "daemon.start.begin",
                        "phase": DaemonStartupPhase.ACTOR_RUNTIME.value,
                    }
                ),
                json.dumps(
                    {
                        "ts": current.isoformat(),
                        "event": "daemon.start.begin",
                        "phase": DaemonStartupPhase.IPC_SERVER.value,
                    }
                ),
                json.dumps(
                    {
                        "ts": current.isoformat(),
                        "event": "daemon.start.begin",
                        "phase": "not-admitted",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _cli_owner_1._daemon_startup_phase_summary(tmp_path, spawned_at=spawned_at) == {
        "lastStartupPhase": "unknown",
        "phasesSeen": 2,
    }


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="CPU evidence is specified for Linux and macOS",
)
def test_child_evidence_distinguishes_sleep_spin_and_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    spinner = subprocess.Popen(
        [sys.executable, "-c", "while True: pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        samples = {sleeper.pid: 0.01, spinner.pid: 1.0}
        monkeypatch.setattr(
            _cli_get_services(), "process_cpu_seconds", lambda pid: samples.get(pid)
        )
        time.sleep(1.1)
        sleeping = _cli_owner_1._daemon_startup_failure_evidence(
            sleeper,
            state_dir=tmp_path,
            spawned_at=started_at,
            started_monotonic=started_monotonic,
        )
        spinning = _cli_owner_1._daemon_startup_failure_evidence(
            spinner,
            state_dir=tmp_path,
            spawned_at=started_at,
            started_monotonic=started_monotonic,
        )
        for evidence in (sleeping, spinning):
            _assert_envelope_shape(evidence)
            assert evidence["lastStartupPhase"] is None
            assert evidence["phasesSeen"] == 0
            assert evidence["child"]["alive"] is True  # type: ignore[index]
            assert evidence["child"]["exitCode"] is None  # type: ignore[index]
            assert evidence["child"]["cpuSeconds"] is not None  # type: ignore[index]
        assert spinning["child"]["cpuSeconds"] > sleeping["child"]["cpuSeconds"]  # type: ignore[index,operator]
    finally:
        for process in (sleeper, spinner):
            process.terminate()
            process.wait(timeout=5)

    exited = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert exited.wait(timeout=5) == 3
    dead = _cli_owner_1._daemon_startup_failure_evidence(
        exited,
        state_dir=tmp_path,
        spawned_at=started_at,
        started_monotonic=started_monotonic,
    )
    _assert_envelope_shape(dead)
    assert dead["child"] == {"alive": False, "exitCode": 3, "cpuSeconds": None}


def test_macos_cpu_probe_is_one_ps_call_capped_at_registered_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "  01:02.50\n", "")

    monkeypatch.setattr(process_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(process_diagnostics.subprocess, "run", run)

    assert process_diagnostics.process_cpu_seconds(4242) == 62.5
    assert calls == [
        (
            ["ps", "-o", "cputime=", "-p", "4242"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 1.0,
                "check": False,
            },
        )
    ]


def test_thread_stack_names_the_event_blocking_frame() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_on_event() -> None:
        entered.set()
        release.wait()

    thread = threading.Thread(target=blocked_on_event)
    thread.start()
    try:
        assert entered.wait(1.0)
        locations = thread_stack_locations(thread)
        assert locations
        assert any(location.endswith(":blocked_on_event") for location in locations)
        assert all(location.count(":") >= 2 for location in locations)
    finally:
        release.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive()


def test_daemon_log_summary_is_key_count_and_byte_bounded(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    records = []
    for index in range(45):
        records.append(
            {
                "ts": f"2026-09-25T00:00:{index:02d}Z",
                "event": f"event-{index}",
                "phase": "ipc-server",
                "level": "info",
                "resource": "daemon",
                "elapsedMs": index,
                "message": f"free-text-secret-{index}",
                "error": "must-be-dropped",
            }
        )
    (logs / "daemon.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    summary = daemon_log_summary(tmp_path)

    assert len(summary) == DAEMON_LOG_RECORD_LIMIT
    assert summary[0]["event"] == "event-5"
    assert summary[-1]["event"] == "event-44"
    assert all(
        set(record) <= {"ts", "event", "phase", "level", "resource", "elapsedMs"}
        for record in summary
    )
    serialized = json.dumps(summary)
    assert "free-text-secret" not in serialized
    assert "must-be-dropped" not in serialized


def test_write_prints_stable_json_and_uses_only_runner_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner_temp = tmp_path / "runner"
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    payload = {"kind": "thread-stop", "threadStack": ["file.py:7:blocked"]}

    write("daemon-stop", payload)

    line = capsys.readouterr().out.strip()
    assert line.startswith(DAEMON_TIMEOUT_EVIDENCE_LABEL)
    decoded = json.loads(line.removeprefix(DAEMON_TIMEOUT_EVIDENCE_LABEL))
    assert set(decoded) == {"schemaVersion", "testId", "evidence"}
    assert decoded == {
        "schemaVersion": 1,
        "testId": "daemon-stop",
        "evidence": payload,
    }
    artifact = (
        runner_temp
        / "pytest-results"
        / "daemon-diagnostics"
        / "daemon-stop.json"
    )
    assert json.loads(artifact.read_text(encoding="utf-8")) == decoded
    assert artifact.stat().st_size <= DIAGNOSTIC_FILE_MAX_BYTES


def test_write_caps_pathological_payloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    write("bounded", {"kind": "log", "daemonLog": ["x" * 4096] * 1000})
    line = capsys.readouterr().out.strip()
    assert len(line.encode()) <= (
        len(DAEMON_TIMEOUT_EVIDENCE_LABEL.encode()) + DIAGNOSTIC_FILE_MAX_BYTES
    )
    assert json.loads(line.removeprefix(DAEMON_TIMEOUT_EVIDENCE_LABEL))
    artifact = (
        tmp_path
        / "runner"
        / "pytest-results"
        / "daemon-diagnostics"
        / "bounded.json"
    )
    assert artifact.stat().st_size <= DIAGNOSTIC_FILE_MAX_BYTES
