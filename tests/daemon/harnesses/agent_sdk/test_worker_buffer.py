"""Bounded SDK message buffer and fatal-reader regressions.

The daemon environment deliberately does not install claude-agent-sdk (see
``test_agent_sdk_dependency``). These tests run the real worker file against a
minimal SDK double that reproduces the pinned SDK's per-line guard: a single
JSON frame above ``ClaudeAgentOptions.max_buffer_size`` raises in the message
reader. The 1 MiB default that killed the coordinator/Android sessions is the
behaviour under test:

* frames above 1 MiB must be accepted through the worker's explicit 16 MiB
  bound (and an operator override within the allowed range);
* a frame above that bound must end the pending turn with an explicit error,
  not leave the wrapper online with an open turn;
* a fatal command-reader exit must end the worker even while a turn is
  pending -- no silent wait on stdin;
* a fatal SDK reader must let the worker exit while stdin stays open: the
  stdin reader is a daemon thread, not an executor thread that
  ``asyncio.run`` waits for forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk_module

WORKER = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")


_FAKE_SDK = '''
"""Minimal claude-agent-sdk double with the pinned reader guard."""

import asyncio
import os


class ClaudeAgentOptions:
    def __init__(
        self,
        cwd=None,
        model=None,
        system_prompt=None,
        setting_sources=None,
        strict_mcp_config=None,
        extra_args=None,
        resume=None,
        session_id=None,
        max_buffer_size=None,
        mcp_servers=None,
        allowed_tools=None,
        disallowed_tools=None,
        cli_path=None,
    ):
        self.max_buffer_size = max_buffer_size


class ResultMessage:
    def __init__(self, result=None, is_error=False):
        self.result = result
        self.is_error = is_error


class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def query(self, prompt):
        return None

    async def receive_response(self):
        mode = os.environ.get("FAKE_SDK_READER_MODE", "normal")
        frame = int(os.environ.get("FAKE_SDK_FRAME_BYTES", "0"))
        limit = self.options.max_buffer_size
        if mode == "hang":
            await asyncio.Event().wait()
        if mode == "silent-end":
            return
        if frame > (limit or 0):
            raise Exception(
                "Failed to decode JSON: JSON message exceeded maximum "
                f"buffer size of {limit} bytes..."
            )
        yield ResultMessage(result=f"limit={limit};frame={frame}", is_error=False)
'''


def _clean_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    package = tmp_path / "claude_agent_sdk"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(_FAKE_SDK, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(tmp_path),
    }
    env.update(overrides)
    return env


def _run_worker(
    tmp_path: Path, commands: list[str], **overrides: str
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    result = subprocess.run(
        [sys.executable, str(WORKER)],
        input="".join(command + "\n" for command in commands),
        env=_clean_env(tmp_path, **overrides),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    frames = [
        json.loads(line) for line in result.stdout.splitlines() if line.strip()
    ]
    return result, frames


def _result(frames: list[dict[str, object]]) -> dict[str, object]:
    matches = [frame for frame in frames if frame.get("type") == "result"]
    assert len(matches) == 1, frames
    return matches[0]


def test_default_bound_accepts_a_frame_above_one_mib(tmp_path: Path) -> None:
    result, frames = _run_worker(
        tmp_path,
        ['{"op":"query","prompt":"work"}', '{"op":"stop"}'],
        FAKE_SDK_FRAME_BYTES="1100000",
    )

    assert result.returncode == 0, result.stderr
    assert frames[0]["type"] == "ready"
    terminal = _result(frames)
    assert terminal["isError"] is False
    assert terminal["result"] == "limit=16777216;frame=1100000"


def test_operator_override_is_honoured_within_bounds(tmp_path: Path) -> None:
    result, frames = _run_worker(
        tmp_path,
        ['{"op":"query","prompt":"work"}', '{"op":"stop"}'],
        FAKE_SDK_FRAME_BYTES="2000000",
        HYPRIAL_AGENT_SDK_MAX_BUFFER_BYTES="2097152",
    )

    assert result.returncode == 0, result.stderr
    assert _result(frames)["result"] == "limit=2097152;frame=2000000"


def test_over_limit_frame_ends_the_turn_and_exits_nonzero(tmp_path: Path) -> None:
    result, frames = _run_worker(
        tmp_path,
        ['{"op":"query","prompt":"work"}', '{"op":"stop"}'],
        FAKE_SDK_FRAME_BYTES="17000000",
    )

    assert result.returncode != 0
    terminal = _result(frames)
    assert terminal["isError"] is True
    assert "buffer limit" in str(terminal["result"])
    assert "16777216" in str(terminal["result"])


def test_reader_end_without_result_is_an_error_not_silent_success(
    tmp_path: Path,
) -> None:
    result, frames = _run_worker(
        tmp_path,
        ['{"op":"query","prompt":"work"}', '{"op":"stop"}'],
        FAKE_SDK_READER_MODE="silent-end",
    )

    assert result.returncode != 0
    terminal = _result(frames)
    assert terminal["isError"] is True
    assert "ended before a result" in str(terminal["result"])


def test_fatal_reader_exits_while_stdin_stays_open(tmp_path: Path) -> None:
    """The incident: the SDK reader died but the worker stayed alive because
    its stdin reader (an executor thread blocked in ``readline``) kept the
    interpreter from shutting down. The parent then waited on a turn that
    could never complete. The worker must emit one error result and exit even
    though stdin has NOT reached EOF.
    """

    env = _clean_env(tmp_path, FAKE_SDK_FRAME_BYTES="17000000")
    process = subprocess.Popen(
        [sys.executable, str(WORKER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write('{"op":"query","prompt":"work"}\n')
        process.stdin.flush()
        # Old behavior: asyncio.run waits for the stdin reader thread forever,
        # so this wait times out even though the turn already failed.
        process.wait(timeout=15)
        frames = [
            json.loads(line)
            for line in process.stdout.read().splitlines()
            if line.strip()
        ]
        terminal = _result(frames)
        assert process.returncode != 0
        assert terminal["isError"] is True
        assert "buffer limit" in str(terminal["result"])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin is not None:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_malformed_command_ends_a_pending_turn_instead_of_hanging(
    tmp_path: Path,
) -> None:
    result, frames = _run_worker(
        tmp_path,
        ['{"op":"query","prompt":"work"}', "not-json"],
        FAKE_SDK_READER_MODE="hang",
    )

    assert result.returncode != 0
    assert frames[0]["type"] == "ready"
    terminal = _result(frames)
    assert terminal["isError"] is True
    assert "command reader" in str(terminal["result"])
    assert "command reader" in result.stderr


def test_out_of_range_override_fails_closed(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(WORKER), "--probe"],
        env=_clean_env(
            tmp_path, HYPRIAL_AGENT_SDK_MAX_BUFFER_BYTES="1024"
        ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert "HYPRIAL_AGENT_SDK_MAX_BUFFER_BYTES" in result.stderr
    assert "between" in result.stderr
