"""A dsh-tui wake answers with the turn's outcome, not with "followup queued".

The Cordis plugin used to answer ``ok`` as soon as ``agent.followup()``
returned, so a turn that dsh rejected (the format-v4 source error, a provider
failure) was logged ``wake.signalled`` and left alone for the 300 s resend
window: a silent failure measured in the third and fourth PTY re-checks.

The plugin now watches the agent's own events for the message it queued and
answers with what happened inside a bounded window: the model started
answering (``accepted``), the message was claimed or is still queued behind a
running turn (``claimed``/``queued``), or the turn failed (``TURN_ERROR``,
``WAKE_DISCARDED``, ``FOLLOWUP_REJECTED``). The carrier maps a failure to
``WakeStatus.FAILED`` (a warn-level ``wake.failed`` plus a retry on the bounded
DSH curve) instead of ``ACCEPTED``.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

import anyio
import pytest

from hyprial.daemon.impl.harnesses.dsh_tui import (
    DshPluginWakeDriver,
    write_dsh_tui_plugin,
)
from hyprial.daemon.impl.harnesses.dsh_tui import carrier as carrier_module
from hyprial.daemon.impl.mcp.wake import WakeCommand, WakeStatus

_HARNESS = r"""
import { pathToFileURL } from 'node:url'
import { connect } from 'node:net'

const plugin = await import(pathToFileURL(process.argv[2]).href)
const socketPath = process.argv[3]
const scenario = process.argv[4]
const listeners = new Map()
const on = (name, fn) => {
  const set = listeners.get(name) ?? new Set()
  set.add(fn)
  listeners.set(name, set)
  return () => set.delete(fn)
}
const emit = (name, payload) => { for (const fn of listeners.get(name) ?? []) fn(payload) }
const other = { session: { id: 'other' } }
const agent = {
  session: { id: 'session-1' },
  followup(message) {
    if (scenario === 'throws') throw new Error('format v4 message requires a producer-owned source kind')
    setTimeout(() => {
      if (scenario === 'discarded') return emit('agent/inbox/discarded', { agent, message })
      if (scenario === 'queued') {
        emit('agent/error', { agent: other, turn: 1, step: 1, error: new Error('not ours') })
        return
      }
      emit('agent/inbox/claimed', { agent, message, turn: 7 })
      if (scenario === 'turn-error') {
        emit('agent/error', { agent: other, turn: 7, step: 1, error: new Error('not ours') })
        emit('agent/error', { agent, turn: 7, step: 1, error: new Error('DeepSeek Messages expected a JSON object') })
      }
      if (scenario === 'accepted') {
        emit('agent/assistant-stream', { agent, frame: { type: 'start', turn: 7, step: 1 } })
        emit('agent/assistant-stream', { agent, frame: { type: 'chunk', chunk: { type: 'text-delta' } } })
      }
      if (scenario === 'start-then-error') {
        emit('agent/assistant-stream', { agent, frame: { type: 'start', turn: 7, step: 1 } })
        emit('agent/assistant-stream', { agent, frame: { type: 'chunk', chunk: { type: 'finish', reason: { kind: 'error', failure: { message: 'HTTP 400' } } } } })
        setTimeout(() => {
          emit('agent/error', { agent, turn: 7, step: 1, error: new Error('HTTP 400 from the provider') })
        }, 20)
      }
    }, 20)
  },
}
const ctx = {
  agents: { roots: () => [agent] },
  on,
  effect(fn) { ctx.cleanup = fn() },
  logger: { warn() {} },
}
plugin.apply(ctx, { socketPath, sessionId: 'session-1' })
await new Promise((resolve) => setTimeout(resolve, 100))
const response = await new Promise((resolve, reject) => {
  const connection = connect(socketPath, () => {
    connection.write(JSON.stringify({ type: 'wake', prompt: '[Harness Network] x', outcomeTimeoutMs: 300 }) + '\n')
  })
  let data = ''
  connection.on('data', (chunk) => {
    data += chunk
    if (data.includes('\n')) { connection.end(); resolve(JSON.parse(data.split('\n')[0])) }
  })
  connection.on('error', reject)
})
const live = [...listeners.values()].reduce((count, set) => count + set.size, 0)
console.log(JSON.stringify({ response, live }))
if (ctx.cleanup) await ctx.cleanup()
"""


def _short_private_dir() -> Path:
    directory = Path("/tmp") / f"dshw-{uuid4().hex[:12]}"
    directory.mkdir(mode=0o700)
    return directory


def _run_plugin(tmp_path: Path, scenario: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the dsh-tui Cordis plugin runtime")
    plugin_path = write_dsh_tui_plugin(tmp_path / scenario)
    harness = tmp_path / scenario / "wake_outcome_harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    control_dir = _short_private_dir()
    control_socket = control_dir / "c.sock"
    try:
        completed = subprocess.run(
            [node, str(harness), str(plugin_path), str(control_socket), scenario],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        control_socket.unlink(missing_ok=True)
        control_dir.rmdir()
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["live"] == 0, "wake outcome listeners must be disposed"
    return payload["response"]


def test_a_turn_error_after_the_claim_is_reported_as_a_failed_wake(
    tmp_path: Path,
) -> None:
    response = _run_plugin(tmp_path, "turn-error")

    assert response["ok"] is False
    assert response["code"] == "TURN_ERROR"
    assert "expected a JSON object" in str(response["detail"])


def test_an_attempt_start_without_a_streamed_chunk_is_not_acceptance(
    tmp_path: Path,
) -> None:
    """Measured on the real PTY: dsh publishes the stream's ``start`` frame
    and a terminal ``finish`` chunk carrying the failure before the turn
    errors. Neither is evidence the model answered; only content is."""

    response = _run_plugin(tmp_path, "start-then-error")

    assert response["ok"] is False
    assert response["code"] == "TURN_ERROR"
    assert "HTTP 400" in str(response["detail"])


def test_a_model_answer_start_is_an_accepted_wake(tmp_path: Path) -> None:
    response = _run_plugin(tmp_path, "accepted")

    assert response == {"ok": True, "state": "accepted", "sessionId": "session-1"}


def test_a_followup_that_throws_or_is_discarded_fails_the_wake(
    tmp_path: Path,
) -> None:
    assert _run_plugin(tmp_path, "throws")["code"] == "FOLLOWUP_REJECTED"
    assert _run_plugin(tmp_path, "discarded")["code"] == "WAKE_DISCARDED"


def test_a_message_queued_behind_a_running_turn_is_not_a_failure(
    tmp_path: Path,
) -> None:
    """Another agent's error, or no event inside the window, is not ours."""

    response = _run_plugin(tmp_path, "queued")

    assert response["ok"] is True
    assert response["state"] == "queued"


