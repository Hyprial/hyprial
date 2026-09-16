"""Receive one launch environment and run a command under pane ownership."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

_MAX_ENVIRONMENT_BYTES = 1024 * 1024
_FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
_CHILD_SIGNAL_GRACE_ENV = "HYPRIAL_TMUX_CHILD_SIGNAL_GRACE_SECONDS"
# Real Claude TUI persistence latency has not been measured. Ten seconds is a
# deliberately wide default: the rejected 2s value truncated a measured 3s
# clean exit, while 10s leaves more than 3x that observed synthetic requirement.
# No upper bound is test-covered because real Claude TUI persistence latency
# is unmeasured. Once measured, turn that observation into an upper-bound test.
_DEFAULT_CHILD_SIGNAL_GRACE_SECONDS = 10.0


def _child_signal_grace_seconds(environment: dict[str, str]) -> float:
    raw = environment.get(_CHILD_SIGNAL_GRACE_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_CHILD_SIGNAL_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{_CHILD_SIGNAL_GRACE_ENV} must be a number") from error
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(
            f"{_CHILD_SIGNAL_GRACE_ENV} must be a finite non-negative number"
        )
    return value


def main() -> int:
    if len(sys.argv) < 4 or "--" not in sys.argv[2:]:
        raise SystemExit(
            "usage: _exec_env_socket <socket> [--cleanup-config <path>] "
            "[--cleanup-recovery <path>] -- <command> [args...]"
        )
    path = Path(sys.argv[1])
    separator = sys.argv.index("--", 2)
    cleanup_config: Path | None = None
    cleanup_recovery: Path | None = None
    index = 2
    while index < separator:
        flag = sys.argv[index]
        if index + 1 >= separator:
            raise SystemExit(f"missing value for {flag}")
        value = Path(sys.argv[index + 1])
        if flag == "--cleanup-config":
            cleanup_config = value
        elif flag == "--cleanup-recovery":
            cleanup_recovery = value
        else:
            raise SystemExit(f"unknown _exec_env_socket option {flag}")
        index += 2
    if cleanup_recovery is not None and cleanup_config is None:
        raise SystemExit("--cleanup-recovery requires --cleanup-config")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path))
        received = bytearray()
        while b"\n" not in received:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise RuntimeError("environment handoff closed before newline")
            received.extend(chunk)
            if len(received) > _MAX_ENVIRONMENT_BYTES:
                raise RuntimeError("environment handoff exceeded safety limit")
    finally:
        client.close()
    value = json.loads(received.partition(b"\n")[0])
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise RuntimeError("environment handoff must be a string map")
    argv = sys.argv[separator + 1 :]
    environment = {**os.environ, **value}
    if cleanup_config is None:
        os.execvpe(argv[0], argv, environment)
        return 1

    # The pane wrapper is the release owner: it exists before the TUI starts,
    # lives exactly as long as the child, and cleans on every child exit path.
    from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

    process: subprocess.Popen[bytes] | None = None
    pending_signals: list[int] = []
    termination_deadline: float | None = None
    previous_handlers: dict[int, object] = {}

    def handle_signal(signum: int, _frame: object) -> None:
        nonlocal termination_deadline
        child = process
        if child is None:
            pending_signals.append(signum)
            return
        if child.poll() is not None:
            return
        if signum == signal.SIGINT:
            # A cooked tty sends SIGINT to the whole foreground process group,
            # so the child already received this same Ctrl-C. Direct SIGINT to
            # pane_pid has no repository caller and is deliberately not a
            # supported relay path; the topology test freezes the shared-group
            # premise so a future process-model change must redesign this.
            return
        if termination_deadline is None:
            termination_deadline = time.monotonic() + signal_grace_seconds
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    try:
        signal_grace_seconds = _child_signal_grace_seconds(environment)
        previous_handlers = {
            signum: signal.getsignal(signum) for signum in _FORWARDED_SIGNALS
        }
        for signum in _FORWARDED_SIGNALS:
            signal.signal(signum, handle_signal)
        process = subprocess.Popen(argv, env=environment)
        for signum in pending_signals:
            if signum == signal.SIGINT:
                try:
                    process.send_signal(signum)
                except ProcessLookupError:
                    pass
            else:
                handle_signal(signum, None)
        while process.poll() is None:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                if (
                    termination_deadline is not None
                    and time.monotonic() >= termination_deadline
                ):
                    process.kill()
                    termination_deadline = None
        returncode = process.returncode
        assert returncode is not None
        return returncode if returncode >= 0 else 128 + abs(returncode)
    finally:
        try:
            cleanup_launch_resources(cleanup_config, cleanup_recovery)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
