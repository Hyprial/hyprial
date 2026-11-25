"""Correlated request/reply between the reconcile coordinator and its actors.

Decision notes (cell model slice I6-2, actor boundary gate):

- Actor-domain code never calls a blocking ``ActorCell.ask``; the only
  sanctioned blocking site is the kernel actor-runtime port.  The coordinator
  therefore ``tell``s a ``ReconcileRequest`` carrying a one-shot
  ``ReconcileReply``; the handler completes the reply (or fails it when the
  command raises) before returning, the same tell-plus-correlated-reply shape
  as the harness actor facade.
- A request whose ``tell`` is not ``ACCEPTED`` (overloaded, closed) or raises
  is refused at once: the caller never waits for an actor that has not taken
  the command.
- The caller's wait is bounded by the coordinator's request timeout, and
  ``ReconcileRequests.close`` fails every outstanding reply so a coordinator
  shutdown releases waiting callers immediately instead of at the deadline.
- Every refusal is fail-closed: the coordinator maps it to "no snapshot" or
  ``False`` exactly as the earlier ask-based path did.
- ``settle_request`` fails a reply with a ``ReconcileRequestRefused`` naming
  the handler's error, never the raw exception object: an exception that is
  not an ``Exception`` (``SystemExit``, ``KeyboardInterrupt``) must not cross
  into the caller's thread, where the coordinator only catches ``Exception``.
  The original error is still re-raised in the actor so supervision sees it.
- A ``BeginWorkCommand`` whose reply is already settled (the caller timed out
  or the coordinator closed) is skipped: the caller was told ``False`` and
  will never call ``finish_work``, so claiming the slot would hold it forever
  and block that member's drain.  Other commands still run after a timeout:
  they settle document, effect or rebuild state that later work depends on.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.cells.model import Cell
from hyprial.kernel import AdmissionResult

from .effects import MemberEffectCompletion


@dataclass(frozen=True, slots=True)
class ReconcileDocCommand:
    cell: Cell
    recheck: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CompleteEffectCommand:
    completion: MemberEffectCompletion


@dataclass(frozen=True, slots=True)
class BeginWorkCommand:
    member: str
    work_id: str


@dataclass(frozen=True, slots=True)
class FinishWorkCommand:
    member: str
    work_id: str


@dataclass(frozen=True, slots=True)
class RebuildCommand:
    pass


class ReconcileRequestRefused(RuntimeError):
    """The reconcile actor did not take, or could not answer, a request."""


class ReconcileReply:
    """One-shot reply slot completed by the reconcile actor handler."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._value: object = None
        self._error: BaseException | None = None

    @property
    def settled(self) -> bool:
        return self._ready.is_set()

    def complete(self, value: object) -> None:
        with self._lock:
            if self._ready.is_set():
                return
            self._value = value
            self._ready.set()

    def fail(self, error: BaseException) -> None:
        with self._lock:
            if self._ready.is_set():
                return
            self._error = error
            self._ready.set()

    def wait(self, timeout: float) -> object:
        if not self._ready.wait(timeout):
            self.fail(TimeoutError("cell reconcile actor reply deadline elapsed"))
        if self._error is not None:
            raise self._error
        return self._value


@dataclass(frozen=True, slots=True)
class ReconcileRequest:
    """A coordinator command plus the reply the handler must settle."""

    command: object
    reply: ReconcileReply


def settle_request(request: ReconcileRequest, dispatch: Callable[[object], object]) -> object:
    """Run *request* through *dispatch* in the actor and settle its reply."""

    if request.reply.settled and isinstance(request.command, BeginWorkCommand):
        return False
    try:
        value = dispatch(request.command)
    except BaseException as error:
        request.reply.fail(ReconcileRequestRefused(f"{type(error).__name__}: {error}"))
        raise
    request.reply.complete(value)
    return value


class ReconcileRequests:
    """Issue correlated requests and release them all on close."""

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._pending: set[ReconcileReply] = set()
        self._closed = False

    def request(self, actor: Any, command: object) -> object:
        reply = ReconcileReply()
        with self._lock:
            if self._closed:
                raise ReconcileRequestRefused("cell reconcile coordinator closed")
            self._pending.add(reply)
        try:
            admission = actor.tell(ReconcileRequest(command, reply))
            if admission is not AdmissionResult.ACCEPTED:
                raise ReconcileRequestRefused(f"cell reconcile actor admission {admission}")
            return reply.wait(self._timeout_s)
        finally:
            with self._lock:
                self._pending.discard(reply)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
        for reply in pending:
            reply.fail(ReconcileRequestRefused("cell reconcile coordinator closed"))


__all__ = [
    "BeginWorkCommand",
    "CompleteEffectCommand",
    "FinishWorkCommand",
    "RebuildCommand",
    "ReconcileDocCommand",
    "ReconcileReply",
    "ReconcileRequest",
    "ReconcileRequestRefused",
    "ReconcileRequests",
    "settle_request",
]
