"""Pi RPC native steer: exact frame, multiplexed response, honest verdicts.

The steer command rides the SAME stdout as the running turn, so the response
must be resolved by the turn's own reader -- never by a second reader on the
same pipe.  These tests drive the real fake pi RPC child.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.streaming.turn_io import (
    _SteerRejected,
    _SteerUnknown,
)
from hyprial.kernel import HarnessLaunchSpec

FAKE_PI = (
    Path(__file__).resolve().parents[4] / "fixtures/harnesses/fake_pi_rpc.py"
)


def _spec() -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="pi",
        name="pi-worker",
        headless=True,
        session_ref="stable-session",
    )


def _frames(tmp_path: Path) -> list[dict]:
    path = tmp_path / "frames.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _drive_steer(tmp_path: Path, steer_mode: str) -> object:
    """Start a long run, steer it, and return the steer outcome."""

    client = PiRpcClient(
        _spec(),
        "stable-session",
        command=(sys.executable, str(FAKE_PI)),
        env={
            "FAKE_PI_MODE": "hang",
            "FAKE_PI_STEER_MODE": steer_mode,
            "FAKE_PI_FRAME_LOG": str(tmp_path / "frames.jsonl"),
        },
        startup_timeout_seconds=10.0,
        settle_grace_seconds=0.5,
    )
    async with client:
        await client.query("start a long run")

        async def consume() -> None:
            try:
                async for _ in client.receive_response():
                    pass
            except Exception:  # noqa: BLE001 - child teardown ends the turn
                pass

        task = asyncio.create_task(consume())
        deadline = time.monotonic() + 5.0
        while not client._reading_response and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert client._reading_response, "the turn never became the stdout reader"
        try:
            return await client.steer("hold the old name", timeout=5.0)
        finally:
            await client.interrupt()
            await task


@pytest.mark.parametrize("disposition", ["queued", "handled"])
def test_pi_steer_frame_is_exact_and_response_is_multiplexed(
    tmp_path: Path, disposition: str
) -> None:
    outcome = asyncio.run(_drive_steer(tmp_path, disposition))
    assert outcome == "steered"
    frames = _frames(tmp_path)
    steer = next(frame for frame in frames if frame.get("type") == "steer")
    assert steer["message"] == "hold the old name"
    assert isinstance(steer["id"], str) and steer["id"]
    # Steering never prompts: the only prompt frame is the original run.
    assert sum(1 for frame in frames if frame.get("type") == "prompt") == 1


def test_pi_steer_explicit_refusal_is_a_safe_reject(tmp_path: Path) -> None:
    with pytest.raises(_SteerRejected):
        asyncio.run(_drive_steer(tmp_path, "reject"))


def test_pi_steer_silence_is_unknown_not_accepted(tmp_path: Path) -> None:
    with pytest.raises(_SteerUnknown):
        asyncio.run(_drive_steer(tmp_path, "silent"))


def test_process_steer_folds_a_queued_delivery_into_the_running_turn(
    tmp_path: Path,
) -> None:
    """The real process seam: a queued delivery reaches the live client."""

    from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
    from tests.daemon.harnesses.pi.rpc.process.support import delivery

    def factory() -> PiRpcClient:
        return PiRpcClient(
            _spec(),
            "stable-session",
            command=(sys.executable, str(FAKE_PI)),
            env={
                "FAKE_PI_MODE": "hang",
                "FAKE_PI_STEER_MODE": "queued",
                "FAKE_PI_FRAME_LOG": str(tmp_path / "frames.jsonl"),
            },
            startup_timeout_seconds=10.0,
            settle_grace_seconds=0.5,
        )

    process = PiRpcProcess(
        _spec(), client_factory=factory, reconnect_delay_seconds=0.01
    )
    try:
        assert process.wait_ready(timeout=10.0)
        assert process.enqueue(delivery("delivery-one", "start a long run"))
        deadline = time.monotonic() + 5.0
        while process.idle and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not process.idle, "the first turn never started"
        assert process.enqueue(delivery("delivery-two", "queued follow up"))
        time.sleep(0.2)
        assert process.steer("delivery-two", timeout=5.0) is True
    finally:
        process.stop()

    steer = next(
        frame for frame in _frames(tmp_path) if frame.get("type") == "steer"
    )
    assert steer["message"] == "queued follow up"
