"""Bounded native query I/O ownership outside Zenoh callback threads."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult


@dataclass(frozen=True, slots=True)
class AnswerQuery:
    token: str
    selector: str
    payload: bytes | None


@dataclass(frozen=True, slots=True)
class QueryIoProjection:
    accepted: int
    rejected: int
    failed: int
    outstanding: int
    closed: bool


class QueryIoOwner:
    """One registration's native Query cells and immutable request tokens.

    Standalone callers retain the original actor mode. Production passes the
    transport's shared query dispatcher: no actor or thread is created per
    declaration, and the dispatcher retires each cell only after native drop.
    """

    def __init__(self, name, handler, *, capacity=128, on_error=None, dispatcher=None):
        if capacity < 1:
            raise ValueError("query owner capacity must be positive")
        self._name = name
        self._handler = handler
        self._on_error = on_error
        self._capacity = capacity
        self._dispatcher = dispatcher
        self._guard = threading.Condition()
        self._queries = {}
        self._closed = False
        self._accepted = self._rejected = self._failed = 0
        if dispatcher is None:
            self._runtime = ActorRuntime(event_sink=lambda event: None)
            self._handle = self._runtime.start(
                ActorSpec(
                    name=f"query-io:{name}",
                    handler_factory=lambda: self._answer,
                    mailbox_capacity=capacity,
                    supervision_profile="external_io",
                )
            )
        else:
            self._runtime = None
            self._handle = None

    def _reject_native(self, query) -> None:
        if self._dispatcher is None:
            return
        self._dispatcher.reject(self._name, query)

    def admit(self, query) -> AdmissionResult:
        try:
            raw = getattr(query, "payload", None)
            payload = (
                None
                if raw is None
                else (raw.to_bytes() if hasattr(raw, "to_bytes") else bytes(raw))
            )
            command = AnswerQuery(uuid4().hex, str(query.selector), payload)
        except Exception as error:
            with self._guard:
                self._rejected += 1
            self._reject_native(query)
            if self._on_error is not None:
                self._on_error(type(error).__name__)
            return AdmissionResult.OVERLOADED
        with self._guard:
            if self._closed or len(self._queries) >= self._capacity:
                self._rejected += 1
                rejected = (
                    AdmissionResult.CLOSED
                    if self._closed
                    else AdmissionResult.OVERLOADED
                )
            else:
                self._queries[command.token] = query
                rejected = None
        if rejected is not None:
            self._reject_native(query)
            return rejected
        if self._dispatcher is None:
            assert self._runtime is not None and self._handle is not None
            admission = self._runtime.tell(self._handle, command)
            with self._guard:
                if admission is AdmissionResult.ACCEPTED:
                    self._accepted += 1
                else:
                    self._queries.pop(command.token, None)
                    self._rejected += 1
            return admission
        try:
            admission = self._dispatcher.submit(
                self._name, self._answer, command, query,
                retire=self._retire, token=command.token,
            )
        except Exception as error:
            self._reject_native(query)
            self._retire(command.token)
            if self._on_error is not None:
                self._on_error(type(error).__name__)
            admission = AdmissionResult.OVERLOADED
        with self._guard:
            if admission is AdmissionResult.ACCEPTED:
                self._accepted += 1
            else:
                self._rejected += 1
        return admission

    def _retire(self, token: str) -> None:
        with self._guard:
            self._queries.pop(token, None)
            self._guard.notify_all()

    def _answer(self, command):
        if not isinstance(command, AnswerQuery):
            raise TypeError("unsupported native query command")
        with self._guard:
            query = self._queries[command.token]
        try:
            for key, payload in self._handler(command.selector, command.payload):
                query.reply(str(key), payload, encoding="application/octet-stream")
        except Exception as error:
            with self._guard:
                self._failed += 1
            if self._on_error is not None:
                try:
                    self._on_error(type(error).__name__)
                except Exception:
                    pass
            try:
                query.reply_err(b"QUERY_HANDLER_FAILED")
            except Exception:
                pass
        finally:
            if self._dispatcher is None:
                self._retire(command.token)

    def projection(self):
        with self._guard:
            return QueryIoProjection(
                self._accepted,
                self._rejected,
                self._failed,
                len(self._queries),
                self._closed,
            )

    def close(self, timeout=1.0):
        with self._guard:
            self._closed = True
        if self._dispatcher is None:
            assert self._runtime is not None and self._handle is not None
            return self._runtime.stop(self._handle, timeout)
        self._dispatcher.retry_cleanup()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            while self._queries:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._guard.wait(remaining)
            return True
