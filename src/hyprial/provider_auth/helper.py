"""Python side of the pi device-code relogin helper.

The helper itself is ``pi_device_login.mjs`` — it must run pi's own
``ModelRuntime.login`` so the client_id, endpoints, and credential write all
stay pi's.  This module owns the spawn, the stdout wire format (one JSON
object per line), and the outcome mapping.

The wire shapes are parsed here and only here; the ``.mjs`` writer is pinned
to them by ``tests/test_provider_auth_helper.py`` so neither side can drift
quietly.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

HELPER_SCRIPT = Path(__file__).with_name("pi_device_login.mjs")


class HelperOutcome(StrEnum):
    OK = "ok"
    TIMED_OUT = "timed-out"  # device code expired; another round may start
    FAILED = "failed"  # login machinery itself failed
    SPAWN_ERROR = "spawn-error"  # node/pi not launchable at all
    STOPPED = "stopped"  # daemon is shutting down; not a failure


@dataclass(frozen=True, slots=True)
class DeviceCodeAnnouncement:
    """The two human-facing values of a device flow, plus its lifetime."""

    user_code: str
    verification_uri: str
    interval_seconds: float | None
    expires_in_seconds: float | None


def parse_helper_line(line: str) -> dict[str, object] | None:
    """Parse one stdout line; anything malformed is not an event."""

    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") not in {
        "device_code",
        "ok",
    }:
        return None
    return record


def announcement_from(record: dict[str, object]) -> DeviceCodeAnnouncement | None:
    """The device_code event, or None when the shape is not the contract's."""

    if record.get("type") != "device_code":
        return None
    user_code = record.get("userCode")
    uri = record.get("verificationUri")
    if not isinstance(user_code, str) or not user_code:
        return None
    if not isinstance(uri, str) or not uri:
        return None
    interval = record.get("intervalSeconds")
    expires = record.get("expiresInSeconds")
    return DeviceCodeAnnouncement(
        user_code=user_code,
        verification_uri=uri,
        interval_seconds=interval if isinstance(interval, (int, float)) else None,
        expires_in_seconds=expires if isinstance(expires, (int, float)) else None,
    )


def find_pi_package_root(command: tuple[str, ...]) -> Path | None:
    """Resolve the pi install root from the worker's own pi binary.

    The binary is ``<root>/dist/bundle/cli.js`` (a symlink from PATH), so the
    resolved path's grandparent's parent is the package root.  Using the
    *worker's* command keeps the helper on the same pi build the workers run.
    """

    if not command:
        return None
    try:
        binary = Path(command[0]).resolve()
    except OSError:
        return None
    # …/dist/bundle/cli.js → parents: bundle, dist, <root>
    root = binary.parent.parent.parent
    if not (root / "dist" / "index.js").exists():
        return None
    return root


class DeviceLoginRunner:
    """Spawn the helper and stream its events; one call = one device code."""

    def __init__(
        self,
        *,
        pi_command: tuple[str, ...] = ("pi",),
        node_command: str = "node",
        timeout_seconds: float = 1500.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("helper timeout must be positive")
        self._pi_command = pi_command
        self._node_command = node_command
        self._timeout_seconds = timeout_seconds

    def __call__(
        self,
        provider: str,
        on_device_code: Callable[[DeviceCodeAnnouncement], None],
        *,
        stop: threading.Event | None = None,
    ) -> HelperOutcome:
        root = find_pi_package_root(self._pi_command)
        if root is None:
            return HelperOutcome.SPAWN_ERROR
        # DEVNULL via an explicit fd we close ourselves: Popen only closes
        # its implicit devnull in __del__, and the reaper thread holds the
        # Popen alive — the suite's per-test fd guard (card #77) counts that.
        with open(os.devnull, "wb") as devnull:
            try:
                process = subprocess.Popen(
                    [
                        self._node_command,
                        str(HELPER_SCRIPT),
                        str(root),
                        provider,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=devnull,  # stderr carries only class names;
                    # the outcome comes from the exit code, so the stream is moot.
                    text=True,
                )
            except OSError:
                return HelperOutcome.SPAWN_ERROR
            # One watcher bounds the helper both ways: a stop event (daemon
            # shutdown) terminates it, and so does the wall-clock deadline.  The
            # deadline matters because the stdout pump below blocks on read: a
            # helper that hangs with the pipe open would never reach a
            # ``wait(timeout=…)``.  pi bounds the flow itself (codex 900s; kimi
            # server-side expires_in); this deadline is the backstop, not the
            # budget.
            reaped = {"deadline": False}
            threading.Thread(
                target=self._reap, args=(process, stop, reaped), daemon=True
            ).start()
            try:
                return self._pump(process, on_device_code, stop, reaped)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()

    def _reap(
        self,
        process: subprocess.Popen[str],
        stop: threading.Event | None,
        reaped: dict[str, bool],
    ) -> None:
        # Wake on process exit, stop, or the deadline — whichever first — so
        # a finished round leaves no sleeping thread behind.
        deadline = time.monotonic() + self._timeout_seconds
        while process.poll() is None:
            if stop is not None and stop.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The wall-clock backstop expired with the helper alive.
                reaped["deadline"] = True
                break
            if stop is not None:
                stop.wait(timeout=min(1.0, remaining))
            else:
                time.sleep(min(1.0, remaining))
        if process.poll() is None:
            process.terminate()

    def _pump(
        self,
        process: subprocess.Popen[str],
        on_device_code: Callable[[DeviceCodeAnnouncement], None],
        stop: threading.Event | None,
        reaped: dict[str, bool],
    ) -> HelperOutcome:
        assert process.stdout is not None
        announced = False
        # The device_code line arrives long before exit; read line by line so
        # the owner sees the code while the helper is still polling.
        for line in process.stdout:
            record = parse_helper_line(line)
            if record is None:
                continue
            if record.get("type") == "device_code" and not announced:
                announcement = announcement_from(record)
                if announcement is not None:
                    announced = True
                    on_device_code(announcement)
        exit_code = process.wait()
        if exit_code == 0:
            return HelperOutcome.OK
        if exit_code == 3:
            return HelperOutcome.TIMED_OUT
        if exit_code < 0:
            if stop is not None and stop.is_set():
                return HelperOutcome.STOPPED
            if reaped["deadline"]:
                return HelperOutcome.TIMED_OUT
        return HelperOutcome.FAILED
