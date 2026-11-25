"""``TeleportTargetService``: the target-side state machine of agent teleport.

Decisions:

- Operation state lives in memory only (protocol section 3).  ``state()``
  reports ``prepared``, ``staging`` (after the first accepted chunk),
  ``received``, ``committed`` or ``None`` (unknown or forgotten).
- prepare runs the six checks of section 2 in order.  ``RUNTIME_UNSUPPORTED``
  also covers a harness outside ``claude``/``codex``/``pi`` and a non-empty
  routine list without a routines port.  A routines port answers
  ``ROUTINE_CONFLICT``.  A dry run runs every check and registers nothing.
  The operation remembers prepare's source agent URI and routine names.
- receive accepts the harness announced by prepare and exactly the routine
  names announced by prepare (any order, no duplicates), checked before any
  port call, then checked against ``existing`` again before landing.
  Routines are created paused by the pipeline between activate and start;
  commit resumes those whose ``restore_enabled`` is true and reports each as a
  ``RoutineOutcome`` (a resume that raises is ``resumed=False`` and the others
  still resume).  Every receive failure forgets the operation and removes its
  staging.  One receive runs at a time: a second is answered at once with
  ``OPERATION_CONFLICT`` and leaves its operation untouched.
- A receive reply is stored; repeating receive on a ``received`` or
  ``committed`` operation returns it without calling a port.  commit is
  idempotent the same way.  abort of an unknown operation is idempotent.
- ``busy`` marks an operation whose receive is running: chunk, commit and
  abort on it are ``OPERATION_CONFLICT`` so staging is never removed under the
  pipeline.
- ``close`` first flips ``closed`` under the lock: from then on prepare,
  chunk, receive and commit answer ``TELEPORT_OPERATION_CONFLICT`` ("shutting
  down") as an ``ok:false`` reply, while a stored receive/commit reply is still
  replayed and abort stays idempotent.  An ``ok:false`` receive verdict makes
  the source restart itself without abort; silence would leave it UNRESOLVED
  with the agent stopped, so a not-yet-started receive is never left unanswered.
- A receive already in flight when ``close`` begins is allowed to finish (or
  undo itself) while ``close`` waits at most ``close_wait_seconds`` for it;
  the daemon shutdown calls ``close`` before any domain the receive depends on
  is torn down.  A receive still running after the bound is not interrupted
  and keeps its outcome (the wait is a bound, not a cancel).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.frames import ChunkRequest
from hyprial.daemon.impl.teleport.protocol import (
    AbortReply,
    AbortRequest,
    CommitReply,
    CommitRequest,
    PrepareReply,
    PrepareRequest,
    ReceiveReply,
    ReceiveRequest,
    RoutineOutcome,
)
from hyprial.daemon.impl.teleport.staging import StagingReceiver
from hyprial.daemon.impl.teleport.target import pipeline
from hyprial.daemon.impl.teleport.target.ports import TargetHost, TargetRoutines
from hyprial.daemon.impl.teleport.target.query import handle_query
from hyprial.daemon.impl.transport import KeySpace

SUPPORTED_HARNESSES = frozenset({"claude", "codex", "pi"})


@dataclass(slots=True)
class _Operation:
    harness: str
    agent: str
    source_uri: str = ""
    routines: tuple[str, ...] = ()
    state: str = "prepared"
    busy: bool = False
    receive_reply: ReceiveReply | None = None
    commit_reply: CommitReply | None = None


class TeleportTargetService:
    def __init__(
        self,
        *,
        host: TargetHost,
        receiver: StagingReceiver,
        routines: TargetRoutines | None = None,
        reply_margin_seconds: float = 60.0,
        commit_timeout_seconds: float = 15.0,
        key_space: KeySpace | None = None,
        close_wait_seconds: float = 5.0,
    ) -> None:
        self.host = host
        self.close_wait_seconds = close_wait_seconds
        self.receiver = receiver
        self.routines = routines
        self.reply_margin_seconds = reply_margin_seconds
        self.commit_timeout_seconds = commit_timeout_seconds
        self.key_space = key_space or KeySpace()
        self.closed = False
        self._operations: dict[str, _Operation] = {}
        self._lock = threading.RLock()
        self._settled = threading.Condition(self._lock)
        self._receiving = 0
        self._receive_slot = threading.Lock()

    @property
    def node_id(self) -> str:
        return self.host.node_id

    def state(self, operation_id: str) -> str | None:
        with self._lock:
            operation = self._operations.get(operation_id)
            return operation.state if operation is not None else None

    def prepare(self, request: PrepareRequest) -> PrepareReply:
        host = self.host
        self._refuse_when_closed()
        if request.target_node != host.node_id:
            raise TeleportError(
                "TELEPORT_WRONG_TARGET", f"node {request.target_node!r} is not {host.node_id!r}"
            )
        if request.owner != host.owner:
            raise TeleportError(
                "TELEPORT_OWNER_MISMATCH", f"owner {request.owner!r} is not {host.owner!r}"
            )
        if request.harness not in SUPPORTED_HARNESSES or not host.runtime_supported(
            request.harness
        ):
            raise TeleportError(
                "TELEPORT_RUNTIME_UNSUPPORTED", f"harness {request.harness!r} is not available"
            )
        if host.agent_exists(request.agent):
            raise TeleportError(
                "TELEPORT_AGENT_EXISTS", f"agent {request.agent!r} already exists"
            )
        if request.routines:
            if self.routines is None:
                raise TeleportError(
                    "TELEPORT_RUNTIME_UNSUPPORTED",
                    "this node cannot receive routines: no routine runtime",
                )
            taken = self.routines.existing(request.routines)
            if taken:
                raise TeleportError(
                    "TELEPORT_ROUTINE_CONFLICT", f"routines already exist: {sorted(taken)}"
                )
        public_key = host.recipient_public_key()
        with self._lock:
            if request.operation_id in self._operations:
                raise TeleportError(
                    "TELEPORT_OPERATION_CONFLICT",
                    f"operation {request.operation_id!r} is already prepared",
                )
            if not request.dry_run:
                self.receiver.prepare(request.operation_id)
                self._operations[request.operation_id] = _Operation(
                    harness=request.harness,
                    agent=request.agent,
                    source_uri=request.source_agent_uri,
                    routines=tuple(request.routines),
                )
        return PrepareReply(
            operation_id=request.operation_id,
            target_node=host.node_id,
            recipient_public_key=public_key,
            dry_run=request.dry_run,
        )

    def chunk(self, request: ChunkRequest) -> int:
        with self._lock:
            self._refuse_when_closed()
            operation = self._require(request.operation_id)
            if operation.state in ("received", "committed") or operation.busy:
                raise TeleportError(
                    "TELEPORT_OPERATION_CONFLICT",
                    f"operation {request.operation_id!r} no longer accepts chunks",
                )
            persisted = self.receiver.accept_chunk(request)
            operation.state = "staging"
            return persisted

    def receive(self, request: ReceiveRequest) -> ReceiveReply:
        operation_id = request.operation_id
        with self._lock:
            operation = self._require(operation_id)
            if operation.receive_reply is not None:
                return operation.receive_reply
            self._refuse_when_closed()
            if operation.busy or not self._receive_slot.acquire(blocking=False):
                raise TeleportError(
                    "TELEPORT_OPERATION_CONFLICT", "another receive is running on this target"
                )
            operation.busy = True
            self._receiving += 1
        undo: list[Any] = []
        try:
            reply = self._execute(operation, request, undo)
        except BaseException:
            self._run_undo(undo)
            with self._lock:
                self._operations.pop(operation_id, None)
            self.receiver.abort(operation_id)
            raise
        finally:
            with self._settled:
                operation.busy = False
                self._receiving -= 1
                self._settled.notify_all()
            self._receive_slot.release()
        with self._lock:
            operation.receive_reply = reply
            operation.state = "received"
        return reply

    def commit(self, request: CommitRequest) -> CommitReply:
        with self._lock:
            operation = self._require(request.operation_id)
            if operation.commit_reply is not None:
                return operation.commit_reply
            self._refuse_when_closed()
            if operation.state != "received" or operation.busy:
                raise TeleportError(
                    "TELEPORT_OPERATION_CONFLICT",
                    f"operation {request.operation_id!r} is {operation.state}, not received",
                )
            self.receiver.abort(request.operation_id)
            operation.commit_reply = CommitReply(
                operation_id=request.operation_id,
                routines=self._resume_routines(operation),
            )
            operation.state = "committed"
            return operation.commit_reply

    def abort(self, request: AbortRequest) -> AbortReply:
        with self._lock:
            operation = self._operations.get(request.operation_id)
            if operation is None:
                return AbortReply(operation_id=request.operation_id)
            if operation.state in ("received", "committed") or operation.busy:
                raise TeleportError(
                    "TELEPORT_OPERATION_CONFLICT",
                    f"operation {request.operation_id!r} is {operation.state}; the agent stays",
                )
            del self._operations[request.operation_id]
            self.receiver.abort(request.operation_id)
        return AbortReply(operation_id=request.operation_id)

    def handle_query(self, selector: str, payload: bytes | None) -> list[tuple[str, bytes]]:
        return handle_query(self, selector, payload)

    def close(self) -> None:
        with self._settled:
            self.closed = True
            self._settled.wait_for(
                lambda: self._receiving == 0, timeout=self.close_wait_seconds
            )

    def _refuse_when_closed(self) -> None:
        if self.closed:
            raise TeleportError(
                "TELEPORT_OPERATION_CONFLICT", "this target is shutting down; retry later"
            )

    def _resume_routines(self, operation: _Operation) -> tuple[RoutineOutcome, ...]:
        reply = operation.receive_reply
        if reply is None:
            return ()
        outcomes: list[RoutineOutcome] = []
        for item in reply.routines:
            resumed = False
            if item.restore_enabled and self.routines is not None:
                try:
                    self.routines.resume(item.name)
                    resumed = True
                except Exception:
                    resumed = False
            outcomes.append(
                RoutineOutcome(item.name, enabled=item.restore_enabled, resumed=resumed)
            )
        return tuple(outcomes)

    def _require(self, operation_id: str) -> _Operation:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise TeleportError(
                "TELEPORT_UNKNOWN_OPERATION", f"operation {operation_id!r} is not prepared"
            )
        return operation

    def _execute(
        self, operation: _Operation, request: ReceiveRequest, undo: list[Any]
    ) -> ReceiveReply:
        names = [item.name for item in request.routines]
        if len(set(names)) != len(names) or set(names) != set(operation.routines):
            raise TeleportError(
                "TELEPORT_BAD_FRAME",
                f"routines {sorted(names)} differ from prepare's {sorted(operation.routines)}",
            )
        if request.harness != operation.harness:
            raise TeleportError(
                "TELEPORT_BAD_FRAME",
                f"harness {request.harness!r} differs from prepare's {operation.harness!r}",
            )
        try:
            self._recheck_routines(names)
            bundle_dir = pipeline.unpack_and_verify(self.receiver, request)
            return pipeline.land_and_start(
                self.host,
                bundle_dir,
                name=operation.agent,
                harness=operation.harness,
                request=request,
                on_undo=undo.append,
                routines=self.routines,
                source_uri=operation.source_uri,
            )
        except TeleportError:
            raise
        except Exception as error:
            raise TeleportError(
                "TELEPORT_LANDING_FAILED", f"receive failed: {type(error).__name__}: {error}"
            ) from error

    def _recheck_routines(self, names: list[str]) -> None:
        if not names or self.routines is None:
            return
        taken = self.routines.existing(names)
        if taken:
            raise TeleportError(
                "TELEPORT_ROUTINE_CONFLICT", f"routines already exist: {sorted(taken)}"
            )

    @staticmethod
    def _run_undo(undo: list[Any]) -> None:
        for step in reversed(undo):
            try:
                step()
            except Exception:
                continue
