# Decisions:
# - Triage §5.1: the contract seam is a facade/runtime factory, never a store
#   protocol.  Every contract test constructs OrgFS only through an
#   ``OrgFsImpl`` from the ``orgfs_impl`` fixture.
# - Package S (2026-11-25): the journal implementation and its ``LegacyImpl``
#   are deleted; ``StateImpl`` is the only implementation.  The fixture keeps
#   its ``state`` parameter id so test ids stay comparable across the switch.
# - Everything implementation specific lives here and nowhere in the tests:
#   failure injection, multi-node wiring, the trash restore adapter and the
#   state-vector probe used by the design §3.3 contract.
# - ``make_facade`` builds ``LocalOrgFs`` over per-facade ``MemoryStateStore``
#   backings.  Failure injection wraps every store the facade opens,
#   including the store of a space created inside the block; the vector probe
#   reads ``SyncHost.read_vectors`` (C4 report §7-3).  The in-memory facade
#   gets an in-memory content-addressed blob store; without one it accepts
#   write_bytes but can never read the bytes back.
# - ``make_runtime`` builds ``OrgFsRuntime`` (file state store, protocol 3
#   sync) and ``connect`` binds runtimes to one ``FakeNetwork`` (presence
#   ``online_for``, online edges ``observe_for``).  Runtime failure injection
#   wraps ``commit`` of the space stores open on entry, once.
# - The runtime runs with ``trust_tailnet_members=False``, the daemon's
#   production setting: the node gate (§3.5) is the only receiver-side check
#   against a removed member.  The contract network has no org directory, so
#   ``_ContractStateRuntime`` resolves a node to its device owner from the
#   authors the factory created the runtimes with (the stand-in for device
#   rows).
"""Parameterized OrgFS implementation factories for the contract suite."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs import LocalOrgFs, NodeInfo, OrgFs, OrgFsError
from hyprial.daemon.impl.orgfs.document.coordinator import MemoryStores
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

from .network import FakeNetwork
from .vectors import decode_state_vector

class InjectedWriteFailure(RuntimeError):
    """The persistence failure raised by ``inject_write_failure``."""


class RuntimeHandle:
    """A durable OrgFS node that can be closed and reopened in place."""

    def __init__(
        self,
        impl: "StateImpl",
        state_dir: Path,
        node_id: str,
        author: str,
        runtime_class: Any,
    ) -> None:
        self._impl = impl
        self.state_dir = Path(state_dir)
        self.node_id = node_id
        self.author = author
        self._runtime_class = runtime_class
        self.network: FakeNetwork | None = None
        self.runtime: Any = None
        self._open()

    def _open(self) -> None:
        self.runtime = self._runtime_class(
            self.state_dir,
            node_id=self.node_id,
            author=self.author,
        )
        if self.network is not None:
            self._bind(self.network)

    def _bind(self, network: FakeNetwork) -> None:
        assert self.runtime is not None
        self.network = network
        session = network.add_node(self.node_id)
        self.runtime.bind_transport(
            session,
            supplier_online=network.online_for(self.node_id),
            holder_candidates=lambda: network.peers_of(self.node_id),
            observe_supplier_online=network.observe_for(self.node_id),
        )

    @property
    def facade(self) -> OrgFs:
        if self.runtime is None:
            raise OrgFsError("closed", {"message": "runtime handle is closed"})
        return self.runtime.facade

    def reopen(self) -> OrgFs:
        """Close normally, then construct the node again from the same state."""

        self.close()
        self._open()
        return self.facade

    def close(self) -> None:
        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            if self.network is not None:
                self.network.forget(self.node_id)
            runtime.close()


class _ContractStateRuntime(OrgFsRuntime):
    """``OrgFsRuntime`` whose device owners come from the contract factory."""

    device_owners: dict[str, str] = {}

    def _device_owner_resolver(self, space_id: str) -> Any:
        owners = self.device_owners
        return lambda node: owners.get(node)


def _state_runtime_class(owners: dict[str, str]) -> Any:
    def build(state_dir: Path, **kwargs: Any) -> OrgFsRuntime:
        kwargs["trust_tailnet_members"] = False
        runtime = _ContractStateRuntime(state_dir, **kwargs)
        runtime.device_owners = owners
        return runtime

    return build


class Connection:
    """Nodes attached to one in-process network."""

    def __init__(self, network: FakeNetwork, handles: tuple[RuntimeHandle, ...]) -> None:
        self.network = network
        self.handles = handles

    def disconnect(self, handle: RuntimeHandle) -> None:
        self.network.partition(handle.node_id)

    def reconnect(self, handle: RuntimeHandle) -> None:
        self.network.heal(handle.node_id)


class _MemoryBlobs:
    """Content-addressed in-memory blobs for the in-memory facade."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(self, space_id: str, data: bytes, *, reason: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.values[digest] = bytes(data)
        return digest

    def get(self, space_id: str, digest: str) -> bytes:
        return self.values[digest]

    def has(self, digest: str) -> bool:
        return digest in self.values


def _connect(handles: tuple[RuntimeHandle, ...]) -> Connection:
    network = FakeNetwork()
    for handle in handles:
        network.add_node(handle.node_id)
    for handle in handles:
        handle._bind(network)
    return Connection(network, handles)


