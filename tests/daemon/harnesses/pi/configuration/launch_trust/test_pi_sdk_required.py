"""A P2 pi launch must find the SDK beside its command, or fail closed.

The direct-RPC fallback that rework 2 added and rework 3 reverts was a P2
boundary escape: any ordinary PATH-forwarding script with no SDK ancestor
silently ran the plain pi CLI, losing config C, the controlled context-link
refusal and the P2 session root.  These tests pin the fail-closed behavior:
an explicit command without an adjacent pi SDK is a synchronous launch
failure, never a direct-RPC start.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess

from ..test_pi_controlled_loader import _real_p22_context

_SDK_REQUIRED = "pi installation with the"


def _path_forwarding_wrapper(root: Path) -> Path:
    """A benign ``exec pi "$@"`` shim with no SDK ancestor, like the repro."""

    root.mkdir(parents=True, exist_ok=True)
    wrapper = root / "pi"
    real = shutil.which("pi") or "/bin/echo"
    wrapper.write_text(f'#!/bin/sh\nexec {real} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o700)
    return wrapper


def test_a_command_without_an_adjacent_sdk_fails_the_ready_gate(
    tmp_path: Path,
) -> None:
    registry, _repository, cwd, _context, channel, complete = _real_p22_context(
        tmp_path
    )
    try:
        spec = HarnessLaunchSpec(
            "pi", "worker", True, args=("--approve",), cwd=str(cwd)
        )
        process = PiRpcProcess(
            spec,
            command=(sys.executable,),
            worker_channel=channel,
            complete_launch=complete,
            reconnect_delay_seconds=0.01,
        )
        try:
            assert process.wait_ready(timeout=10.0) is False
            error = process.last_error or ""
            assert _SDK_REQUIRED in error, error
        finally:
            try:
                process.stop()
            except RuntimeError:
                # A failed start may leave the pump unstable; the assertion
                # above already carries the red/green signal.
                pass
    finally:
        registry.close()


def test_a_path_forwarding_wrapper_does_not_fall_into_direct_rpc(
    tmp_path: Path,
) -> None:
    wrapper = _path_forwarding_wrapper(tmp_path / "bin")
    registry, _repository, cwd, _context, channel, complete = _real_p22_context(
        tmp_path
    )
    try:
        spec = HarnessLaunchSpec(
            "pi", "worker", True, args=("--approve",), cwd=str(cwd)
        )
        with pytest.raises(ValueError, match=_SDK_REQUIRED):
            PiRpcClient(
                spec,
                "wrapper",
                command=(str(wrapper),),
                worker_channel=channel,
                complete_launch=complete,
            )
    finally:
        registry.close()
