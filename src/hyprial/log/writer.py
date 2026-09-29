"""Bounded log I/O owner; producer threads only admit immutable bytes."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from hyprial.actor_runtime import ActorRuntime
from hyprial.actor_runtime.contracts import ActorSpec, AdmissionResult


@dataclass(frozen=True, slots=True)
class AppendLog:
    sequence: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class LogWriterStatus:
    accepted: int
    written: int
    failed: int
    rejected: int
    last_error: str | None


class LogWriter:
    """One external-I/O actor owns append order and disk operations.

    Logs are observations, not durable business admission. Rejected and failed
    writes remain observable in a memory projection without recursively logging
    through the failing writer. A timed-out drain retains the accepted queue.
    """

    def __init__(self, append: Callable[[bytes], None], *, capacity: int = 4096):
        self._append = append
        self._pid = os.getpid()
        self._guard = threading.Condition()
        self._accepted = self._written = self._failed = self._rejected = 0
        self._first_failed_sequence: int | None = None
        self._last_error: str | None = None
        self._closed = False
        # Runtime lifecycle diagnostics must never feed back into this writer.
        self._runtime = ActorRuntime(event_sink=lambda event: None)
        self._handle = self._runtime.start(
            ActorSpec(
                name="log-writer",
                handler_factory=lambda: self._handle_append,
                mailbox_capacity=capacity,
                supervision_profile="external_io",
            )
        )

    def submit(self, payload: bytes) -> AdmissionResult:
        if os.getpid() != self._pid:
            return AdmissionResult.CLOSED
        with self._guard:
            sequence = self._accepted + 1
            result = (
                AdmissionResult.CLOSED
                if self._closed
                else self._runtime.tell(self._handle, AppendLog(sequence, payload))
            )
            if result is AdmissionResult.ACCEPTED:
                self._accepted = sequence
            else:
                self._rejected += 1
            return result

    def _handle_append(self, command: object) -> None:
        if not isinstance(command, AppendLog):
            raise TypeError("unsupported log writer command")
        try:
            self._append(command.payload)
        except Exception as error:
            with self._guard:
                self._failed += 1
                if self._first_failed_sequence is None:
                    self._first_failed_sequence = command.sequence
                # Exception text may contain credentials; keep only its type.
                self._last_error = type(error).__name__
                self._guard.notify_all()
        else:
            with self._guard:
                self._written += 1
                self._guard.notify_all()

    def status(self) -> LogWriterStatus:
        with self._guard:
            return LogWriterStatus(
                self._accepted,
                self._written,
                self._failed,
                self._rejected,
                self._last_error,
            )

    def wait_for_writes(
        self, *, target: int, rejected_at_capture: int, timeout: float = 2.0
    ) -> bool:
        """Wait for an exact accepted prefix without extending it for later logs.

        This observes the asynchronous writer; producers still only enqueue
        immutable bytes. An append failure in this prefix or a refusal already
        visible in the caller's capture returns false; later work cannot
        overturn its result.
        """

        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            if target < 0 or target > self._accepted or not 0 <= rejected_at_capture <= self._rejected:
                raise ValueError("log completion target was not accepted")
            if rejected_at_capture:
                return False
            while self._written + self._failed < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._guard.wait(remaining)
            return (
                self._first_failed_sequence is None
                or self._first_failed_sequence > target
            )

    def close(self, timeout: float = 1.0) -> bool:
        if os.getpid() != self._pid:
            return True
        with self._guard:
            self._closed = True
        return self._runtime.stop(self._handle, timeout=max(0.0, timeout))