@contextmanager
def _fail_runtime_stores(handle: RuntimeHandle, names: tuple[str, ...]) -> Iterator[None]:
    """Make the next commit of any store open on entry raise, once."""

    assert handle.runtime is not None
    stores = tuple(handle.runtime.stores.snapshot())
    state = {"failed": False}
    patched: list[tuple[Any, str]] = []

    def wrap(original: Any) -> Any:
        def failing(*args: Any, **kwargs: Any) -> Any:
            if not state["failed"]:
                state["failed"] = True
                raise InjectedWriteFailure("injected persistence failure")
            return original(*args, **kwargs)

        return failing

    for store in stores:
        for name in names:
            if hasattr(store, name):
                setattr(store, name, wrap(getattr(store, name)))
                patched.append((store, name))
    try:
        yield
    finally:
        for store, name in patched:
            delattr(store, name)


class _FailGate:
    """One state facade's one-shot persistence failure switch."""

    def __init__(self) -> None:
        self.armed = False


class _GatedStore:
    """A ``StateStore`` whose next commit raises while its gate is armed."""

    def __init__(self, store: Any, gate: _FailGate) -> None:
        self._store = store
        self._gate = gate
        self.space_id = store.space_id

    @property
    def recovered_batch(self) -> bool:
        return self._store.recovered_batch

    def load(self, doc_id: str) -> bytes | None:
        return self._store.load(doc_id)

    def commit(self, batch: Any) -> None:
        if self._gate.armed:
            self._gate.armed = False
            raise InjectedWriteFailure("injected persistence failure")
        self._store.commit(batch)

    def needs_compaction(self, doc_id: str) -> bool:
        return self._store.needs_compaction(doc_id)

    def compact(self, doc_id: str, state: bytes | None = None) -> None:
        self._store.compact(doc_id, state)

    def close(self) -> None:
        self._store.close()


class StateImpl:
    """Phase D assembly over StateStore / Admission / SpaceSync.

    ``make_facade`` is ``LocalOrgFs`` over ``MemoryStateStore``;
    ``make_runtime`` is ``OrgFsRuntime`` over ``FileStateStore`` and
    ``connect`` binds runtimes to one in-process network.
    """

    name = "state"

    def __init__(self) -> None:
        self._facades: list[LocalOrgFs] = []
        self._gates: dict[int, _FailGate] = {}
        self._handles: list[RuntimeHandle] = []
        self._device_owners: dict[str, str] = {}

    def make_runtime(self, state_dir: Path, node_id: str, author: str) -> RuntimeHandle:
        self._device_owners[node_id] = author
        handle = RuntimeHandle(
            self, state_dir, node_id, author, _state_runtime_class(self._device_owners)
        )
        self._handles.append(handle)
        return handle

    def connect(self, *handles: RuntimeHandle) -> Connection:
        return _connect(handles)

    def make_facade(self, author: str, actor: str | None = None) -> OrgFs:
        gate = _FailGate()
        stores = MemoryStores()

        def factory(space_id: str, create: bool) -> _GatedStore:
            return _GatedStore(stores(space_id, create), gate)

        facade = LocalOrgFs(
            blobs=_MemoryBlobs(),
            author=author,
            actor=actor,
            store_factory=factory,
            store_discard=stores.discard,
        )
        self._facades.append(facade)
        self._gates[id(facade)] = gate
        return facade

    @contextmanager
    def inject_write_failure(self, target: Any) -> Iterator[None]:
        if isinstance(target, RuntimeHandle):
            with _fail_runtime_stores(target, ("commit",)):
                yield
            return
        gate = self._gates.get(id(target))
        if gate is None:
            raise TypeError("inject_write_failure needs a make_facade or make_runtime target")
        gate.armed = True
        try:
            yield
        finally:
            gate.armed = False

    def restore_last(self, fs: OrgFs, space_id: str, node: str, *, recursive: bool = True) -> NodeInfo:
        """Restore a trashed node's last state (design §6, no version)."""

        node_id = node.removeprefix("id:") if node.startswith("id:") else None
        trash = fs.trash(space_id, limit=100_000)
        target = next(
            (
                entry
                for entry in trash
                if entry.node_id == node_id or (node_id is None and entry.path == node)
            ),
            None,
        )
        if target is None:
            raise OrgFsError("unknown-doc", {"node": node})
        return fs.restore(space_id, f"id:{target.node_id}", recursive=recursive)

    def document_vector(self, fs: OrgFs, space_id: str, doc: str) -> dict[int, int]:
        """Decoded state vector read through the space's ``SyncHost``."""

        if doc in ("meta", "tree"):
            doc_id = doc
        else:
            doc_id = fs.stat(space_id, doc).doc_id
            if doc_id is None:
                raise OrgFsError("invalid-argument", {"message": f"{doc} has no content document"})
        if not isinstance(fs, LocalOrgFs):
            raise TypeError("document_vector needs a state facade")
        host = fs.sync_host(space_id)
        return decode_state_vector(bytes(host.read_vectors((doc_id,))[doc_id]))

    def close(self) -> None:
        for handle in reversed(self._handles):
            handle.close()
        for facade in self._facades:
            facade.close_effects(timeout=5.0)


OrgFsImpl = StateImpl

__all__ = [
    "Connection",
    "InjectedWriteFailure",
    "OrgFsImpl",
    "RuntimeHandle",
    "StateImpl",
]
