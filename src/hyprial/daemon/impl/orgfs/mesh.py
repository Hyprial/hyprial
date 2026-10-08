from __future__ import annotations


from collections.abc import Callable, Iterable, Mapping

from concurrent.futures import Future, ThreadPoolExecutor, wait




from threading import Event, Lock

import time

from typing import TYPE_CHECKING, Any


import weakref

from hyprial.kernel import ActorHandle, ActorRuntime, ActorSpec, AdmissionResult

from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority

from hyprial.daemon.impl.transport import (
    KeySpace,
    Registration,
    TransportSession,
    ZenohTransport)


from hyprial.daemon.impl.orgfs.storage.blobs  import (
    BlobStore)

from hyprial.daemon.impl.orgfs.storage.store  import (
    ORGFS_ENVELOPE_BYTES,
    ORGFS_PAGE_BYTES,
    LocalSpaceStore)


if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.storage.replica  import ReplicaStore

from hyprial.daemon.impl.orgfs.network.recovery import MeshRecovery
from hyprial.daemon.impl.orgfs.network.ingress import MeshIngress
from hyprial.daemon.impl.orgfs.network.queries import MeshQueries
from hyprial.daemon.impl.orgfs.network.sync import MeshSync
from hyprial.daemon.impl.orgfs.network.protocol import AnnouncementSource as AnnouncementSource
from hyprial.daemon.impl.orgfs.network.protocol import AppliedHook as AppliedHook
from hyprial.daemon.impl.orgfs.network.protocol import MeshLogger as MeshLogger
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_ANNOUNCE_BUFFER_LIMIT as ORGFS_ANNOUNCE_BUFFER_LIMIT
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT as ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_BLOB_FETCH_QUEUE_LIMIT as ORGFS_BLOB_FETCH_QUEUE_LIMIT
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_BLOB_FETCH_TIMEOUT_SECONDS as ORGFS_BLOB_FETCH_TIMEOUT_SECONDS
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_LOG_INGRESS_CAPACITY as ORGFS_LOG_INGRESS_CAPACITY
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_LOG_RANGE_LIMIT as ORGFS_LOG_RANGE_LIMIT
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET as ORGFS_PROJECTION_FAILURE_ATTEMPT_BUDGET
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_REPLICA_SYNC_SCAN_ROWS as ORGFS_REPLICA_SYNC_SCAN_ROWS
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS as ORGFS_SCHEDULED_SYNC_ATTEMPT_SECONDS
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS as ORGFS_SCHEDULED_SYNC_BUDGET_SECONDS
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_SYNC_QUEUE_LIMIT as ORGFS_SYNC_QUEUE_LIMIT
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_SYNC_RETRY_BACKOFF_SECONDS as ORGFS_SYNC_RETRY_BACKOFF_SECONDS
from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_WIRE_VERSION as ORGFS_WIRE_VERSION
from hyprial.daemon.impl.orgfs.network.protocol import RecoveryCandidates as RecoveryCandidates
from hyprial.daemon.impl.orgfs.network.protocol import ReplacementHook as ReplacementHook
from hyprial.daemon.impl.orgfs.network.protocol import SupplierGate as SupplierGate
from hyprial.daemon.impl.orgfs.network.protocol import _BLOB_ABSENCE_CODES as _BLOB_ABSENCE_CODES
from hyprial.daemon.impl.orgfs.network.protocol import _LogIngress as _LogIngress
from hyprial.daemon.impl.orgfs.network.protocol import _READ_DIRECT as _READ_DIRECT
from hyprial.daemon.impl.orgfs.network.protocol import _b64 as _b64
from hyprial.daemon.impl.orgfs.network.protocol import _decode_request as _decode_request
from hyprial.daemon.impl.orgfs.network.protocol import _decode_update_header as _decode_update_header
from hyprial.daemon.impl.orgfs.network.protocol import _elapsed_ms as _elapsed_ms
from hyprial.daemon.impl.orgfs.network.protocol import _error as _error
from hyprial.daemon.impl.orgfs.network.protocol import _json_bytes as _json_bytes
from hyprial.daemon.impl.orgfs.network.protocol import _reply_origin as _reply_origin
from hyprial.daemon.impl.orgfs.network.protocol import _unb64 as _unb64