class _OneShotPlugin:
    def __init__(self, reply: dict[str, object]) -> None:
        self.directory = _short_private_dir()
        self.socket_path = self.directory / "c.sock"
        self.reply = reply
        self.frames: list[dict[str, object]] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _address = self._server.accept()
        with connection:
            data = b""
            while b"\n" not in data:
                data += connection.recv(4096)
            self.frames.append(json.loads(data.split(b"\n", 1)[0]))
            connection.sendall(json.dumps(self.reply).encode() + b"\n")

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)


def _wake(reply: dict[str, object]) -> tuple[object, dict[str, object]]:
    plugin = _OneShotPlugin(reply)
    try:
        driver = DshPluginWakeDriver(plugin.socket_path)
        command = WakeCommand(
            kind="formal",
            actor="agent:op:node:dsh",
            delivery_id="delivery-1",
            cwd="/tmp/work",
            command=("dsh-tui",),
            prompt="Call harness_read once",
        )
        attempt = anyio.run(driver.wake, command)
    finally:
        plugin.close()
    return attempt, plugin.frames[0]


def test_the_driver_maps_a_failed_turn_to_failed_not_accepted() -> None:
    attempt, frame = _wake(
        {"ok": False, "code": "TURN_ERROR", "detail": "provider said no"}
    )

    assert attempt.status == WakeStatus.FAILED
    assert "TURN_ERROR" in str(attempt.detail)
    assert "provider said no" in str(attempt.detail)
    timeout_ms = frame["outcomeTimeoutMs"]
    assert isinstance(timeout_ms, int) and timeout_ms > 0
    driver = DshPluginWakeDriver(Path("/nonexistent"))
    assert driver.wake_timeout_seconds > timeout_ms / 1000


def test_the_driver_keeps_a_queued_wake_accepted_and_names_its_state() -> None:
    attempt, _frame = _wake({"ok": True, "state": "queued", "sessionId": "s"})

    assert attempt.status == WakeStatus.ACCEPTED
    assert attempt.detail == "dsh-tui wake queued"


def test_a_failed_dsh_wake_retries_on_the_bounded_turn_curve() -> None:
    """A failed wake is a failed model turn; never retry it every second."""

    delay = carrier_module.dsh_wake_failure_delay

    assert delay(1) == 30.0
    assert delay(5) == carrier_module.DSH_WAKE_RESIGNAL_SECONDS
    assert delay(50) == carrier_module.DSH_WAKE_RESIGNAL_SECONDS
