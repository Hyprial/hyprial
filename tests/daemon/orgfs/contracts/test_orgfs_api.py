# Decisions (phase D, package T3, 2026-11-25):
# - The cases that the contract suite now covers were deleted rather than
#   rewritten (C4 report §5); the T3 report lists each one with its contract.
# - The two rewritten cases build ``LocalOrgFs`` directly, so they run the new
#   facade whether or not ``HYPRIAL_ORGFS_IMPL`` is set.
# - Serialization of a remote apply with a local write is observed through the
#   facade's ``store_factory`` seam (a test store that parks one commit) and the
#   remote edit arrives through ``sync_host(space_id).apply_remote`` as a C2
#   ``forge_content`` frame; nothing reads the store or registry internals.
from __future__ import annotations

import inspect
import threading

from hyprial.daemon.impl.orgfs import LocalOrgFs, OrgFs
from hyprial.daemon.impl.orgfs.composition.vocabulary import _SpaceMetaWatcher
from hyprial.daemon.impl.orgfs.document.coordinator import MemoryStores
from hyprial.daemon.impl.orgfs.membership import Accepted

from tests.daemon.orgfs.membership.support import forge_content

EMPTY_VECTOR = b"\x00"


def test_public_protocol_does_not_expose_watch_meta() -> None:
    """A4: membership meta watching is an orgfs-internal interface."""

    assert not hasattr(OrgFs, "watch_meta")
    internal = inspect.signature(_SpaceMetaWatcher.watch_meta)
    implementation = inspect.signature(LocalOrgFs.watch_meta)
    for signature in (internal, implementation):
        assert tuple(signature.parameters) == ("self", "space_id", "callback")


class _ParkingStore:
    """A state store that parks the next commit of one document until released."""

    def __init__(self, inner, gate: "_Gate") -> None:
        self._inner = inner
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def commit(self, batch, *args, **kwargs):
        if self._gate.doc_id is not None and any(
            doc_id == self._gate.doc_id for doc_id, _update in batch.appends
        ):
            self._gate.doc_id = None
            self._gate.entered.set()
            assert self._gate.release.wait(5)
        return self._inner.commit(batch, *args, **kwargs)


class _Gate:
    def __init__(self) -> None:
        self.doc_id: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()


def test_facade_serializes_inbound_apply_without_swapping_local_identity() -> None:
    """F4: a remote apply and a local write serialize; the local identity stays."""

    gate = _Gate()
    stores = MemoryStores()
    fs = LocalOrgFs(
        author="user:owner",
        actor="agent:local",
        store_factory=lambda space_id, create: _ParkingStore(stores(space_id, create), gate),
        store_discard=stores.discard,
    )
    try:
        space = fs.create_space("team")
        fs.invite(space.space_id, "user:remote", "rw")
        item = fs.write_text(space.space_id, "note.md", "local-before")
        doc_id = item.doc_id or ""
        host = fs.sync_host(space.space_id)
        found = host.read_update(doc_id, EMPTY_VECTOR)
        assert found is not None
        frame = forge_content(doc_id, found[0], "remote", author=None, clock=True, client_id=424242)

        gate.doc_id = doc_id
        outcomes: list[object] = []
        inbound = threading.Thread(
            target=lambda: outcomes.append(
                host.apply_remote(doc_id, frame.update, frame.vector, "node-remote")
            ),
            daemon=True,
        )
        inbound.start()
        assert gate.entered.wait(2)
        assert (fs.author, fs.actor) == ("user:owner", "agent:local")

        local_started = threading.Event()
        local_done = threading.Event()

        def local_write() -> None:
            local_started.set()
            fs.write_text(space.space_id, "note.md", "local-after")
            local_done.set()

        local = threading.Thread(target=local_write, daemon=True)
        local.start()
        assert local_started.wait(2)
        assert not local_done.wait(0.1)
        gate.release.set()
        inbound.join(2)
        local.join(2)
        assert not inbound.is_alive()
        assert local_done.is_set()
        assert outcomes == [Accepted()]
        assert fs.read_text(space.space_id, "note.md")[0] == "local-after"
        assert fs.stat(space.space_id, "note.md").modified_by == "user:owner"
        assert (fs.author, fs.actor) == ("user:owner", "agent:local")
    finally:
        gate.release.set()
        assert fs.close_effects()


def test_a_reinvite_keeps_added_at_and_the_owner_keeps_the_space_birth() -> None:
    """added_at orders who dials whom between org devices (org.network.dialing),
    so a re-invite of a current member (a second device, a mode change) must
    not move it, and the owner's row stays at the space's creation time."""

    fs = LocalOrgFs(author="user:owner", actor="agent:owner:test")
    try:
        space = fs.create_space("team")

        def added_at(user: str) -> str:
            return next(m.added_at for m in fs.members(space.space_id) if m.user == user)

        first = fs.invite(space.space_id, "user:reader", "ro").added_at
        again = fs.invite(space.space_id, "user:reader", "rw")
        assert again.added_at == first == added_at("user:reader")
        assert again.mode == "rw"

        fs.invite(space.space_id, "user:owner", "rw")
        assert added_at("user:owner") == space.created_at

        fs.remove_member(space.space_id, "user:reader")
        back = fs.invite(space.space_id, "user:reader", "rw")
        assert back.added_at != first
    finally:
        assert fs.close_effects()


def test_structured_documents_are_not_part_of_the_orgfs_surface() -> None:
    """Q6 (owner 2026-11-25): open_doc and its handle types were removed."""

    import hyprial.daemon.impl.orgfs as orgfs_package
    from hyprial.daemon.impl.orgfs import api

    assert not hasattr(OrgFs, "open_doc")
    assert not hasattr(LocalOrgFs, "open_doc")
    removed = ("OrgDoc", "DocMap", "DocList", "DocText", "StructuredOrgDoc")
    for name in removed:
        assert name not in orgfs_package.__all__
        assert not hasattr(orgfs_package, name)
        assert not hasattr(api, name)
