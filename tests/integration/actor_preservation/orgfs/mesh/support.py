"""Loopback production-entry coverage for the P1 mesh rework."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs import mesh as mesh_module
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import assert_client_ids_registered
from hyprial.daemon.impl.configuration.identity import classify_target_identity
from hyprial.daemon.impl.transport import (
    KeySpace,
    LivelinessDirectory,
    ZenohConfig,
    ZenohTransport,
)
from hyprial.kernel import TARGET_KIND_HOST
from tests.support.tailnet_daemon import bind_admitted


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (OrgFsError, AssertionError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _assert_true(value):
    assert value
    return value


def _assert_equal(actual, expected):
    assert actual == expected
    return actual


def _assert_registered(space_id: str, *runtimes: OrgFsRuntime) -> None:
    for runtime in runtimes:
        assert_client_ids_registered(runtime.stores.get(space_id))


def _bind_with_daemon_presence(runtime, transport):
    """Use the same host-class + shared liveliness verdict as DaemonApplication."""

    presence = LivelinessDirectory(transport)
    bind_admitted(
        runtime, transport,
        observe_supplier_online=presence.observe_actor_online,
        supplier_online=lambda supplier: (
            classify_target_identity(supplier) == TARGET_KIND_HOST
            and presence.actor_online(supplier)
        ),
        holder_candidates=lambda: tuple(
            actor
            for actor in presence.online_actors()
            if classify_target_identity(actor) == TARGET_KIND_HOST
        ),
    )
    token = transport.declare_liveliness(KeySpace().actor_liveliness(runtime.node_id))
    return presence, token


def _large_write_with_recovery_spies(tmp_path, *, fetch_fails: bool):
    """Drive a >512000-byte write across two loopback meshes and record, on the
    reader, the blob fetches and the re-imports made by the recovery worker."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    events: list[tuple[str, str, dict]] = []
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(
        tmp_path / "reader",
        node_id="node-b",
        author="user:reader",
        logger=lambda level, event, **fields: events.append((level, event, fields)),
    )
    fetches: list[tuple[str, str]] = []
    recovery_reimports: list[str] = []
    recovery_done = threading.Event()
    in_recovery = threading.local()
    original_fetch = mesh_module.OrgFsMesh._fetch_blob_into_store
    original_run = mesh_module.OrgFsMesh._run_blob_recovery

    def spy_fetch(self, supplier, digest):
        fetches.append((supplier, digest))
        if fetch_fails:
            raise mesh_module.StoreError("blob-unavailable", "test: supplier lost it")
        return original_fetch(self, supplier, digest)

    def spy_run(self, digest):
        # Observe the recovery worker's typed import entry. The store mutation
        # itself runs on the space authority thread and cannot see this
        # worker's thread-local recovery marker.
        original_import = self._import_envelope

        def counted_import(envelope, **kwargs):
            if getattr(in_recovery, "active", False):
                recovery_reimports.append(digest)
            return original_import(envelope, **kwargs)

        self._import_envelope = counted_import
        in_recovery.active = True
        try:
            return original_run(self, digest)
        finally:
            in_recovery.active = False
            self._import_envelope = original_import
            recovery_done.set()

    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        # Model admitted private peers; retain liveliness and content checks.
        bind_admitted(owner, owner_transport, supplier_online=online)
        bind_admitted(reader, reader_transport, supplier_online=online)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: reader.facade.join(space.space_id))

        mesh_module.OrgFsMesh._fetch_blob_into_store = spy_fetch
        mesh_module.OrgFsMesh._run_blob_recovery = spy_run
        content = "retry-once-" + "y" * 900_000
        created = owner.facade.write_text(space.space_id, "large.md", content)
        assert recovery_done.wait(8), "the reader never ran blob recovery"
        # The tree entry travels separately from the large update; wait until the
        # node resolves on the reader before reading it.
        if fetch_fails:
            _eventually(
                lambda: reader.facade.stat(
                    space.space_id, f"id:{created.node_id}"
                )
            )
            with pytest.raises(OrgFsError) as pending:
                reader.facade.read_text(space.space_id, f"id:{created.node_id}")
            assert pending.value.code == "content-pending"
            read_back = ""
        else:
            read_back = _eventually(
                lambda: _assert_equal(
                    reader.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                    content,
                )
            )
        return content, read_back, fetches, recovery_reimports, events
    finally:
        mesh_module.OrgFsMesh._fetch_blob_into_store = original_fetch
        mesh_module.OrgFsMesh._run_blob_recovery = original_run
        reader.close()
        owner.close()
        reader_transport.close()
        owner_transport.close()
