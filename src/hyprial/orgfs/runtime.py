"""Daemon-owned composition for the P1 orgfs store, mesh, and local facade."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import Lock, RLock
import threading
import time
from typing import Any, Final, Mapping
import uuid
import weakref

from hyprial.actor_runtime import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
)
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from hyprial.transport import KeySpace, Registration, TransportSample, TransportSession

from .api import OrgFsError, SpaceInfo, SpaceStatus
from .blob_authority import BlobAuthority
from .checkout_authority import CheckoutAuthority
from .docs import LocalOrgFs
from .mesh import (
    ORGFS_ANNOUNCE_BUFFER_LIMIT,
    ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT,
    OrgFsMesh,
)
from .replica import FsReplicaBackend, MemoryReplicaBackend, ReplicaStore
from .replica_authority import ReplicaAuthority
from .space_authority import _ReadStore
from .store import CommitRecord, LocalSpaceStore, StoreError, state_covers


@dataclass(frozen=True, slots=True)
class _DirectoryCommand:
    operation_id: str
    generation: int
    action: str
    sample: TransportSample


@dataclass(frozen=True, slots=True)
class _DirectoryEffect:
    action: str
    peer: str | None = None
    space_ids: tuple[str, ...] = ()
    details: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _DirectoryEffectBatch:
    effects: tuple[_DirectoryEffect, ...]


@dataclass(frozen=True, slots=True)
class _JoinSpace:
    space_id: str


@dataclass(frozen=True, slots=True)
class _ServeSpace:
    space_id: str
    backend: str


@dataclass(frozen=True, slots=True)
class _CheckoutSpace:
    space_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class _OpenSpaceMesh:
    space_id: str


@dataclass(frozen=True, slots=True)
class _SpaceResourceHandle:
    space_id: str
    generation: int


@dataclass(slots=True)
class _MeshCreation:
    done: threading.Event = field(default_factory=threading.Event)
    handle: _SpaceResourceHandle | None = None
    error: BaseException | None = None


_DirectoryCallOperation = _JoinSpace | _ServeSpace | _CheckoutSpace | _OpenSpaceMesh


@dataclass(frozen=True, slots=True)
class _DirectoryCall:
    operation_id: str
    generation: int
    operation: _DirectoryCallOperation


@dataclass(frozen=True, slots=True)
class _DirectoryCallOutcome:
    value: object = None
    error: str | None = None


@dataclass(slots=True)
class _DirectoryWaiter:
    done: threading.Event
    value: object = None


@dataclass(frozen=True, slots=True)
class _LifecycleFailure:
    kind: str
    code: str
    message: str
    details: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _LifecycleOutcome:
    value: object = None
    error: _LifecycleFailure | None = None


@dataclass(frozen=True, slots=True)
class _LifecycleCommand:
    operation_id: str
    generation: int
    operation: _DirectoryCallOperation


@dataclass(slots=True)
class _LifecycleWaiter:
    done: threading.Event
    value: object = None
    error: _LifecycleFailure | None = None


@dataclass(slots=True)
class _LifecycleAuthority:
    runtime: ActorRuntime
    actor: ActorHandle
    effects: EffectLane[_LifecycleCommand, _LifecycleOutcome]
    generation: int = 1
    commands: dict[str, _LifecycleCommand] = field(default_factory=dict)
    submitted: set[str] = field(default_factory=set)
    waiters: dict[str, _LifecycleWaiter] = field(default_factory=dict)
    mesh_opening: dict[str, str] = field(default_factory=dict)
    mesh_aliases: dict[str, list[str]] = field(default_factory=dict)
    deferred: list[_LifecycleCommand] = field(default_factory=list)

ORGFS_CONTENT_WAIT_S: Final[float] = 10.0


class _StoreRegistry(dict[str, LocalSpaceStore]):
    def __init__(self, runtime: "OrgFsRuntime") -> None:
        super().__init__()
        self.runtime = runtime
        self._entries_lock = Lock()
        self._space_locks: dict[str, Lock] = {}

    def _space_lock(self, key: str) -> Lock:
        with self._entries_lock:
            return self._space_locks.setdefault(key, Lock())

    def get(self, key: str, default: Any = None) -> LocalSpaceStore:
        lock = self._space_lock(key)
        with lock:
            with self._entries_lock:
                existing = dict.get(self, key)
            if existing is not None:
                return existing
            created = LocalSpaceStore(
                self.runtime.state_dir,
                key,
                node_id=self.runtime.node_id,
                blob_store=self.runtime.blobs,
            )
            with self._entries_lock:
                existing = dict.get(self, key)
                if existing is None:
                    dict.__setitem__(self, key, created)
                    return created
            created.close()
            return existing

    def snapshot_ids(self) -> tuple[str, ...]:
        with self._entries_lock:
            return tuple(self.keys())

    def snapshot(self) -> tuple[LocalSpaceStore, ...]:
        with self._entries_lock:
            return tuple(self.values())

    def clear(self) -> None:
        with self._entries_lock:
            super().clear()


#: Backoff between await_content provisioning attempts for one blob.
ORGFS_AWAIT_RETRY_SECONDS = 0.1


class OrgFsRuntime:
    """Own all orgfs resources for one daemon generation."""

    def __init__(
        self,
        state_dir: Path,
        *,
        node_id: str,
        author: str,
        actor: str | None = None,
        logger: Callable[..., None] | None = None,
        owner_notifier: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.node_id = node_id
        self.author = author
        self.actor = actor
        self.logger = logger
        self.owner_notifier = owner_notifier
        self.blobs = BlobAuthority(self.state_dir)
        self.stores = _StoreRegistry(self)
        self.facade = LocalOrgFs(
            self.stores,
            self.blobs,
            self,
            author=author,
            actor=actor,
            node_id=node_id,
        )
        self._resource_lock = RLock()
        self._closing = False
        self._closed = False
        self._close_lock = Lock()
        self._checkouts: dict[str, CheckoutAuthority] = {}
        self._checkout_lock = Lock()
        self._checkout_config_lock = Lock()
        self._checkout_closing = False
        self._checkout_config = self.state_dir / "orgfs" / "checkouts.json"
        self._replicas: dict[str, ReplicaAuthority] = {}
        self._replica_creation_locks: dict[str, Lock] = {}
        self._mesh_creations: dict[str, _MeshCreation] = {}
        spaces_root = self.state_dir / "orgfs" / "spaces"
        if spaces_root.is_dir():
            for path in sorted(spaces_root.iterdir()):
                if not path.is_dir() or not (path / "journal.sqlite3").is_file():
                    continue
                self.stores.get(path.name)
                self.facade.load_space(path.name)
        self._restore_checkouts()
        self._session: TransportSession | None = None
        self._supplier_online: Callable[[str], bool] = lambda _supplier: True
        self._holder_candidates: Callable[[], tuple[str, ...]] = lambda: ()
        self._meshes: dict[str, OrgFsMesh] = {}
        self._announce_registration: Registration | None = None
        self._liveliness_registration: Registration | None = None
        self._holders: dict[str, set[str]] = {}
        self._known_holders: dict[str, set[str]] = {}
        self._lively_peers: set[str] = set()
        self._pending_announces: dict[str, dict[str, Any]] = {}
        self._pending_announces_lock = Lock()
        self._announce_buffer_dropped = 0
        self._announce_buffer_log_bucket = -1
        self._directory_generation = 1
        self._directory_runtime: ActorRuntime | None = None
        self._directory_actor: ActorHandle | None = None
        self._directory_effects: EffectLane[
            _DirectoryEffectBatch, None
        ] | None = None
        self._directory_closing = False
        self._directory_dropped = 0
        self._directory_custody_lock = Lock()
        self._directory_pending: set[tuple[str, int]] = set()
        # One additional bounded envelope must reach the state owner so a full
        # retained announce buffer can evict its oldest entry and report that
        # semantic drop. Total queued + active + deferred custody remains fixed.
        self._directory_capacity = ORGFS_ANNOUNCE_BUFFER_LIMIT + 1
        self._directory_deferred: list[EffectRequest[_DirectoryEffectBatch]] = []
        self._lifecycle_lock = Lock()
        self._lifecycle_authority: _LifecycleAuthority | None = None
        self._lifecycle_context = threading.local()
        self._lifecycle_closing = False

    @property
    def directory_ingress_dropped(self) -> int:
        return self._directory_dropped

    def _start_directory_ingress(self) -> None:
        if self._directory_effects is not None:
            return
        owner_ref = weakref.ref(self)

        def handle_command(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_directory_command(command)

        def execute_effect(effect: _DirectoryEffectBatch) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._execute_directory_effect(effect)

        runtime = ActorRuntime()
        handle = runtime.start(
            ActorSpec(
                "orgfs-directory-ingress",
                lambda: handle_command,
                # The custody bound includes one overflow envelope so the
                # state owner can evict/report a full retained buffer. Admit
                # that same bounded envelope even before the worker dequeues.
                mailbox_capacity=self._directory_capacity,
                supervision_profile="state_authority",
            )
        )
        self._directory_runtime = runtime
        self._directory_actor = handle
        self._directory_effects = EffectLane(
            name="orgfs-directory-effects",
            execute=execute_effect,
            complete=lambda completion: runtime.tell(handle, completion),
            capacity=ORGFS_ANNOUNCE_BUFFER_LIMIT,
            workers=1,
        )

    @property
    def directory_ingress_pending(self) -> int:
        with self._directory_custody_lock:
            return len(self._directory_pending)

    def _release_directory_credit(self, operation_id: str, generation: int) -> None:
        with self._directory_custody_lock:
            self._directory_pending.discard((operation_id, generation))

    def _admit_directory_sample(self, action: str, sample: TransportSample) -> None:
        runtime = self._directory_runtime
        actor = self._directory_actor
        with self._directory_custody_lock:
            if (runtime is None or actor is None or self._directory_closing
                or len(self._directory_pending) >= self._directory_capacity):
                self._directory_dropped += 1
                return
            command = _DirectoryCommand(
                uuid.uuid4().hex, self._directory_generation, action, sample
            )
            token = (command.operation_id, command.generation)
            self._directory_pending.add(token)
            if runtime.tell(actor, command) is not AdmissionResult.ACCEPTED:
                self._directory_pending.discard(token)
                self._directory_dropped += 1

    def _on_directory_command(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            effects = self._directory_effects
            if effects is not None:
                if effects.acknowledge(command.operation_id, command.generation):
                    self._release_directory_credit(command.operation_id, command.generation)
                self._pump_directory_effects()
            if command.error is not None:
                self._directory_dropped += 1
            return
        if not isinstance(command, _DirectoryCommand):
            raise TypeError("orgfs directory ingress received an invalid command")
        lane = self._directory_effects
        if lane is None or command.generation != self._directory_generation:
            self._directory_dropped += 1
            self._release_directory_credit(command.operation_id, command.generation)
            return
        if command.action == "announce":
            effects = self._apply_announcement_command(command.sample)
        elif command.action == "liveliness":
            effects = self._apply_liveliness_command(command.sample)
        else:
            self._directory_dropped += 1
            self._release_directory_credit(command.operation_id, command.generation)
            return
        if not effects:
            self._release_directory_credit(command.operation_id, command.generation)
            return
        batch = _DirectoryEffectBatch(tuple(effects))
        request = EffectRequest(command.operation_id, command.generation, batch)
        if not any(item.operation_id == request.operation_id for item in self._directory_deferred):
            self._directory_deferred.append(request)
        self._pump_directory_effects()

    def _pump_directory_effects(self) -> None:
        lane = self._directory_effects
        if lane is None:
            return
        while self._directory_deferred:
            request = self._directory_deferred[0]
            admission = lane.submit(request)
            if admission is AdmissionResult.OVERLOADED:
                return
            self._directory_deferred.pop(0)
            if admission is AdmissionResult.CLOSED:
                self._directory_dropped += 1
                self._release_directory_credit(request.operation_id, request.generation)

    def _apply_announcement_command(
        self, sample: TransportSample
    ) -> list[_DirectoryEffect]:
        try:
            value = json.loads(sample.payload)
            prefix = f"{KeySpace().prefix}/org/fs/announce/"
            if not sample.key.startswith(prefix):
                return []
            node = sample.key[len(prefix) :]
            if (
                not isinstance(value, dict)
                or value.get("schemaVersion") != 1
                or value.get("type") != "orgfs-announce"
                or value.get("node") != node
                or not node
                or "/" in node
                or not isinstance(value.get("spaces"), list)
            ):
                return []
            with self._pending_announces_lock:
                observed_live = node in self._lively_peers
            if not observed_live and not self._supplier_online(node):
                diagnostics: list[dict[str, object]] = []
                self._buffer_announce(node, value, diagnostics=diagnostics)
                return [
                    _DirectoryEffect(
                        "log",
                        details=tuple(sorted(fields.items())),
                    )
                    for fields in diagnostics
                ]
            with self._pending_announces_lock:
                self._pending_announces.pop(node, None)
            self._apply_announce(node, value)
            return []
        except Exception:
            self._directory_dropped += 1
            return []

    def _apply_liveliness_command(
        self, sample: TransportSample
    ) -> list[_DirectoryEffect]:
        peer, pending = self._settle_peer_liveliness(sample)
        if not peer or peer == self.node_id:
            return []
        if sample.kind == "delete":
            return []
        if pending is not None:
            self._apply_announce(peer, pending)
        effects = [_DirectoryEffect("announce")]
        checkout_spaces = tuple(self._checkouts)
        if checkout_spaces:
            effects.append(_DirectoryEffect("reconcile", space_ids=checkout_spaces))
        sync_spaces: list[str] = []
        for space_id in self.stores.snapshot_ids():
            with self._pending_announces_lock:
                holders = frozenset(self._holders.get(space_id, ()))
            if holders and peer not in holders:
                continue
            sync_spaces.append(space_id)
        if sync_spaces:
            effects.append(
                _DirectoryEffect("sync", peer=peer, space_ids=tuple(sync_spaces))
            )
        return effects

    def _execute_directory_effect(self, batch: _DirectoryEffectBatch) -> None:
        for effect in batch.effects:
            if effect.action == "announce":
                self._announce_all()
            elif effect.action == "reconcile":
                for space_id in effect.space_ids:
                    with self._checkout_lock:
                        checkout = self._checkouts.get(space_id)
                    if checkout is None:
                        continue
                    self._request_checkout_reconcile(checkout)
            elif effect.action == "sync" and effect.peer is not None:
                for space_id in effect.space_ids:
                    mesh = self._mesh(space_id)
                    if mesh is not None:
                        mesh.schedule_sync_from(effect.peer)
            elif effect.action == "log" and self.logger is not None:
                self.logger("warn", "orgfs.announce.buffer-dropped", **dict(effect.details))

    @staticmethod
    def _request_checkout_reconcile(checkout: Any) -> None:
        if isinstance(checkout, CheckoutAuthority):
            checkout.request_reconcile()
        else:
            # Test/offline protocol adapters may expose only the frozen legacy
            # reconcile seam; production entries are always CheckoutAuthority.
            checkout.reconcile()

    def _close_directory_ingress(self, timeout: float = 5.0) -> bool:
        with self._directory_custody_lock:
            self._directory_closing = True
        runtime = self._directory_runtime
        actor = self._directory_actor
        effects = self._directory_effects
        if runtime is None or actor is None or effects is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = runtime.snapshot(actor)
            if (
                snapshot.queued == 0
                and snapshot.in_flight == 0
                and not self._directory_deferred
                and self.directory_ingress_pending == 0
            ):
                break
            time.sleep(0.005)
        else:
            return False
        if not effects.close(max(0.0, deadline - time.monotonic())):
            return False
        self._directory_generation += 1
        stopped = runtime.stop(actor, timeout=max(0.0, deadline - time.monotonic()))
        if stopped:
            self._directory_runtime = None
            self._directory_actor = None
            self._directory_effects = None
        return stopped

    def _ensure_lifecycle_authority(self) -> _LifecycleAuthority:
        with self._lifecycle_lock:
            if self._lifecycle_authority is not None:
                return self._lifecycle_authority
            owner_ref = weakref.ref(self)

            def actor_event(event: ActorEvent) -> None:
                owner = owner_ref()
                if owner is not None:
                    owner._on_lifecycle_event(event)

            def handle(command: object) -> None:
                owner = owner_ref()
                if owner is not None:
                    owner._on_lifecycle_command(command)

            def execute(command: _LifecycleCommand) -> _LifecycleOutcome:
                owner = owner_ref()
                if owner is None:
                    return _LifecycleOutcome(
                        error=_LifecycleFailure(
                            "OrgFsError", "unavailable", "orgfs runtime is closed", ()
                        )
                    )
                return owner._execute_lifecycle(command)

            runtime = ActorRuntime(event_sink=actor_event)
            actor = runtime.start(
                ActorSpec(
                    "orgfs-directory-lifecycle",
                    lambda: handle,
                    mailbox_capacity=64,
                    supervision_profile="state_authority",
                )
            )

            def complete(
                completion: EffectCompleted[_LifecycleOutcome],
            ) -> AdmissionResult:
                owner = owner_ref()
                if owner is None:
                    return AdmissionResult.CLOSED
                return runtime.tell(actor, completion)

            authority = _LifecycleAuthority(
                runtime,
                actor,
                EffectLane(
                    name="orgfs-directory-lifecycle-effects",
                    execute=execute,
                    complete=complete,
                    capacity=64,
                    workers=4,
                ),
            )
            self._lifecycle_authority = authority
            return authority

    def _execute_lifecycle(self, command: _LifecycleCommand) -> _LifecycleOutcome:
        self._lifecycle_context.running = True
        try:
            operation = command.operation
            if isinstance(operation, _JoinSpace):
                value = self._join_sync(operation.space_id)
            elif isinstance(operation, _ServeSpace):
                value = self._serve_sync(operation.space_id, operation.backend)
            elif isinstance(operation, _CheckoutSpace):
                value = self._checkout_sync(operation.space_id, operation.enabled)
            elif isinstance(operation, _OpenSpaceMesh):
                value = self._ensure_mesh_sync(operation.space_id)
            else:
                raise TypeError(f"unsupported directory lifecycle command {type(operation).__name__}")
            if isinstance(value, Mapping):
                value = tuple(sorted(value.items()))
            return _LifecycleOutcome(value=value)
        except Exception as error:
            details = getattr(error, "details", {})
            frozen = (
                tuple(sorted((str(key), str(value)) for key, value in dict(details).items()))
                if isinstance(details, dict)
                else ()
            )
            return _LifecycleOutcome(
                error=_LifecycleFailure(
                    type(error).__name__,
                    str(getattr(error, "code", "internal")),
                    str(error),
                    frozen,
                )
            )
        finally:
            self._lifecycle_context.running = False

    def _on_lifecycle_command(self, command: object) -> None:
        authority = self._lifecycle_authority
        if authority is None:
            return
        if isinstance(command, EffectCompleted):
            with self._lifecycle_lock:
                current = authority.commands.get(command.operation_id)
                if current is None or current.generation != command.generation:
                    waiter = None
                    aliases = ()
                else:
                    authority.commands.pop(command.operation_id, None)
                    authority.submitted.discard(command.operation_id)
                    waiter = authority.waiters.pop(command.operation_id, None)
                    aliases = tuple(authority.mesh_aliases.pop(command.operation_id, ()))
                    if isinstance(current.operation, _OpenSpaceMesh):
                        authority.mesh_opening.pop(current.operation.space_id, None)
            if waiter is not None:
                outcome = command.result
                if isinstance(outcome, _LifecycleOutcome):
                    waiter.value = outcome.value
                    waiter.error = outcome.error
                else:
                    waiter.error = _LifecycleFailure(
                        "RuntimeError", "internal", "invalid lifecycle result", ()
                    )
                waiter.done.set()
            if aliases:
                outcome = command.result
                for alias_id in aliases:
                    with self._lifecycle_lock:
                        alias_waiter = authority.waiters.pop(alias_id, None)
                        authority.commands.pop(alias_id, None)
                    if alias_waiter is None:
                        continue
                    if isinstance(outcome, _LifecycleOutcome):
                        alias_waiter.value = outcome.value
                        alias_waiter.error = outcome.error
                    else:
                        alias_waiter.error = _LifecycleFailure(
                            "RuntimeError", "internal", "invalid lifecycle result", ()
                        )
                    alias_waiter.done.set()
            authority.effects.acknowledge(command.operation_id, command.generation)
            self._pump_lifecycle(authority)
            return
        if not isinstance(command, _LifecycleCommand):
            raise TypeError("orgfs lifecycle actor received an invalid command")
        if isinstance(command.operation, _OpenSpaceMesh):
            with self._lifecycle_lock:
                opening = authority.mesh_opening.get(command.operation.space_id)
                if opening is not None and opening != command.operation_id:
                    aliases = authority.mesh_aliases.setdefault(opening, [])
                    if command.operation_id not in aliases:
                        aliases.append(command.operation_id)
                    return
                authority.mesh_opening[command.operation.space_id] = command.operation_id
        admission = authority.effects.submit(
            EffectRequest(command.operation_id, command.generation, command)
        )
        if admission is AdmissionResult.ACCEPTED:
            authority.deferred = [
                item for item in authority.deferred
                if item.operation_id != command.operation_id
            ]
            with self._lifecycle_lock:
                authority.submitted.add(command.operation_id)
            return
        if admission is AdmissionResult.OVERLOADED:
            if all(item.operation_id != command.operation_id for item in authority.deferred):
                authority.deferred.append(command)
            return

    def _pump_lifecycle(self, authority: _LifecycleAuthority) -> None:
        if not authority.deferred:
            return
        command = authority.deferred[0]
        admission = authority.effects.submit(
            EffectRequest(command.operation_id, command.generation, command)
        )
        if admission is AdmissionResult.ACCEPTED:
            authority.deferred.pop(0)
            with self._lifecycle_lock:
                authority.submitted.add(command.operation_id)
        elif admission is AdmissionResult.CLOSED:
            authority.deferred.pop(0)
            with self._lifecycle_lock:
                authority.commands.pop(command.operation_id, None)
                waiter = authority.waiters.pop(command.operation_id, None)
            if waiter is not None:
                waiter.error = _LifecycleFailure(
                    "OrgFsError", "unavailable", "directory lifecycle effects are closed", ()
                )
                waiter.done.set()

    def _on_lifecycle_event(self, event: ActorEvent) -> None:
        if event.kind is not ActorEventKind.CHILD_RESTARTED:
            return
        authority = self._lifecycle_authority
        if authority is None:
            return
        with self._lifecycle_lock:
            if event.generation <= authority.generation:
                return
            authority.generation = event.generation
            replay = tuple(
                _LifecycleCommand(
                    call.operation_id, event.generation, call.operation
                )
                for operation_id, call in authority.commands.items()
                if operation_id not in authority.submitted
            )
            for call in replay:
                authority.commands[call.operation_id] = call
        for call in replay:
            admission = authority.runtime.tell(authority.actor, call)
            if admission is AdmissionResult.CLOSED:
                return

    def _ask_lifecycle(self, operation: _DirectoryCallOperation) -> object:
        if getattr(self._lifecycle_context, "running", False):
            if isinstance(operation, _JoinSpace):
                return self._join_sync(operation.space_id)
            if isinstance(operation, _ServeSpace):
                return self._serve_sync(operation.space_id, operation.backend)
            if isinstance(operation, _CheckoutSpace):
                return self._checkout_sync(operation.space_id, operation.enabled)
            if isinstance(operation, _OpenSpaceMesh):
                return self._ensure_mesh_sync(operation.space_id)
        authority = self._ensure_lifecycle_authority()
        operation_id = uuid.uuid4().hex
        waiter = _LifecycleWaiter(threading.Event())
        with self._lifecycle_lock:
            if self._lifecycle_closing:
                raise OrgFsError("unavailable", {"message": "directory lifecycle is closing"})
            command = _LifecycleCommand(operation_id, authority.generation, operation)
            authority.commands[operation_id] = command
            authority.waiters[operation_id] = waiter
        while True:
            admission = authority.runtime.tell(authority.actor, command)
            if admission is AdmissionResult.ACCEPTED:
                break
            with self._lifecycle_lock:
                current = authority.commands.get(operation_id)
                if admission is AdmissionResult.CLOSED and current is not None and current.generation != command.generation:
                    command = current
                    continue
                authority.commands.pop(operation_id, None)
                authority.waiters.pop(operation_id, None)
            raise OrgFsError(
                "resource-exhausted" if admission is AdmissionResult.OVERLOADED else "unavailable",
                {"message": "directory lifecycle admission was " + admission.value},
            )
        waiter.done.wait()
        if waiter.error is not None:
            error = waiter.error
            details = {key: value for key, value in error.details}
            if error.kind == "OrgFsError":
                raise OrgFsError(error.code, {**details, "message": error.message})
            raise RuntimeError(error.message)
        if isinstance(waiter.value, tuple) and all(
            isinstance(item, tuple) and len(item) == 2 for item in waiter.value
        ):
            return dict(waiter.value)
        return waiter.value

    def _close_lifecycle(self, timeout: float = 5.0) -> bool:
        with self._lifecycle_lock:
            self._lifecycle_closing = True
            authority = self._lifecycle_authority
        if authority is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = authority.runtime.snapshot(authority.actor)
            if (
                snapshot.queued == 0
                and snapshot.in_flight == 0
                and not authority.deferred
            ):
                break
            time.sleep(0.005)
        else:
            return False
        if not authority.effects.close(max(0.0, deadline - time.monotonic())):
            return False
        if not authority.runtime.stop(
            authority.actor, timeout=max(0.0, deadline - time.monotonic())
        ):
            return False
        with self._lifecycle_lock:
            self._lifecycle_authority = None
        return True

    def bind_transport(
        self,
        session: TransportSession,
        *,
        supplier_online: Callable[[str], bool] | None = None,
        holder_candidates: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        with self._resource_lock:
            if self._closing:
                raise OrgFsError("unavailable", {"message": "orgfs runtime is closing"})
            self._session = session
        if supplier_online is not None:
            self._supplier_online = supplier_online
        if holder_candidates is not None:
            self._holder_candidates = holder_candidates
        self._start_directory_ingress()
        self._announce_registration = session.subscribe(
            f"{KeySpace().prefix}/org/fs/announce/*",
            lambda sample: self._admit_directory_sample("announce", sample),
        )
        observe_liveliness = getattr(session, "observe_liveliness", None)
        if callable(observe_liveliness):
            self._liveliness_registration = observe_liveliness(
                f"{KeySpace().prefix}/liveliness/actor/*",
                lambda sample: self._admit_directory_sample("liveliness", sample),
                history=True,
            )
        for space_id in self.stores.snapshot_ids():
            self._mesh(space_id)

    def _on_peer_liveliness(self, sample: TransportSample) -> None:
        self._process_peer_liveliness(sample)

    def _settle_peer_liveliness(
        self, sample: TransportSample
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Track host presence and settle holder hints on liveliness delete."""
        peer = self._host_peer_from_liveliness(sample)
        if not peer or peer == self.node_id:
            return None, None
        with self._pending_announces_lock:
            if sample.kind == "delete":
                self._lively_peers.discard(peer)
                for holders in self._holders.values():
                    holders.discard(peer)
                return peer, None
            self._lively_peers.add(peer)
            return peer, self._pending_announces.pop(peer, None)

    @staticmethod
    def _host_peer_from_liveliness(sample: TransportSample) -> str | None:
        prefix = f"{KeySpace().prefix}/liveliness/actor/"
        if not sample.key.startswith(prefix):
            return None
        peer = KeySpace().decode_identity(sample.key[len(prefix) :])
        # Import here to avoid making the orgfs module initialize the daemon
        # package while DaemonApplication is importing OrgFsRuntime.
        from hyprial.daemon.identity import classify_target_identity
        from hyprial.uri import TARGET_KIND_HOST

        if classify_target_identity(peer) != TARGET_KIND_HOST:
            return None
        return peer
    def _process_peer_liveliness(self, sample: TransportSample) -> None:
        """F5/F6: admit buffered discovery and schedule bounded anti-entropy."""

        peer, pending = self._settle_peer_liveliness(sample)
        if peer and sample.kind != "delete":
            self._announce_all()
            if pending is not None:
                self._apply_announce(peer, pending)
            # An announce can race the first pop while the liveliness callback
            # is applying the previous buffered value.  Recheck once after the
            # presence verdict has flipped so that value cannot be stranded
            # until a later liveliness transition.
            with self._pending_announces_lock:
                landed_during_flip = self._pending_announces.pop(peer, None)
            if landed_during_flip is not None:
                self._apply_announce(peer, landed_during_flip)
            with self._checkout_lock:
                checkouts = tuple(self._checkouts.values())
            for checkout in checkouts:
                self._request_checkout_reconcile(checkout)
            for space_id in self.stores.snapshot_ids():
                # F6: only schedule anti-entropy for spaces the peer actually
                # announced when holder hints exist.  A freshly restarted
                # node has no in-memory hints yet, so it retains the P1
                # liveliness fallback until the first announce arrives.
                with self._pending_announces_lock:
                    holders = frozenset(self._holders.get(space_id, ()))
                if holders and peer not in holders:
                    continue
                mesh = self._mesh(space_id)
                if mesh is not None:
                    mesh.schedule_sync_from(peer)

    def _announcement_spaces(self) -> tuple[dict[str, object], ...]:
        spaces: list[dict[str, object]] = []
        for info in sorted(self.facade.spaces(), key=lambda item: item.space_id):
            mode = next(
                (
                    member.mode
                    for member in self.facade.members(info.space_id)
                    if member.user == self.author
                ),
                None,
            )
            if mode is None:
                continue
            roles = ["member"]
            with self._resource_lock:
                resident = info.space_id in self._replicas
            if resident:
                roles.append("resident")
            spaces.append({"spaceId": info.space_id, "roles": roles, "mode": mode})
        return tuple(spaces)

    def _announce_all(self) -> None:
        with self._resource_lock:
            if self._closing or self._session is None:
                return
            mesh = next(iter(self._meshes.values()), None)
        if mesh is None:
            return
        mesh.announce()

    def _on_announce(self, sample: TransportSample) -> None:
        self._process_announce(sample)

    def _process_announce(self, sample: TransportSample) -> None:
        try:
            value = json.loads(sample.payload)
            prefix = f"{KeySpace().prefix}/org/fs/announce/"
            if not sample.key.startswith(prefix):
                return
            key_node = sample.key[len(prefix) :]
            if (
                not isinstance(value, dict)
                or value.get("schemaVersion") != 1
                or value.get("type") != "orgfs-announce"
                or value.get("node") != key_node
                or not key_node
                or "/" in key_node
                or not isinstance(value.get("spaces"), list)
            ):
                return
            with self._pending_announces_lock:
                observed_live = key_node in self._lively_peers
            if not observed_live and not self._supplier_online(key_node):
                self._buffer_announce(key_node, value)
                return
            with self._pending_announces_lock:
                self._pending_announces.pop(key_node, None)
            self._apply_announce(key_node, value)
        except Exception:
            return

    def _buffer_announce(
        self,
        node: str,
        value: dict[str, Any],
        *,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> None:
        with self._pending_announces_lock:
            spaces = value["spaces"]
            previous = self._pending_announces.get(node)
            if (
                previous is None
                and len(self._pending_announces) >= ORGFS_ANNOUNCE_BUFFER_LIMIT
            ):
                evicted = next(iter(self._pending_announces))
                self._pending_announces.pop(evicted, None)
                self._record_announce_buffer_drop(
                    reason="announcer-limit",
                    node=node,
                    dropped=1,
                    diagnostics=diagnostics,
                )
            if not spaces or previous is None or not previous["spaces"]:
                retained = list(spaces[:ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT])
                dropped = len(spaces) - len(retained)
                self._pending_announces[node] = {**value, "spaces": retained}
                if dropped:
                    self._record_announce_buffer_drop(
                        reason="space-limit",
                        node=node,
                        dropped=dropped,
                        space_count=len(retained),
                        diagnostics=diagnostics,
                    )
                return
            by_space = {
                str(space["spaceId"]): space
                for space in previous["spaces"]
                if isinstance(space, dict) and isinstance(space.get("spaceId"), str)
            }
            by_space.update(
                {
                    str(space["spaceId"]): space
                    for space in spaces
                    if isinstance(space, dict) and isinstance(space.get("spaceId"), str)
                }
            )
            merged = list(by_space.values())
            retained = merged[:ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT]
            self._pending_announces[node] = {**value, "spaces": retained}
            dropped = len(merged) - len(retained)
            if dropped:
                self._record_announce_buffer_drop(
                    reason="space-limit",
                    node=node,
                    dropped=dropped,
                    space_count=len(retained),
                    diagnostics=diagnostics,
                )

    def _record_announce_buffer_drop(
        self,
        *,
        reason: str,
        node: str,
        dropped: int,
        space_count: int = 0,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> dict[str, object] | None:
        self._announce_buffer_dropped += dropped
        bucket = self._announce_buffer_dropped.bit_length()
        if bucket == self._announce_buffer_log_bucket:
            return None
        self._announce_buffer_log_bucket = bucket
        fields: dict[str, object] = {
            "reason": reason,
            "node": node,
            "droppedCount": self._announce_buffer_dropped,
            "announcerCount": len(self._pending_announces),
            "announcerLimit": ORGFS_ANNOUNCE_BUFFER_LIMIT,
            "spaceCount": space_count,
            "spacesPerEntryLimit": ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT,
        }
        if diagnostics is not None:
            diagnostics.append(fields)
        elif self.logger is not None:
            self.logger("warn", "orgfs.announce.buffer-dropped", **fields)
        return fields

    def _apply_announce(self, node: str, value: dict[str, Any]) -> None:
        spaces = value["spaces"]
        with self._pending_announces_lock:
            if not spaces:
                for holders in self._holders.values():
                    holders.discard(node)
                return
            for space in spaces:
                if isinstance(space, dict) and isinstance(space.get("spaceId"), str):
                    space_id = str(space["spaceId"])
                    self._holders.setdefault(space_id, set()).add(node)
                    self._known_holders.setdefault(space_id, set()).add(node)

    def _holder_snapshot(self, space_id: str) -> tuple[str, ...]:
        with self._pending_announces_lock:
            return tuple(sorted(self._holders.get(space_id, ())))

    def _known_holder_snapshot(self, space_id: str) -> tuple[str, ...]:
        with self._pending_announces_lock:
            return tuple(sorted(self._known_holders.get(space_id, ())))

    def _ensure_mesh_sync(self, space_id: str) -> _SpaceResourceHandle | None:
        with self._resource_lock:
            existing = self._meshes.get(space_id)
            if existing is not None:
                return _SpaceResourceHandle(space_id, self._directory_generation)
            creation = self._mesh_creations.get(space_id)
            if creation is None:
                creation = _MeshCreation()
                self._mesh_creations[space_id] = creation
                creator = True
            else:
                creator = False
        if not creator:
            creation.done.wait()
            if creation.error is not None:
                raise creation.error
            return creation.handle
        try:
            handle = self._create_mesh_sync(space_id)
        except BaseException as error:
            with self._resource_lock:
                creation.error = error
                self._mesh_creations.pop(space_id, None)
                creation.done.set()
            raise
        with self._resource_lock:
            creation.handle = handle
            self._mesh_creations.pop(space_id, None)
            creation.done.set()
        return handle

    def _create_mesh_sync(self, space_id: str) -> _SpaceResourceHandle | None:
        with self._resource_lock:
            session = self._session
            if self._closing or session is None:
                return None
            existing = self._meshes.get(space_id)
        if existing is not None:
            return _SpaceResourceHandle(space_id, self._directory_generation)

        def applied(envelope: bytes) -> tuple[str, ...]:
            self.facade.apply_envelope(space_id, envelope)
            self._reconcile_retirements(space_id)
            return self.facade.retained_blob_digests(space_id)

        def replacement(
            old_doc_id: str, new_doc_id: str, snapshot_bytes: bytes
        ) -> None:
            if space_id in self.facade._spaces:  # noqa: SLF001
                self.facade.install_replacement_snapshot(
                    space_id, old_doc_id, new_doc_id, snapshot_bytes
                )
                self._reconcile_retirements(space_id)

        mesh = OrgFsMesh(
            session,
            self.stores.get(space_id),
            self.node_id,
            author=self.author,
            blob_store=self.blobs,
            supplier_online=self._supplier_online,
            announcement_source=self._announcement_spaces,
            holder_discovery=lambda: self._holder_snapshot(space_id),
            recovery_candidates=lambda: (
                *self._holder_snapshot(space_id), *self._holder_candidates()
            ),
            on_applied=applied,
            on_replacement=replacement,
            logger=self.logger,
            space_authority=self.facade.space_authority(
                space_id, self.stores.get(space_id)
            ),
        )
        with self._resource_lock:
            if self._closing or self._session is not session:
                current = None
                discard = True
            else:
                current = self._meshes.get(space_id)
                if current is None:
                    current = mesh
                    self._meshes[space_id] = mesh
                discard = current is not mesh
            replica = self._replicas.get(space_id)
        if discard:
            mesh.close()
            return (
                _SpaceResourceHandle(space_id, self._directory_generation)
                if current is not None
                else None
            )
        if replica is not None:
            mesh.attach_replica(replica)
        self._announce_all()
        return _SpaceResourceHandle(space_id, self._directory_generation)

    def _mesh(self, space_id: str) -> OrgFsMesh | None:
        with self._resource_lock:
            if self._closing or self._session is None:
                return None
            existing = self._meshes.get(space_id)
        if existing is not None:
            return existing
        try:
            handle = self._ask_lifecycle(_OpenSpaceMesh(space_id))
        except OrgFsError as error:
            if error.code == "unavailable":
                return None
            raise
        if not isinstance(handle, _SpaceResourceHandle):
            return None
        with self._resource_lock:
            return self._meshes.get(handle.space_id)

    def _reconcile_retirements(self, space_id: str) -> None:
        """Finish G2 cleanup and ack after all replacement bytes are local."""

        store = self.stores.get(space_id)
        authority = self.facade.space_authority(space_id, store)
        records = tuple(authority.read(_ReadStore("retirement_records")))
        if authority.read(_ReadStore("pending_replacements")):
            return
        with self._resource_lock:
            replica = self._replicas.get(space_id)
        purge_entries = dict(authority.read(_ReadStore("purge_list_entries")))
        retirement_plan_ids = {record.plan_id for record in records}
        for record in records:
            snapshot = authority.read(
                _ReadStore("snapshot", doc_id=record.replacement_doc_id)
            )
            if replica is not None:
                plan_blobs = tuple(
                    {"sha": digest}
                    for digest, plan_id in sorted(purge_entries.items())
                    if plan_id == record.plan_id
                )
                replica.apply_retirement(
                    {
                        "oldPhysicalDocId": record.old_physical_doc_id,
                        "replacementDocId": record.replacement_doc_id,
                        "snapshotId": record.snapshot_id,
                        "replacementSnapshotBytes": snapshot.snapshot_bytes,
                    },
                    {
                        # R4: this replicated purge-list inventory is owner-authored
                        # and survives replica log retention.  Reconstructing the
                        # delete set from remaining rows loses trimmed updateBlob
                        # digests and can acknowledge with forbidden residue.
                        "blobs": plan_blobs,
                    },
                )
            authority.delete_retired_objects(record.old_physical_doc_id)
            if authority.read(
                _ReadStore("retired_residue", doc_id=record.old_physical_doc_id)
            ):
                raise StoreError("invalid-argument", "retired document residue remains")

        if replica is not None:
            replica.apply_blob_purge(
                digest
                for digest, plan_id in purge_entries.items()
                if plan_id not in retirement_plan_ids
            )
            for doc_id in authority.read(_ReadStore("document_ids")):
                if authority.read(_ReadStore("snapshot_point", doc_id=doc_id)) is not None:
                    replica.apply_retention(doc_id)
        for digest in purge_entries:
            self.blobs.release(space_id, digest)
            self.blobs.delete_if_unreferenced(digest)

        plans = {record.plan_id for record in records} | set(purge_entries.values())
        for plan_id in sorted(plans):
            if any(
                record.plan_id == plan_id
                and authority.read(
                    _ReadStore("retired_residue", doc_id=record.old_physical_doc_id)
                )
                for record in records
            ):
                continue
            self.facade.acknowledge_purge(space_id, plan_id)

    def broadcast_pending(self, space_id: str) -> int:
        mesh = self._mesh(space_id)
        return 0 if mesh is None else mesh.broadcast_pending()

    def broadcast_records(
        self, space_id: str, records: tuple[CommitRecord, ...]
    ) -> int:
        mesh = self._mesh(space_id)
        if mesh is None:
            return 0
        if "broadcast_pending" in vars(mesh):
            return mesh.broadcast_pending(space_id)
        return mesh.broadcast_records(records)

    def reconcile_replica_blobs(self, space_id: str) -> tuple[str, ...]:
        """Reconcile retained local blob refs through the per-space authority."""

        with self._resource_lock:
            replica = self._replicas.get(space_id)
        if replica is None:
            return ()
        reconciled = replica.reconcile_blobs()
        for digest in reconciled:
            self.blobs.pin(space_id, digest, "replica")
        return reconciled

    def status(self, space_id: str) -> SpaceStatus:
        """Live holder view: durable probe history plus current presence.

        ``holders_online`` is the sorted, de-duplicated union of this node
        when it currently serves the space and the announced holders that
        are online now per the runtime's liveliness and presence view.
        ``durable_holders_seen`` keeps its probe-recorded meaning.
        """

        base = self.facade.status(space_id)
        with self._pending_announces_lock:
            announced = set(self._holders.get(space_id, ()))
            lively = set(self._lively_peers)
        # When this runtime observes host liveliness, presence is exactly that
        # view: an announce applied after a peer's liveliness delete must not
        # make it look online again.  The supplier predicate is only the
        # fallback for sessions without a liveliness view.
        observes_liveliness = self._liveliness_registration is not None
        online = {
            node
            for node in announced
            if node != self.node_id
            and (
                node in lively
                if observes_liveliness
                else self._supplier_online(node)
            )
        }
        with self._resource_lock:
            serving_locally = space_id in self._replicas
        if serving_locally:
            online.add(self.node_id)
        return SpaceStatus(
            base.space_id,
            base.unconfirmed_commits,
            base.durable_holders_seen,
            tuple(sorted(online)),
        )

    def writer_attributions(self, space_id: str) -> dict[str, tuple[str, ...]]:
        """Return the space's replicated node -> author attribution table."""

        if space_id not in {info.space_id for info in self.facade.spaces()}:
            raise OrgFsError("unknown-space", {"spaceId": space_id})
        store = self.stores.get(space_id)
        authority = self.facade.space_authority(space_id, store)
        if authority is None:
            raise OrgFsError("unavailable", {"spaceId": space_id})
        return dict(authority.read(_ReadStore("writer_attributions")))

    def refresh_space(self, space_id: str, *, timeout: float) -> bool:
        """Try every online holder once without exceeding the caller's bound."""

        if self._session is None:
            return False
        holders = tuple(
            node
            for node in self._holder_snapshot(space_id)
            if node != self.node_id and self._supplier_online(node)
        )
        if not holders:
            return True
        mesh = self._mesh(space_id)
        if mesh is None:
            return False
        deadline = time.monotonic() + timeout
        refreshed = True
        for holder in holders:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                mesh.sync_from(
                    holder,
                    timeout=remaining,
                    deadline_monotonic=deadline,
                )
            except (StoreError, OrgFsError):
                refreshed = False
            if time.monotonic() > deadline:
                refreshed = False
                break
        return refreshed

    def known_holders(
        self,
        space_id: str,
        *,
        doc_id: str | None = None,
        required_frontier: bytes | None = None,
    ) -> tuple[str, ...]:
        """Return in-memory holder hints, preferring proven doc coverage."""

        online = {
            node
            for node in self._holder_snapshot(space_id)
            if node != self.node_id and self._supplier_online(node)
        }
        known = {
            node
            for node in self._known_holder_snapshot(space_id)
            if node != self.node_id
        }
        candidates = online | known
        store = self.stores.get(space_id)
        authority = self.facade.space_authority(space_id, store)
        assert authority is not None
        frontiers = {
            holder: dict(docs)
            for holder, docs in authority.read(_ReadStore("holder_frontiers"))
        }

        def key(node: str) -> tuple[bool, bool, str]:
            covers = False
            if doc_id is not None and required_frontier is not None:
                actual = frontiers.get(node, {}).get(doc_id)
                covers = actual is not None and state_covers(
                    actual, required_frontier
                )
            return (not covers, node not in online, node)

        return tuple(sorted(candidates, key=key))

    def await_content(
        self, space_id: str, node_id: str, *, deadline_monotonic: float
    ) -> bool:
        """Actively provision one node's content without holding the facade lock."""

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return False
        info = self.facade.stat(space_id, f"id:{node_id}")
        if info.kind == "blob":
            if info.content_state == "arrived":
                return True
            if not info.blob_hash:
                return False
            mesh = self._mesh(space_id)
            if mesh is None:
                return False
            # request_blob's event only means "this attempt ended"; a holder
            # that is not routable yet makes an attempt end without the blob.
            # Keep provisioning until the caller's deadline, with a short
            # backoff so an already-set event cannot spin.
            while True:
                event = mesh.request_blob(info.blob_hash)
                event.wait(max(0.0, deadline_monotonic - time.monotonic()))
                if self.blobs.contains(info.blob_hash):
                    return True
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(ORGFS_AWAIT_RETRY_SECONDS, remaining))
        if info.kind == "doc":
            if info.content_state in {"arrived", "unverifiable"}:
                return True
            self.refresh_space(
                space_id,
                timeout=max(0.0, deadline_monotonic - time.monotonic()),
            )
            refreshed = self.facade.stat(space_id, f"id:{node_id}")
            if refreshed.doc_id:
                store = self.stores.get(space_id)
                authority = self.facade.space_authority(space_id, store)
                assert authority is not None
                snapshot = authority.read(
                    _ReadStore("snapshot", doc_id=refreshed.doc_id)
                )
                self.facade.hydrate_content_snapshot(
                    space_id, f"id:{node_id}", snapshot.snapshot_bytes,
                    expected_doc_id=snapshot.doc_id,
                )
            return (
                self.facade.stat(space_id, f"id:{node_id}").content_state
                == "arrived"
            )
        return True

    def fetch_blob(self, space_id: str, digest: str) -> bytes:
        """Fetch a facade-referenced blob from any currently online holder."""

        candidates = sorted(
            {
                *(
                    node
                    for node in self._holder_snapshot(space_id)
                    if node != self.node_id
                ),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        mesh = self._mesh(space_id)
        last_error: Exception | None = None
        if mesh is not None:
            for holder in candidates:
                if not self._supplier_online(holder):
                    continue
                try:
                    payload = mesh.fetch_blob(holder, digest)
                    stored = self.blobs.put(space_id, payload, reason="replica")
                    if stored != digest:
                        raise StoreError(
                            "blob-unavailable",
                            "fetched blob did not match requested digest",
                        )
                    with self._resource_lock:
                        replica = self._replicas.get(space_id)
                    if replica is not None:
                        replica.store_blob(digest, payload)
                        self.blobs.pin(space_id, digest, "replica")
                    return payload
                except StoreError as exc:
                    last_error = exc
        if self.logger is not None:
            self.logger(
                "warn",
                "orgfs.content.blob-fetch-failed",
                reason=getattr(last_error, "code", "no-holder-online"),
                spaceId=space_id,
                digest=digest,
                candidates=candidates,
                detail=str(last_error) if last_error is not None else "",
            )
        details: dict[str, object] = {
            "digest": digest,
            "lastKnownHolders": list(self._known_holder_snapshot(space_id)),
        }
        if last_error is not None:
            details["message"] = str(last_error)
        raise OrgFsError("blob-unavailable", details)

    def join(self, space_id: str) -> SpaceInfo:
        return self._ask_lifecycle(_JoinSpace(space_id))  # type: ignore[return-value]

    def _join_sync(self, space_id: str) -> SpaceInfo:
        holders = sorted(
            {
                *(
                    node
                    for node in self._holder_snapshot(space_id)
                    if node != self.node_id
                ),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        if not holders:
            raise OrgFsError("no-holder-online", {"spaceId": space_id})
        mesh = self._mesh(space_id)
        assert mesh is not None
        last_error: Exception | None = None
        for holder in holders:
            try:
                mesh.sync_from(holder)
                info = self.facade.load_space(space_id)
                self._reconcile_retirements(space_id)
                self._announce_all()
                return info
            except (StoreError, OrgFsError) as exc:
                last_error = exc
        if last_error is not None:
            code = getattr(last_error, "code", "no-holder-online")
            raise OrgFsError(str(code), {"message": str(last_error)}) from last_error
        raise OrgFsError("no-holder-online", {"spaceId": space_id})

    def watch_events(
        self, space_id: str, glob: str, since_version: str | None
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "spaceId": event.space_id,
                "kind": event.kind,
                "node": event.node,
                "oldPath": event.old_path,
            }
            for event in self.facade.changes_since(space_id, glob, since_version)
        )

    def serve(self, space_id: str, backend: str = "fs") -> dict[str, object]:
        return self._ask_lifecycle(_ServeSpace(space_id, backend))  # type: ignore[return-value]

    def _serve_sync(self, space_id: str, backend: str = "fs") -> dict[str, object]:
        if backend not in {"fs", "memory"}:
            raise OrgFsError(
                "invalid-argument", {"message": "backend must be fs or memory"}
            )
        try:
            self.facade.stat(space_id, "id:root")
        except OrgFsError as exc:
            if exc.code != "unknown-space":
                raise
            self.join(space_id)
        local_store = self.stores.get(space_id)
        with self._resource_lock:
            creation_lock = self._replica_creation_locks.setdefault(space_id, Lock())
        with creation_lock:
            return self._create_or_reuse_replica(space_id, backend, local_store)

    def _create_or_reuse_replica(
        self, space_id: str, backend: str, local_store: LocalSpaceStore
    ) -> dict[str, object]:
        """Own one space's replica construction before touching its backend."""

        with self._resource_lock:
            if self._closing:
                raise OrgFsError("unavailable", {"message": "orgfs runtime is closing"})
            existing = self._replicas.get(space_id)
        if existing is not None:
            existing_backend = "fs" if existing.backend_kind == "FsReplicaBackend" else "memory"
            if existing_backend != backend:
                raise OrgFsError(
                    "invalid-argument",
                    {
                        "message": "space is already served by another backend",
                        "spaceId": space_id,
                        "backend": existing_backend,
                    },
                )
            existing.reconcile_blobs()
            for digest in existing.pinned_blobs():
                self.blobs.pin(space_id, digest, "replica")
            self._reconcile_retirements(space_id)
            return {
                "spaceId": space_id,
                "backend": backend,
                "durable": existing.durable(),
                "pinnedBlobs": list(existing.pinned_blobs()),
            }
        replica_backend = (
            FsReplicaBackend(space_id, self.state_dir)
            if backend == "fs"
            else MemoryReplicaBackend(space_id)
        )

        def conflict_event(event: str, details: Any) -> None:
            if self.logger is not None:
                self.logger("error", f"orgfs.{event}", **dict(details))

        def owner_notice(event: str, details: Any) -> None:
            owner = next(
                info.owner for info in self.facade.spaces() if info.space_id == space_id
            )
            if self.logger is not None:
                self.logger(
                    "warn",
                    "orgfs.owner-notice",
                    notice=event,
                    owner=owner,
                    **dict(details),
                )
            if self.owner_notifier is not None:
                self.owner_notifier(owner, event, dict(details))

        replica_core = ReplicaStore(
            space_id, replica_backend, local_store,
            event_callback=conflict_event, owner_notifier=owner_notice,
        )
        replica = ReplicaAuthority(replica_core, local_store, self.blobs)
        replica.prime_from(local_store, self.blobs)
        for digest in replica.pinned_blobs():
            self.blobs.pin(space_id, digest, "replica")
        with self._resource_lock:
            if self._closing:
                replica.close(timeout=5.0)
                raise OrgFsError("unavailable", {"message": "orgfs runtime is closing"})
            self._replicas[space_id] = replica
        mesh = self._mesh(space_id)
        if mesh is not None:
            mesh.attach_replica(replica)
        self._reconcile_retirements(space_id)
        return {
            "spaceId": space_id,
            "backend": backend,
            "durable": replica.durable(),
            "pinnedBlobs": list(replica.pinned_blobs()),
        }

    def purge_plan(self, space_id: str, targets: list[dict[str, Any]]) -> Any:
        return self.facade.purge_plan(space_id, targets)

    def purge(self, space_id: str, plan_id: str) -> Any:
        result = self.facade.purge(space_id, plan_id)
        self._reconcile_retirements(space_id)
        return result

    def purge_status(self, space_id: str, plan_id: str) -> Any:
        return self.facade.purge_status(space_id, plan_id)

    def unban(self, space_id: str, sha: str) -> None:
        self.facade.unban(space_id, sha)

    def _write_checkout_config(self) -> None:
        with self._checkout_config_lock:
            with self._checkout_lock:
                spaces = sorted(self._checkouts)
            self._checkout_config.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._checkout_config.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"schemaVersion": 1, "spaces": spaces},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._checkout_config)

    def _restore_checkouts(self) -> None:
        try:
            value = json.loads(self._checkout_config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            if self.logger is not None:
                self.logger("warn", "orgfs.checkout.restore-failed", detail=str(exc))
            return
        spaces = value.get("spaces") if isinstance(value, dict) else None
        if not isinstance(spaces, list):
            return
        for space_id in spaces:
            if not isinstance(space_id, str) or space_id not in self.stores:
                continue
            try:
                self._set_checkout(space_id, True, persist=False)
            except Exception as exc:
                if self.logger is not None:
                    self.logger(
                        "warn",
                        "orgfs.checkout.restore-failed",
                        spaceId=space_id,
                        detail=str(exc),
                    )

    def _set_checkout(
        self, space_id: str, enabled: bool, *, persist: bool
    ) -> dict[str, object]:
        root = self.state_dir / "orgfs" / "spaces" / space_id / "checkout"
        with self._checkout_lock:
            if self._checkout_closing:
                raise OrgFsError("unavailable", {"message": "checkouts are closing"})
            authority = self._checkouts.get(space_id)
            if authority is None:
                authority = CheckoutAuthority(self.facade, self.blobs, space_id, root)
                if enabled:
                    self._checkouts[space_id] = authority
        if enabled:
            try:
                authority.enable()
            except Exception:
                with self._checkout_lock:
                    if self._checkouts.get(space_id) is authority:
                        self._checkouts.pop(space_id, None)
                authority.close(timeout=5.0)
                raise
        else:
            try:
                authority.disable()
            finally:
                with self._checkout_lock:
                    if self._checkouts.get(space_id) is authority:
                        self._checkouts.pop(space_id, None)
                authority.close(timeout=5.0)
        if persist:
            self._write_checkout_config()
        return {"spaceId": space_id, "enabled": enabled, "path": str(root)}

    def checkout(self, space_id: str, enabled: bool) -> dict[str, object]:
        return self._ask_lifecycle(_CheckoutSpace(space_id, bool(enabled)))  # type: ignore[return-value]

    def _checkout_sync(self, space_id: str, enabled: bool) -> dict[str, object]:
        return self._set_checkout(space_id, bool(enabled), persist=True)

    def close(self) -> None:
        with self._close_lock:
            with self._resource_lock:
                if self._closed:
                    return
                # Admission is fenced immediately, but remains retryable until
                # every accepted operation and owned resource has drained.
                self._closing = True
            self._close_impl()

    def _close_impl(self) -> None:
        with self._resource_lock:
            self._closing = True
            creations = tuple(self._mesh_creations.values())
        deadline = time.monotonic() + 5.0
        for creation in creations:
            if not creation.done.wait(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("orgfs mesh creation did not drain")
        if not self._close_lifecycle(timeout=5.0):
            raise TimeoutError("orgfs directory lifecycle did not drain")
        with self._checkout_lock:
            self._checkout_closing = True
            checkouts = tuple(self._checkouts.items())
        for _space_id, checkout in checkouts:
            if isinstance(checkout, CheckoutAuthority) and not checkout.close(timeout=5.0):
                raise TimeoutError("orgfs checkout I/O did not drain")
        with self._checkout_lock:
            self._checkouts.clear()
        if self._session is not None:
            try:
                self._session.put(
                    KeySpace().orgfs_announce(self.node_id),
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "type": "orgfs-announce",
                            "node": self.node_id,
                            "durable": True,
                            "spaces": [],
                        },
                        separators=(",", ":"),
                    ).encode(),
                )
            except Exception:
                pass
        if self._announce_registration is not None:
            self._announce_registration.close()
            self._announce_registration = None
        if self._liveliness_registration is not None:
            self._liveliness_registration.close()
            self._liveliness_registration = None
        if not self._close_directory_ingress(timeout=5.0):
            raise TimeoutError("orgfs directory effects did not drain")
        with self._resource_lock:
            meshes = tuple(self._meshes.values())
            replicas = tuple(self._replicas.values())
        for mesh in reversed(meshes):
            mesh.close()
        for replica in replicas:
            if not replica.close(timeout=5.0):
                raise TimeoutError("orgfs replica authority did not drain")
        if not self.facade.close_effects(timeout=5.0):
            raise TimeoutError("orgfs post-commit effects did not drain")
        with self._resource_lock:
            self._meshes.clear()
            self._replicas.clear()
        with self._pending_announces_lock:
            self._pending_announces.clear()
            self._lively_peers.clear()
        with self._resource_lock:
            self._session = None
        if not self.blobs.close(timeout=5.0):
            raise TimeoutError("orgfs blob authority did not drain")
        for store in self.stores.snapshot():
            store.close()
        self.stores.clear()
        with self._resource_lock:
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = ["OrgFsRuntime"]
