# Decision record (Z-5, owner decision 2026-11-18: first login is one daemon operation):
# - One ``FirstLoginRun`` is the daemon's in-memory projection of one login it
#   drives.  It is not journaled and not a second authority: durable truth stays
#   in the three receipts the journal already holds (login, device, initialize),
#   and ``onboarding.status`` stays the source of truth for the device after the
#   login.  ``completion`` is a SNAPSHOT taken at the end of the login, not a
#   live view of the device.  It carries no pid and no epoch: a client that
#   wants them reads ``ping`` once the stage is ``serving``.
# - Locking.  The run has its own lock and condition, taken last: the driver
#   and the request threads may hold the dispatcher's exclusive lock when they
#   touch a run, and nothing here ever takes the dispatcher lock, so the order
#   is always dispatcher lock, then run lock.  A parked waiter holds neither:
#   it blocks on ``wait_change`` only, and answers from ``answer`` without the
#   dispatcher lock or the journal.
# - Waits.  ``wait_change`` is bounded by the caller's timeout and every change
#   notifies, so a parked reader wakes at once and also re-checks its own stop
#   condition between slices.
# - The operation view.  The driver publishes the login operation's last known
#   projection (pending with its verification, then authenticated with its owner,
#   then the frozen final one) after each step it took under the dispatcher lock.
#   ``answer`` is that view plus ``completion``; once the view is frozen the
#   journal is never read again for this login: after the accept the journal
#   migrates its transport generation on another thread.
# - Secrets and text.  Nothing from the device flow (device code, tokens) and no
#   key material is ever stored here.  The identity block is mode, issuer and
#   where the profile came from; the device block is the public record's id and
#   key generation and the user key's state and code.  An error is a CODE plus a
#   fixed message authored here per code: exception text (paths, OS errors,
#   sidecar output) never enters a run, however it was raised.
# - Retry.  A device that is not ready at first login is attempted again by the
#   daemon exactly once at each serving start, including the start this login
#   leads to (the 2026-11-16 decision in ``lifecycle/recovery.py``, which now also
#   runs after a promoted start).  ``retry`` therefore says ``serving-start`` and
#   never promises a loop or any other retry.
"""The projection of one daemon-driven first login."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any

from hyprial.kernel import ipc_errors

__all__ = [
    "RETRY_SERVING_START",
    "STAGES",
    "TERMINAL_STAGES",
    "FirstLoginRun",
    "authored_error",
]

STAGES = ("authorizing", "device", "starting", "serving", "stopped")
TERMINAL_STAGES = frozenset({"serving", "stopped"})
RETRY_SERVING_START = "serving-start"

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_DEFAULT_CODE = ipc_errors.DAEMON_ERROR
_GENERIC = "the first login stopped before the daemon was serving"
_MESSAGES = {
    ipc_errors.DEVICE_ENSURE_FAILED: "the device could not be registered during this login",
    ipc_errors.SUBMIT_OUTCOME_UNKNOWN: "the device registration outcome is unknown",
    ipc_errors.IDENTITY_UNBOUND: "the committed identity has no readable device record",
    ipc_errors.IDENTITY_CONFLICT: "此 HYPRIAL_HOME 已绑定其他账号，本次登录未写入任何内容。",
}


def authored_error(code: object) -> dict[str, str]:
    """``{code, message}`` for ``code``; the message is fixed, never exception text."""

    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        code = _DEFAULT_CODE
    return {"code": code, "message": _MESSAGES.get(code, _GENERIC)}


class FirstLoginRun:
    """The state every reader of one driven login sees."""

    def __init__(self, operation: Mapping[str, Any]) -> None:
        self.operation_id = str(operation["operationId"])
        self._condition = threading.Condition(threading.Lock())
        self._version = 0
        self._stage = "authorizing"
        self._identity: dict[str, Any] | None = None
        self._device: dict[str, Any] | None = None
        self._failure: dict[str, str] | None = None
        self._view: dict[str, Any] = dict(operation)
        self._frozen = False
        self._claimed = False

    @property
    def claimed(self) -> bool:
        """True when THIS run's accept created the initialize claim."""

        with self._condition:
            return self._claimed

    def claim(self) -> None:
        with self._condition:
            self._claimed = True

    @property
    def stage(self) -> str:
        with self._condition:
            return self._stage

    @property
    def version(self) -> int:
        with self._condition:
            return self._version

    @property
    def final(self) -> bool:
        return self.stage in TERMINAL_STAGES

    @property
    def device_ready(self) -> bool:
        with self._condition:
            return bool(self._device and self._device.get("ready") is True)

    @property
    def frozen(self) -> bool:
        with self._condition:
            return self._frozen

    def operation(self) -> dict[str, Any]:
        """The login operation's last published projection."""

        with self._condition:
            return dict(self._view)

    def completion(self) -> dict[str, Any]:
        """The wire projection: a snapshot, never a live reference."""

        with self._condition:
            return self._completion_locked()

    def _completion_locked(self) -> dict[str, Any]:
        return {
            "stage": self._stage,
            "identity": None if self._identity is None else dict(self._identity),
            "device": _copy_device(self._device),
            "failure": None if self._failure is None else dict(self._failure),
        }

    def answer(self) -> dict[str, Any]:
        """The ``auth.operation.get`` answer from the published view; no lock, no journal.

        The view and the completion are read under one hold, so a concurrent
        stop can never produce a pending view with a stopped stage.
        """

        with self._condition:
            return {
                "ok": True,
                "operation": {**self._view, "completion": self._completion_locked()},
            }

    def publish(self, operation: Mapping[str, Any]) -> None:
        """Publish a newer projection of the login operation (ignored once frozen)."""

        with self._condition:
            if self._frozen:
                return
            self._view = dict(operation)
            self._version += 1
            self._condition.notify_all()

    def advance(
        self,
        stage: str,
        *,
        identity: Mapping[str, Any] | None = None,
        device: Mapping[str, Any] | None = None,
    ) -> None:
        """Move to ``stage``; a terminal stage is never left again."""

        if stage not in STAGES:
            raise ValueError(f"unknown first-login stage {stage!r}")
        with self._condition:
            if self._stage in TERMINAL_STAGES:
                return
            self._stage = stage
            if identity is not None:
                self._identity = dict(identity)
            if device is not None:
                self._device = _copy_device(device)
            self._version += 1
            self._condition.notify_all()

    def freeze(self, operation: Mapping[str, Any]) -> None:
        """Record the last journal projection of the login; the first one wins."""

        with self._condition:
            if self._frozen:
                return
            self._view = dict(operation)
            self._frozen = True
            self._version += 1
            self._condition.notify_all()

    def stop(self, code: object = None) -> None:
        """End the run without serving; ``code`` names why, when it is known."""

        with self._condition:
            if self._stage in TERMINAL_STAGES:
                return
            self._stage = "stopped"
            if code is not None:
                self._failure = authored_error(code)
            if not self._frozen:
                self._view = {**self._view, "state": "unknown", "verification": None}
                self._frozen = True
            self._version += 1
            self._condition.notify_all()

    def wait_change(self, seen: int, timeout: float) -> int:
        """Block until the run changed after ``seen`` or ``timeout`` seconds pass."""

        with self._condition:
            if self._version == seen:
                self._condition.wait(timeout=max(0.0, timeout))
            return self._version


def _copy_device(device: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if device is None:
        return None
    copied = dict(device)
    for key in ("userKey", "error"):
        if isinstance(copied.get(key), Mapping):
            copied[key] = dict(copied[key])
    return copied
