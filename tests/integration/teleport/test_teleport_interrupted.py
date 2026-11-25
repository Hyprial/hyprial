"""Slice 5 scenario 3: the target daemon goes away in the middle of the stream.

Decision record:

- The home carries ``INTERRUPT_CHUNKS`` chunks of random bytes so the stream
  outlives the moment the test needs.  The deterministic hook is the
  target's own staging file: the target fsyncs each accepted chunk to
  ``<daemon state dir>/teleport/staging/<operationId>/stream`` before it
  acknowledges it, so a stream file of at least one chunk means the first
  chunk was persisted.  No product seam is added.
- The teleport runs on a background thread through the raw ``teleport.run``
  request while the test watches the staging file, then stops the target.
- Two arms.  The graceful arm sends SIGTERM, the daemon's own shutdown
  signal (``hyprial daemon stop`` was tried first and was too slow: the CLI
  round trip let every chunk and the receive finish before the daemon went
  down).  The crash arm sends SIGKILL, the pure "chunk goes unanswered" case.
- Per docs/design/agent-teleport-protocol.md §2/§4 silence on a chunk is
  ``TELEPORT_INCOMPLETE``; the source restarts its agent and sends a
  best-effort abort.  Both arms assert exactly that.  The witnesses are the
  source's ``ps`` connector and desired-state row (running again on the same
  session ref), its unchanged workspace bytes, and, after the target is
  started again, the purged staging root and the absence of a landed row.
- Whether the killed daemon is gone is read through ``tests.support.process_table.alive``
  (``ps`` state, zombies count as gone), not ``/proc``, which a macOS host lacks and
  where an unreadable ``/proc`` entry would have read as "gone" at once.
- The target is always started again in ``finally`` so the next arm has a
  peer; ``bytesSent`` from an ``UNRESOLVED`` answer is part of the failure
  message, because it shows how much of the stream the stopping target still
  accepted.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.support.process_table import alive

from .conftest import (
    CHUNK,
    TARGET_NODE,
    Pair,
    agent_rows,
    connector,
    desired_harness,
    seed_worker,
    staging_entries,
    teleport,
    tree_digest,
    wait_running,
)

INTERRUPT_CHUNKS = 96
FIRST_CHUNK_WAIT_SECONDS = 180.0


def _stream_files(pair: Pair) -> list[Path]:
    root = pair.target.root / "state" / "teleport" / "staging"
    return list(root.glob("*/stream")) if root.exists() else []


def _wait_first_chunk(pair: Pair, finished: threading.Event) -> int:
    deadline = time.monotonic() + FIRST_CHUNK_WAIT_SECONDS
    while time.monotonic() < deadline and not finished.is_set():
        for stream in _stream_files(pair):
            try:
                size = stream.stat().st_size
            except FileNotFoundError:
                continue
            if size >= CHUNK:
                return size
        time.sleep(0.005)
    return -1


def _daemon_pid(pair: Pair) -> int:
    metadata = json.loads(
        (pair.target.root / "state" / "daemon.json").read_text(encoding="utf-8")
    )
    return int(metadata["pid"])


def _graceful(pair: Pair) -> None:
    pair.target.sigterm()
    pair.target.wait_stopped(timeout=120.0)


def _crash(pair: Pair) -> None:
    pid = _daemon_pid(pair)
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"target daemon {pid} survived SIGKILL")


def _interrupt(pair: Pair, name: str, stop: Callable[[Pair], None]) -> None:
    worker = seed_worker(pair.source, name, payload_bytes=INTERRUPT_CHUNKS * CHUNK)
    workspace_before = tree_digest(worker.home / "workspace")
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def run() -> None:
        try:
            outcome["document"] = teleport(pair.source, worker.name, TARGET_NODE)
        except BaseException as error:
            outcome["exception"] = error
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    stopped = False
    try:
        persisted = _wait_first_chunk(pair, finished)
        assert persisted >= CHUNK, (
            "the first chunk was never seen on the target before the run ended",
            outcome,
        )
        assert persisted < INTERRUPT_CHUNKS * CHUNK, persisted
        stop(pair)
        stopped = True
        thread.join(timeout=300.0)
        assert finished.is_set(), "teleport.run did not answer within 300 s"

        assert "exception" not in outcome, outcome
        document = outcome["document"]
        error = document.get("error")
        assert isinstance(error, dict), document
        assert error["code"] == "TELEPORT_INCOMPLETE", error

        assert wait_running(pair.source, worker.name), connector(
            pair.source, worker.name
        )
        desired = desired_harness(pair.source, worker.name)
        assert desired is not None and desired["session_ref"] == worker.session_ref
        assert worker.name in agent_rows(pair.source)
        assert tree_digest(worker.home / "workspace") == workspace_before
        assert pair.stub_calls() == []
    finally:
        thread.join(timeout=300.0)
        if stopped:
            pair.target.start()

    assert worker.name not in agent_rows(pair.target)
    assert staging_entries(pair.target) == []


def test_target_sigterm_mid_stream_restores_the_source(pair: Pair) -> None:
    _interrupt(pair, "tp-interrupt-term", _graceful)


def test_target_crash_mid_stream_restores_the_source(pair: Pair) -> None:
    _interrupt(pair, "tp-interrupt-kill", _crash)
