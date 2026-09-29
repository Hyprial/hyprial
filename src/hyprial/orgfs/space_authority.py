"""Per-space serialized durable edit authority.

Commands contain CRDT update bytes and attribution, never caller code. The
bounded effect worker performs the one-space journal transaction; its actor
retains accepted command and completion custody without doing disk I/O in the
mailbox.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
import uuid
import weakref
from typing import Any, Literal, Mapping

from pycrdt import Doc

from hyprial.actor_runtime import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
)
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest

from .store import ImportResult, StoreError


_SPACE_AUTHORITY_CAPACITY = 128


@dataclass(frozen=True, slots=True)
class _CommittedDelta:
    """The store's committed ops for one content doc past the caller's state.

    Every commit adds a reserved coverage-clock op under the store's writer
    client.  Peers receive it in the envelope, but the writer's own facade
    doc never sees it unless the authority hands the delta back with the
    commit result (D4 x D1 seam).
    """

    doc_id: str
    delta: bytes


@dataclass(frozen=True, slots=True)
class _Edit:
    doc_id: str
    update: bytes
    author: str
    actor: str | None
    absorb_since: bytes | None = None


@dataclass(frozen=True, slots=True)
class _EditBatch:
    edits: tuple[_Edit, ...]
    author: str
    actor: str | None


@dataclass(frozen=True, slots=True)
class _ImportEnvelope:
    envelope: bytes
    supplier: str


@dataclass(frozen=True, slots=True)
class _MarkBroadcast:
    writer: str
    seq: int
    doc_id: str


@dataclass(frozen=True, slots=True)
class _UnbroadcastThrough:
    keys: tuple[tuple[str, int, str], ...]


@dataclass(frozen=True, slots=True)
class _InstallReplacement:
    old_doc_id: str
    new_doc_id: str
    snapshot_bytes: bytes
    author: str | None
    actor: str | None


@dataclass(frozen=True, slots=True)
class _RecordHolderFrontier:
    node: str
    durable: bool
    frontiers: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True, slots=True)
class _DeleteRetiredObjects:
    old_doc_id: str


@dataclass(frozen=True, slots=True)
class _SavePurgePlan:
    plan_id: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class _JsonProjection:
    payload: bytes

    def decode(self) -> object:
        return json.loads(self.payload)


@dataclass(frozen=True, slots=True)
class _ReadStore:
    """Fixed read catalogue for transport query projections.

    Query handlers may ask the per-space authority for immutable read results,
    but cannot run arbitrary store callables or SQL on the transport callback.
    """

    kind: Literal[
        "document_ids",
        "unbroadcast",
        "snapshot",
        "snapshot_point",
        "frontier",
        "export_since",
        "commit_version",
        "log_range",
        "member_mode",
        "retired",
        "retirement_records",
        "pending_replacements",
        "purge_list_entries",
        "retired_residue",
        "covered",
        "purge_inventory",
        "load_purge_plan",
        "writer_seq_watermarks",
        "writer_attributions",
        "holder_frontiers",
        "active_tree_doc_id",
    ]
    doc_id: str | None = None
    writer: str | None = None
    seq: int | None = None
    version: bytes | None = None
    vv: bytes | None = None
    cursor: str | None = None
    shallow_since: bytes | None = None
    max_bytes: int | None = None
    after: int | None = None
    limit: int | None = None
    author: str | None = None
    plan_id: str | None = None


_SpaceOperation = (
    _Edit
    | _EditBatch
    | _ImportEnvelope
    | _MarkBroadcast
    | _UnbroadcastThrough
    | _InstallReplacement
    | _RecordHolderFrontier
    | _DeleteRetiredObjects
    | _SavePurgePlan
    | _ReadStore
)


@dataclass(frozen=True, slots=True)
class _SpaceCommand:
    operation_id: str
    generation: int
    operation: _SpaceOperation


@dataclass(frozen=True, slots=True)
class _SpaceFailure:
    kind: str
    code: str
    message: str
    details: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _SpaceOutcome:
    value: object = None
    error: _SpaceFailure | None = None


@dataclass(slots=True)
class _Waiter:
    done: threading.Event
    value: object = None
    error: _SpaceFailure | None = None


class OrgSpaceAuthority:
    """Bounded per-space writer lane for CRDT journal transactions."""

    def __init__(self, space_id: str, store: object) -> None:
        self.space_id = space_id
        self._store = store
        self._lock = threading.RLock()
        self._drained = threading.Condition(self._lock)
        self._generation = 1
        self._closing = False
        self._waiters: dict[str, _Waiter] = {}
        self._commands: dict[str, _SpaceCommand] = {}
        self._submitted: set[str] = set()
        self._deferred: list[_SpaceCommand] = []
        owner_ref = weakref.ref(self)

        def actor_event(event: ActorEvent) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_actor_event(event)

        def handle_command(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_command(command)

        def execute(command: _SpaceCommand) -> _SpaceOutcome:
            owner = owner_ref()
            if owner is None:
                return _SpaceOutcome(
                    error=_SpaceFailure(
                        "StoreError", "closed", "orgfs space authority is closed", ()
                    )
                )
            return owner._execute(command)

        def complete(
            completion: EffectCompleted[_SpaceOutcome],
        ) -> AdmissionResult:
            owner = owner_ref()
            if owner is None:
                return AdmissionResult.CLOSED
            return owner._runtime.tell(owner._actor, completion)

        self._runtime = ActorRuntime(event_sink=actor_event)
        self._actor: ActorHandle = self._runtime.start(
            ActorSpec(
                f"orgfs-space-{space_id[:12]}-authority",
                lambda: handle_command,
                mailbox_capacity=_SPACE_AUTHORITY_CAPACITY,
                supervision_profile="state_authority",
            )
        )
        self._effects: EffectLane[_SpaceCommand, _SpaceOutcome] = EffectLane(
            name=f"orgfs-space-{space_id[:12]}-writer",
            execute=execute,
            complete=complete,
            capacity=_SPACE_AUTHORITY_CAPACITY,
            workers=1,
        )

    @staticmethod
    def _apply_update(update: bytes):
        def mutate(document: Doc) -> None:
            document.apply_update(update)

        return mutate

    def _absorb(
        self, doc_id: str, absorb_since: bytes | None
    ) -> tuple[_CommittedDelta, ...]:
        """Committed ops the caller asked to fold back into its facade doc.

        Computed on the effect worker right after the commit, while no other
        authority operation can interleave, so the delta is atomic with the
        commit.  Only requested for caller-owned content docs; ``meta`` and
        the tree doc never carry an ``absorb_since``.
        """

        if absorb_since is None:
            return ()
        delta = self._store.committed_update(doc_id, bytes(absorb_since))
        if not delta:
            return ()
        return (_CommittedDelta(doc_id, delta),)

    def _execute(self, command: _SpaceCommand) -> _SpaceOutcome:
        try:
            operation = command.operation
            if isinstance(operation, _Edit):
                mutate = self._apply_update(operation.update)
                if hasattr(self._store, "commit_with_outbox"):
                    record, outbox_records = self._store.commit_with_outbox(
                        operation.doc_id,
                        mutate,
                        author=operation.author,
                        actor=operation.actor,
                    )
                else:
                    record = self._store.commit(
                        operation.doc_id,
                        mutate,
                        author=operation.author,
                        actor=operation.actor,
                    )
                    outbox_records = ()
                absorbs = self._absorb(operation.doc_id, operation.absorb_since)
                return _SpaceOutcome(value=(record, outbox_records, absorbs))
            if isinstance(operation, _EditBatch):
                mutations = tuple(
                    (edit.doc_id, self._apply_update(edit.update))
                    for edit in operation.edits
                )
                if hasattr(self._store, "commit_many_with_outbox"):
                    records, outbox_records = self._store.commit_many_with_outbox(
                        mutations, author=operation.author, actor=operation.actor
                    )
                else:
                    records = self._store.commit_many(
                        mutations, author=operation.author, actor=operation.actor
                    )
                    outbox_records = ()
                absorbs = tuple(
                    absorb
                    for edit in operation.edits
                    for absorb in self._absorb(edit.doc_id, edit.absorb_since)
                )
                return _SpaceOutcome(value=(records, outbox_records, absorbs))
            if isinstance(operation, _ImportEnvelope):
                return _SpaceOutcome(
                    value=self._store.import_envelope(
                        operation.envelope, supplier=operation.supplier
                    )
                )
            if isinstance(operation, _MarkBroadcast):
                self._store.mark_broadcast(
                    operation.writer, operation.seq, doc_id=operation.doc_id
                )
                return _SpaceOutcome()
            if isinstance(operation, _UnbroadcastThrough):
                return _SpaceOutcome(
                    value=self._store.unbroadcast_through(operation.keys)
                )
            if isinstance(operation, _InstallReplacement):
                self._store.install_replacement(
                    operation.old_doc_id,
                    operation.new_doc_id,
                    operation.snapshot_bytes,
                    author=operation.author,
                    actor=operation.actor,
                )
                return _SpaceOutcome()
            if isinstance(operation, _RecordHolderFrontier):
                self._store.record_holder_frontier(
                    operation.node,
                    operation.durable,
                    dict(operation.frontiers),
                )
                return _SpaceOutcome()
            if isinstance(operation, _DeleteRetiredObjects):
                value = self._store.delete_retired_objects(operation.old_doc_id)
                return _SpaceOutcome(value=tuple(value))
            if isinstance(operation, _SavePurgePlan):
                plan, targets = json.loads(operation.payload)
                self._store.save_purge_plan(operation.plan_id, plan, tuple(targets))
                return _SpaceOutcome()
            if isinstance(operation, _ReadStore):
                store = self._store
                if operation.kind == "document_ids":
                    value = tuple(store.document_ids())
                elif operation.kind == "unbroadcast":
                    value = tuple(store.unbroadcast())
                elif operation.kind == "snapshot":
                    value = store.snapshot(
                        operation.doc_id or "", shallow_since=operation.shallow_since
                    )
                elif operation.kind == "snapshot_point":
                    value = store.snapshot_point(operation.doc_id or "")
                elif operation.kind == "frontier":
                    value = bytes(store.frontier(operation.doc_id or ""))
                elif operation.kind == "export_since":
                    value = store.export_since(
                        operation.doc_id or "",
                        bytes(operation.vv or b""),
                        cursor=operation.cursor,
                        max_bytes=int(operation.max_bytes or 0),
                    )
                elif operation.kind == "commit_version":
                    value = store.commit_version(
                        operation.doc_id or "",
                        operation.writer or "",
                        int(operation.seq or 0),
                    )
                elif operation.kind == "log_range":
                    value = tuple(
                        store.log_range(
                            operation.doc_id or "",
                            operation.writer or "",
                            after=operation.after,
                            limit=int(operation.limit or 0),
                        )
                    )
                elif operation.kind == "member_mode":
                    value = store.member_mode(operation.author or "")
                elif operation.kind == "retired":
                    value = store.retired(operation.doc_id or "")
                elif operation.kind == "retirement_records":
                    value = tuple(store.retirement_records())
                elif operation.kind == "pending_replacements":
                    value = tuple(store.pending_replacements())
                elif operation.kind == "purge_list_entries":
                    value = tuple(sorted(store.purge_list_entries().items()))
                elif operation.kind == "retired_residue":
                    value = tuple(store.retired_residue(operation.doc_id or ""))
                elif operation.kind == "covered":
                    value = store._covered(
                        bytes(operation.version or b""), bytes(operation.vv or b"")
                    )
                elif operation.kind == "purge_inventory":
                    value = _JsonProjection(json.dumps(
                        store.purge_inventory(operation.doc_id or "")
                    ).encode())
                elif operation.kind == "load_purge_plan":
                    loaded = store.load_purge_plan(operation.plan_id or "")
                    value = None if loaded is None else _JsonProjection(json.dumps(loaded).encode())
                elif operation.kind == "writer_seq_watermarks":
                    value = tuple(
                        sorted(store.writer_seq_watermarks(operation.doc_id or "").items())
                    )
                elif operation.kind == "writer_attributions":
                    value = tuple(
                        sorted(
                            (writer, tuple(authors))
                            for writer, authors in store.writer_attributions().items()
                        )
                    )
                elif operation.kind == "holder_frontiers":
                    value = tuple(
                        sorted(
                            (holder, tuple(sorted((doc_id, bytes(frontier))
                             for doc_id, frontier in docs.items())))
                            for holder, docs in store.holder_frontiers().items()
                        )
                    )
                elif operation.kind == "active_tree_doc_id":
                    value = store.active_tree_doc_id()
                else:
                    raise TypeError(f"unknown orgfs read operation {operation.kind}")
                return _SpaceOutcome(value=value)
            raise TypeError(f"unknown orgfs space operation: {type(operation).__name__}")
        except Exception as error:
            details = getattr(error, "details", {})
            frozen = tuple(
                sorted((str(key), self._freeze(value)) for key, value in dict(details).items())
            ) if isinstance(details, dict) else ()
            return _SpaceOutcome(
                error=_SpaceFailure(
                    type(error).__name__,
                    str(getattr(error, "code", "internal")),
                    str(error),
                    frozen,
                )
            )

    @staticmethod
    def _freeze(value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return value
        if isinstance(value, dict):
            return tuple(sorted((str(key), OrgSpaceAuthority._freeze(item)) for key, item in value.items()))
        if isinstance(value, (tuple, list)):
            return tuple(OrgSpaceAuthority._freeze(item) for item in value)
        return str(value)

    def _on_command(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            self._complete(command)
            self._effects.acknowledge(command.operation_id, command.generation)
            self._pump_deferred()
            return
        if not isinstance(command, _SpaceCommand):
            raise TypeError("orgfs space authority received an invalid command")
        with self._lock:
            if command.generation != self._generation:
                return
            admission = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admission is AdmissionResult.ACCEPTED:
                self._deferred = [
                    item
                    for item in self._deferred
                    if item.operation_id != command.operation_id
                ]
                self._submitted.add(command.operation_id)
            elif admission is AdmissionResult.OVERLOADED:
                if all(
                    item.operation_id != command.operation_id
                    for item in self._deferred
                ):
                    self._deferred.append(command)
        if admission is AdmissionResult.CLOSED:
            self._complete(
                EffectCompleted(
                    command.operation_id,
                    command.generation,
                    result=_SpaceOutcome(
                        error=_SpaceFailure(
                            "StoreError",
                            "closed",
                            "orgfs space effect lane is closed",
                            (),
                        )
                    ),
                )
            )

    def _pump_deferred(self) -> None:
        with self._lock:
            if not self._deferred:
                return
            command = self._deferred[0]
            admission = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admission is AdmissionResult.ACCEPTED:
                self._deferred.pop(0)
                self._submitted.add(command.operation_id)

    def _complete(self, completion: EffectCompleted[_SpaceOutcome]) -> None:
        with self._lock:
            command = self._commands.get(completion.operation_id)
            if command is None or command.generation != completion.generation:
                return
            waiter = self._waiters.pop(completion.operation_id, None)
            self._commands.pop(completion.operation_id, None)
            self._submitted.discard(completion.operation_id)
            self._drained.notify_all()
        if waiter is None:
            return
        outcome = completion.result
        waiter.value = outcome.value if outcome is not None else None
        waiter.error = (
            outcome.error
            if outcome is not None
            else _SpaceFailure("StoreError", "internal", completion.error or "empty completion", ())
        )
        waiter.done.set()

    def _on_actor_event(self, event: ActorEvent) -> None:
        if (
            event.handle.name != f"orgfs-space-{self.space_id[:12]}-authority"
            or event.kind is not ActorEventKind.CHILD_RESTARTED
        ):
            return
        with self._lock:
            if event.generation <= self._generation:
                return
            self._generation = event.generation
            deferred_ids = {command.operation_id for command in self._deferred}
            replay = tuple(
                _SpaceCommand(command.operation_id, event.generation, command.operation)
                for operation_id, command in self._commands.items()
                if operation_id not in self._submitted
            )
            for command in replay:
                self._commands[command.operation_id] = command
            replacements = {command.operation_id: command for command in replay}
            self._deferred = [
                replacements[command.operation_id]
                for command in self._deferred
                if command.operation_id in replacements
            ]
        self._pump_deferred()
        for command in replay:
            if command.operation_id in deferred_ids:
                continue
            admission = self._runtime.tell(self._actor, command)
            if admission is not AdmissionResult.ACCEPTED:
                with self._lock:
                    if all(
                        item.operation_id != command.operation_id
                        for item in self._deferred
                    ):
                        self._deferred.append(command)
                self._pump_deferred()

    @staticmethod
    def _raise(error: _SpaceFailure) -> None:
        if error.kind == "StoreError":
            raise StoreError(error.code, error.message, **dict(error.details))
        raise RuntimeError(error.message)

    def _ask(self, operation: _SpaceOperation) -> Any:
        operation_id = uuid.uuid4().hex
        waiter = _Waiter(threading.Event())
        with self._lock:
            if self._closing:
                raise StoreError("closed", "orgfs space authority is closing")
            if len(self._commands) >= _SPACE_AUTHORITY_CAPACITY:
                raise StoreError(
                    "resource-exhausted",
                    "orgfs space authority total custody is full",
                )
            command = _SpaceCommand(operation_id, self._generation, operation)
            self._waiters[operation_id] = waiter
            self._commands[operation_id] = command
        while True:
            admission = self._runtime.tell(self._actor, command)
            if admission is AdmissionResult.ACCEPTED:
                break
            with self._lock:
                current = self._commands.get(operation_id)
                if (
                    admission is AdmissionResult.CLOSED
                    and current is not None
                    and current.generation != command.generation
                ):
                    command = current
                    continue
                self._commands.pop(operation_id, None)
                self._waiters.pop(operation_id, None)
            raise StoreError(
                "resource-exhausted" if admission is AdmissionResult.OVERLOADED else "closed",
                "orgfs space command admission was rejected",
            )
        # A timeout cannot roll back the facade's speculative CRDT update, so
        # this synchronous adapter waits for settlement; callers never mistake
        # an accepted journal transaction for cancellation.
        waiter.done.wait()
        if waiter.error is not None:
            self._raise(waiter.error)
        return waiter.value

    def commit(
        self,
        doc_id: str,
        update: bytes,
        *,
        author: str,
        actor: str | None,
        absorb_since: bytes | None = None,
    ) -> Any:
        return self._ask(_Edit(doc_id, bytes(update), author, actor, absorb_since))

    def commit_many(
        self,
        edits: tuple[tuple[str, bytes], ...],
        *,
        author: str,
        actor: str | None,
        absorb_since: Mapping[str, bytes] | None = None,
    ) -> Any:
        since = absorb_since or {}
        return self._ask(
            _EditBatch(
                tuple(
                    _Edit(
                        doc_id,
                        bytes(update),
                        author,
                        actor,
                        bytes(since[doc_id]) if doc_id in since else None,
                    )
                    for doc_id, update in edits
                ),
                author,
                actor,
            )
        )

    def import_envelope(self, envelope: bytes, *, supplier: str) -> ImportResult:
        return self._ask(_ImportEnvelope(bytes(envelope), supplier))

    def mark_broadcast(self, writer: str, seq: int, *, doc_id: str) -> None:
        self._ask(_MarkBroadcast(writer, seq, doc_id))

    def unbroadcast_through(
        self, keys: tuple[tuple[str, int, str], ...]
    ) -> tuple[Any, ...]:
        return self._ask(_UnbroadcastThrough(tuple(keys)))

    def install_replacement(
        self,
        old_doc_id: str,
        new_doc_id: str,
        snapshot_bytes: bytes,
        *,
        author: str | None = None,
        actor: str | None = None,
    ) -> None:
        self._ask(
            _InstallReplacement(
                old_doc_id, new_doc_id, bytes(snapshot_bytes), author, actor
            )
        )

    def record_holder_frontier(
        self, node: str, durable: bool, frontiers: Mapping[str, bytes]
    ) -> None:
        self._ask(
            _RecordHolderFrontier(
                node,
                bool(durable),
                tuple(sorted((str(key), bytes(value)) for key, value in frontiers.items())),
            )
        )

    def delete_retired_objects(self, old_doc_id: str) -> tuple[str, ...]:
        return tuple(self._ask(_DeleteRetiredObjects(old_doc_id)))

    def save_purge_plan(
        self,
        plan_id: str,
        plan: Mapping[str, object],
        targets: tuple[Mapping[str, object], ...],
    ) -> None:
        # JSON bytes retain empty arrays/objects and pair-shaped arrays without
        # inferring their type from tuple contents across the actor boundary.
        payload = json.dumps([dict(plan), tuple(dict(target) for target in targets)]).encode()
        self._ask(_SavePurgePlan(plan_id, payload))

    def read(self, operation: _ReadStore) -> Any:
        return self._ask(operation)

    def close(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closing = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self._runtime.snapshot(self._actor)
            if snapshot.queued == 0 and snapshot.in_flight == 0 and not self._deferred:
                break
            time.sleep(0.005)
        else:
            return False
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        if not self._runtime.stop(self._actor, max(0.0, deadline - time.monotonic())):
            return False
        self._generation += 1
        return True
