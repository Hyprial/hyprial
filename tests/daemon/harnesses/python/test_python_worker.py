"""Packaged Jev worker protocol tests (credential-free)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.python_worker import _python_worker as worker
from hyprial.daemon.impl.harnesses.python_worker._python_worker import WorkerError, parse_payload


def test_jev_payload_rejects_unknown_question_fields() -> None:
    with pytest.raises(WorkerError, match="unknown fields"):
        parse_payload(
            {
                "state": {"ticket": "redacted"},
                "questions": {
                    "urgent": {"type": "noul", "unexpected": "no"},
                },
            }
        )


def test_jev_worker_emits_ready_and_decode_error_without_a_credential() -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "LANG", "PYTHONPATH"}
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hyprial.daemon.impl.harnesses.python_worker._python_worker",
            "--kind",
            "jev",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    assert ready["type"] == "ready"
    assert ready["kind"] == "jev"
    assert ready["typesafeSdkVersion"] == "0.7.0"
    process.stdin.write(
        json.dumps(
            {
                "v": 1,
                "op": "call",
                "id": "redacted-id",
                "payload": {
                    "state": {"ticket": "not logged"},
                    "questions": {},
                },
            }
        ).encode()
        + b"\n"
    )
    process.stdin.write(b'{"v":1,"op":"stop"}\n')
    process.stdin.close()
    frames = []
    while True:
        line = process.stdout.readline()
        if not line:
            break
        frames.append(json.loads(line))
    assert any(frame.get("type") == "metric" and frame.get("segment") == "decode" for frame in frames)
    error = next(frame for frame in frames if frame.get("type") == "error")
    assert error["code"] == "PROVIDER_INVALID_REQUEST"
    assert "not logged" not in json.dumps(frames)
    assert process.wait(timeout=2) == 0


def test_startup_hash_golden_is_utf8_sorted_and_finite() -> None:
    values = {
        "kind": "jev",
        "effectiveConfig": {"model": "模型", "concurrency": 10},
        "typesafeSdkVersion": "0.7.0",
        "scriptSha256": "b" * 64,
    }
    assert worker.canonical_startup_json(values).decode("utf-8") == (
        '{"effectiveConfig":{"concurrency":10,"model":"模型"},'
        '"kind":"jev",'
        '"scriptSha256":"' + "b" * 64 + '","typesafeSdkVersion":"0.7.0"}'
    )
    assert worker.startup_hash(values) == (
        "9baa5676e073789ea7ba4f1f5bfc99f9124c99c6516f0da2bf9dff602b76fce4"
    )


def test_startup_metadata_hashes_exactly_the_published_input() -> None:
    """The wire startupHash covers exactly the published input, nothing more."""

    metadata = worker._startup_metadata("jev")
    values = {
        key: metadata[key]
        for key in ("kind", "effectiveConfig", "typesafeSdkVersion", "scriptSha256")
    }
    assert worker.startup_hash(values) == metadata["startupHash"]
    assert set(metadata) == {
        "kind",
        "effectiveConfig",
        "typesafeSdkVersion",
        "scriptSha256",
        "startupHash",
        "credentialSource",
    }


def test_worker_starts_from_a_non_source_cwd_and_restarts(tmp_path: Path) -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "LANG", "PYTHONPATH"}
    }
    hashes: list[str] = []
    for _ in range(2):
        process = subprocess.Popen(
            [sys.executable, "-m", "hyprial.daemon.impl.harnesses.python_worker._python_worker", "--kind", "jev"],
            cwd=tmp_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        assert process.stdout is not None and process.stdin is not None
        ready = json.loads(process.stdout.readline())
        hashes.append(ready["startupHash"])
        process.stdin.write(b'{"v":1,"op":"stop"}\n')
        process.stdin.close()
        assert process.wait(timeout=2) == 0
    assert hashes[0] == hashes[1]


def test_worker_fails_closed_when_cwd_shadows_package_names(tmp_path: Path) -> None:
    (tmp_path / "hyprial").mkdir()
    (tmp_path / "hyprial" / "__init__.py").write_text("shadow = True\n")
    (tmp_path / "typesafe_sdk").mkdir()
    (tmp_path / "typesafe_sdk" / "__init__.py").write_text("shadow = True\n")
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "LANG"}
    }
    process = subprocess.run(
        [sys.executable, "-m", "hyprial.daemon.impl.harnesses.python_worker._python_worker", "--kind", "jev"],
        cwd=tmp_path,
        input=b"",
        capture_output=True,
        env=environment,
        timeout=2,
    )
    assert process.returncode != 0
    assert b"ready" not in process.stdout


# -- 2026-09-26: python-worker starts failed silently past a hard 10 s ------
#
# Production jev and user-proxy starts exceeded the 10 s ready deadline under
# load (43 s from op start to a PROCESSED ready); the failure left no log line
# (only connectors[].error), the retry budget ran out, and a late ready was
# still logged as worker.started -- so the logs looked healthy.


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def info(self, event: str, **fields: object) -> None:
        self.items.append((event, fields))


def _jev_spec():
    from hyprial.kernel import HarnessLaunchSpec

    return HarnessLaunchSpec(harness="jev", name="p", headless=True)


def test_a_worker_that_never_readies_says_so_in_the_log() -> None:
    from hyprial.daemon.impl.harnesses.protocol.common import HarnessStartError
    from hyprial.daemon.impl.harnesses.python_worker import PythonHarnessProcess

    events = _Events()
    with pytest.raises(HarnessStartError):
        PythonHarnessProcess(
            _jev_spec(),
            command=(sys.executable, "-c", "import time; time.sleep(30)"),
            startup_timeout_seconds=0.5,
            stop_timeout_seconds=0.5,
            logger=events,  # type: ignore[arg-type]
        )
    failed = [f for e, f in events.items if e == "worker.start_failed"]
    assert len(failed) == 1
    assert failed[0]["budgetMs"] == 500
    assert failed[0]["waitedMs"] >= 400
    assert failed[0]["kind"] == "jev"


def test_a_started_worker_logs_how_long_ready_took(tmp_path: Path) -> None:
    """readyAfterMs is always logged for a started worker."""

    from hyprial.daemon.impl.harnesses.python_worker import PythonHarnessProcess

    home = tmp_path / "isolated-home"
    home.mkdir()
    env = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "LANG"}
    }
    env["HOME"] = str(home)
    env["HARNESS_STATE_DIR"] = str(tmp_path / "state")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[4] / "src")
    events = _Events()
    process = PythonHarnessProcess(
        _jev_spec(),
        command=(
            sys.executable, "-m", "hyprial.daemon.impl.harnesses.python_worker._python_worker",
            "--kind", "jev",
        ),
        env=env,
        logger=events,  # type: ignore[arg-type]
    )
    try:
        started = [f for e, f in events.items if e == "worker.started"]
    finally:
        process.stop()
    assert len(started) == 1
    assert started[0]["readyAfterMs"] >= 0


def test_the_worker_emit_time_is_read_from_its_stderr_marker() -> None:
    from hyprial.daemon.impl.harnesses.python_worker import _ready_emitted_at_ms

    assert _ready_emitted_at_ms(b"noise\nhyprial-worker-ready-emitted-at-ms 1790400000123\n") == 1790400000123
    assert _ready_emitted_at_ms(b"nothing here\n") is None
    assert _ready_emitted_at_ms(b"hyprial-worker-ready-emitted-at-ms nope\n") is None


def test_the_default_ready_budget_covers_the_observed_production_start() -> None:
    from hyprial.daemon.impl.harnesses.python_worker import (
        PYTHON_WORKER_STARTUP_TIMEOUT_SECONDS_DEFAULT,
    )

    # 43 s op-start -> processed ready on 2026-09-26; the old 10 s failed it.
    assert PYTHON_WORKER_STARTUP_TIMEOUT_SECONDS_DEFAULT > 43.0
