"""Bounded failure-only evidence shared by daemon tests and E2E runners."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import traceback
from typing import Any

from hyprial.daemon.impl.processes.process_diagnostics import process_cpu_sample


DAEMON_TIMEOUT_EVIDENCE_LABEL = "DAEMON_TIMEOUT_EVIDENCE="
DAEMON_LOG_RECORD_LIMIT = 40
DAEMON_LOG_READ_BYTES = 64 * 1024
DIAGNOSTIC_FILE_MAX_BYTES = 64 * 1024
_DAEMON_LOG_KEYS = ("ts", "event", "phase", "level", "resource", "elapsedMs")
_SAFE_TEST_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def thread_stack_locations(thread: threading.Thread) -> list[str]:
    """Sample one thread stack as bounded ``file:line:function`` locations.

    ``sys._current_frames`` is a non-suspending snapshot.  This deliberately
    uses ``traceback.extract_stack(sys._current_frames()[thread.ident])`` and
    never inspects frame locals, sends a signal, or attempts to suspend the
    target thread.
    """

    if thread.ident is None:
        return []
    try:
        frames = sys._current_frames()
        extracted = traceback.extract_stack(frames[thread.ident])
    except (KeyError, RuntimeError):
        return []
    return [
        f"{frame.filename[-384:]}:{frame.lineno}:{frame.name[-96:]}"
        for frame in extracted[-64:]
    ]


def _bounded_log_value(key: str, value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        limit = 64 if key in {"ts", "level"} else 256
        return value[:limit]
    return None


def daemon_log_summary(state_dir: Path) -> list[dict[str, Any]]:
    """Return at most the last 40 allow-listed daemon.jsonl records.

    Only the final 64 KiB of the input file is inspected.  Each record keeps
    only ``ts``, ``event``, ``phase``, ``level``, ``resource`` and
    ``elapsedMs`` with scalar, length-bounded values; free-text fields such as
    errors and messages never cross this boundary.
    """

    path = Path(state_dir) / "logs" / "daemon.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            start = max(0, end - DAEMON_LOG_READ_BYTES)
            stream.seek(start)
            raw = stream.read(DAEMON_LOG_READ_BYTES)
        if start:
            _, separator, raw = raw.partition(b"\n")
            if not separator:
                return []
    except OSError:
        return []

    records: list[dict[str, Any]] = []
    for raw_line in raw.splitlines():
        try:
            entry = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        record = {
            key: _bounded_log_value(key, entry[key])
            for key in _DAEMON_LOG_KEYS
            if key in entry
        }
        if record:
            records.append(record)
    return records[-DAEMON_LOG_RECORD_LIMIT:]


def child_pid_state(pid: int) -> dict[str, Any]:
    """Take one non-signalling process CPU/liveness sample for an E2E child."""

    alive, cpu_seconds = process_cpu_sample(pid)
    return {"pid": pid, "alive": alive, "cpuSeconds": cpu_seconds}


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth=depth + 1) for item in value[-64:]]
    if isinstance(value, dict):
        return {
            str(key)[:96]: _bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:32]
        }
    return type(value).__name__


def _encoded_record(test_id: str, payload: dict[str, Any]) -> bytes:
    record: dict[str, Any] = {
        "schemaVersion": 1,
        "testId": test_id[:160],
        "evidence": _bounded_value(payload),
    }
    encoded = json.dumps(
        record, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    evidence = record["evidence"]
    # Normal evidence fits comfortably.  If a caller supplies two unusually
    # deep stacks/logs, discard oldest list entries until the same valid JSON
    # document fits the per-file and stdout-line cap.
    while len(encoded) > DIAGNOSTIC_FILE_MAX_BYTES and isinstance(evidence, dict):
        candidates = [
            value for value in evidence.values() if isinstance(value, list) and value
        ]
        if not candidates:
            break
        max(candidates, key=len).pop(0)
        record["truncated"] = True
        encoded = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    if len(encoded) > DIAGNOSTIC_FILE_MAX_BYTES:
        record = {
            "schemaVersion": 1,
            "testId": test_id[:160],
            "evidence": {"kind": str(payload.get("kind", "unknown"))[:128]},
            "truncated": True,
        }
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    return encoded


def write(test_id: str, payload: dict[str, Any]) -> None:
    """Print one stable evidence line and optionally persist the same JSON."""

    encoded = _encoded_record(test_id, payload)
    print(DAEMON_TIMEOUT_EVIDENCE_LABEL + encoded.decode("utf-8"), flush=True)
    runner_temp = os.environ.get("RUNNER_TEMP")
    if not runner_temp:
        return
    safe_test_id = _SAFE_TEST_ID.sub("-", test_id).strip(".-")[:160] or "unknown"
    destination = (
        Path(runner_temp)
        / "pytest-results"
        / "daemon-diagnostics"
        / f"{safe_test_id}.json"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
    except OSError:
        # The labelled stdout line is the always-available evidence channel.
        # An unavailable artifact directory must not replace the original
        # timeout failure with a diagnostics-write exception.
        pass