class OrgFsMesh(MeshRecovery, MeshIngress, MeshQueries, MeshSync):
    """Broadcast, query, import, and probe one local orgfs space."""


    def __init__(
        self,
        session: TransportSession,
        store: LocalSpaceStore,
        node_id: str,
        *,
        author: str,
        blob_store: BlobStore | None = None,
        supplier_online: SupplierGate | None = None,
        on_applied: AppliedHook | None = None,
        on_replacement: ReplacementHook | None = None,
        announcement_source: AnnouncementSource | None = None,
        holder_discovery: set[str] | Callable[[], Iterable[str]] | None = None,
        recovery_candidates: RecoveryCandidates | None = None,
        durable: bool = True,
        logger: MeshLogger | None = None,
        keys: KeySpace | None = None,
        replica_store: ReplicaStore | None = None,
        space_authority: Any | None = None,
        projection_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not node_id:
            raise ValueError("node_id must not be empty")
        self.session = session
        self.store = store
        self.node_id = node_id
        self.author = author
        self.blob_store = blob_store
        self.durable = bool(durable)
        self._supplier_online = supplier_online or (lambda _supplier: True)
        self._on_applied = on_applied
        self._on_replacement = on_replacement
        self._announcement_source = announcement_source
        self._holder_discovery = (
            holder_discovery if holder_discovery is not None else set()
        )
        self._recovery_candidates = recovery_candidates or (
            lambda: tuple(self._announced_nodes)
        )
        self._logger = logger
        self._keys = keys or KeySpace()
        self.replica_store = replica_store
        self.space_authority = space_authority
        self._closed = False
        self._worker_lock = Lock()
        self._blob_fetch_pending: dict[str, list[tuple[bytes, str]]] = {}
        self._blob_fetch_waiters: dict[str, list[Event]] = {}
        self._blob_fetch_pending_count = 0
        self._blob_fetch_dropped = 0
        self._replica_blob_pending: dict[str, str] = {}
        self._replica_blob_dropped = 0
        self._sync_pending: set[str] = set()
        self._sync_active: set[str] = set()
        self._sync_rerun: set[str] = set()
        self._sync_dropped = 0
        self._projection_lock = Lock()
        self._projection_clock = projection_clock
        self._projected_frontiers: dict[tuple[str, str], int] = {}
        self._projection_scanned: dict[tuple[str, str], int] = {}
        self._projection_failures: dict[
            tuple[str, str], dict[int, tuple[int, float, bool]]
        ] = {}
        self._projection_dirty: dict[tuple[str, str], tuple[int, str]] = {}
        self._projection_active: set[tuple[str, str]] = set()
        self._projection_failed: set[tuple[str, str, int]] = set()
        self._initialize_projection_frontiers()
        self._worker_futures: set[Future[object]] = set()
        self._worker_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"orgfs-worker-{store.space_id[:12]}",
        )
        owner_ref = weakref.ref(self)

        def handle_ingress(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_ingress_command(command)

        def execute_ingress(command: _LogIngress) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._process_log_ingress(command)

        def complete_ingress(
            completion: EffectCompleted[None],
        ) -> AdmissionResult:
            owner = owner_ref()
            if owner is None:
                return AdmissionResult.CLOSED
            return owner._ingress_runtime.tell(owner._ingress_actor, completion)

        self._ingress_generation = 1
        self._ingress_dropped = 0
        self._ingress_guard = Lock()
        self._ingress_pending: set[tuple[str, int]] = set()
        self._ingress_capacity = ORGFS_LOG_INGRESS_CAPACITY
        self._ingress_closing = False
        self._ingress_closed = False
        self._ingress_deferred: list[EffectRequest[_LogIngress]] = []
        self._ingress_runtime = ActorRuntime()
        self._ingress_actor: ActorHandle = self._ingress_runtime.start(
            ActorSpec(
                f"orgfs-space-{store.space_id[:12]}-ingress",
                lambda: handle_ingress,
                mailbox_capacity=ORGFS_LOG_INGRESS_CAPACITY,
                supervision_profile="state_authority",
            )
        )
        self._ingress_effects: EffectLane[_LogIngress, None] = EffectLane(
            name=f"orgfs-space-{store.space_id[:12]}-effects",
            execute=execute_ingress,
            complete=complete_ingress,
            capacity=ORGFS_LOG_INGRESS_CAPACITY,
            workers=1,
        )
        self._registrations: list[Registration] = []
        log_ingress = (
            self._admit_log_sample
            if isinstance(session, (ZenohTransport, TransportSessionAuthority))
            else self._on_log
        )
        self._registrations.append(
            session.subscribe(
                self._keys.orgfs_log_any(store.space_id), log_ingress
            )
        )
        try:
            self._registrations.append(
                session.declare_query_handler(
                    self._keys.orgfs_sync(store.space_id, node_id), self._handle_sync
                )
            )
            self._registrations.append(
                session.declare_query_handler(
                    self._keys.orgfs_log_any(store.space_id), self._handle_log_range
                )
            )
            if blob_store is not None:
                self._registrations.append(
                    session.declare_query_handler(
                        f"{self._keys.prefix}/org/fs/blob/*", self._handle_blob
                    )
                )
        except BaseException:
            self.close()
            raise


    def _log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger(level, event, **fields)


    def _log_blob_fetch_failed(
        self, event: str, exc: BaseException, *, supplier: str, digest: str
    ) -> None:
        details = getattr(exc, "details", None)
        details = details if isinstance(details, Mapping) else {}
        self._log(
            "warn",
            event,
            reason=getattr(exc, "code", "blob-unavailable"),
            supplier=supplier,
            digest=digest,
            spaceId=self.store.space_id,
            replies=list(details.get("replies", ())),
            elapsedMs=details.get("elapsedMs"),
        )


    def _supplier_allowed(self, supplier: str) -> bool:
        try:
            return bool(supplier) and bool(self._supplier_online(supplier))
        except Exception:
            return False


    @property
    def _announced_nodes(self) -> set[str]:
        """Deprecated test view of runtime discovery; never an admission gate."""

        source = self._holder_discovery
        return set(source() if callable(source) else source)


    def close(self, timeout: float = 5.0) -> None:
        if self._ingress_closed:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        with self._worker_lock:
            self._closed = True
        for registration in reversed(self._registrations):
            registration.close()
        self._registrations.clear()
        with self._ingress_guard:
            self._ingress_closing = True
        while time.monotonic() < deadline:
            snapshot = self._ingress_runtime.snapshot(self._ingress_actor)
            if (
                snapshot.queued == 0
                and snapshot.in_flight == 0
                and not self._ingress_deferred
                and self.ingress_pending == 0
            ):
                break
            time.sleep(0.005)
        else:
            raise TimeoutError("orgfs log ingress did not drain")
        if not self._ingress_effects.close(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("orgfs log effects did not drain")
        self._ingress_generation += 1
        if not self._ingress_runtime.stop(
            self._ingress_actor, timeout=max(0.0, deadline - time.monotonic())
        ):
            raise TimeoutError("orgfs log ingress actor did not stop")
        # Accepted sync/blob jobs keep custody after a caller's close deadline.
        # Do not let ThreadPoolExecutor's unbounded join defeat that deadline.
        self._worker_executor.shutdown(wait=False, cancel_futures=False)
        with self._worker_lock:
            pending_workers = tuple(self._worker_futures)
        if pending_workers:
            _, unfinished = wait(pending_workers, timeout=max(0.0, deadline - time.monotonic()))
            if unfinished:
                raise TimeoutError("orgfs sync/blob workers did not drain")
        with self._worker_lock:
            self._blob_fetch_pending.clear()
            self._blob_fetch_pending_count = 0
            self._replica_blob_pending.clear()
            waiters = tuple(
                waiter
                for batch in self._blob_fetch_waiters.values()
                for waiter in batch
            )
            self._blob_fetch_waiters.clear()
            self._sync_pending.clear()
            self._sync_rerun.clear()
        self._ingress_closed = True
        for waiter in waiters:
            waiter.set()


__all__ = [
    "ORGFS_ENVELOPE_BYTES",
    "ORGFS_BLOB_FETCH_QUEUE_LIMIT",
    "ORGFS_LOG_RANGE_LIMIT",
    "ORGFS_PAGE_BYTES",
    "ORGFS_SYNC_RETRY_BACKOFF_SECONDS",
    "ORGFS_SYNC_QUEUE_LIMIT",
    "OrgFsMesh",
]
