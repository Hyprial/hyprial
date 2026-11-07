"""Focused contract tests for resident replica backends."""

from __future__ import annotations

import builtins
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import document_file_stem
from hyprial.daemon.impl.orgfs.storage.replica import (
    FsReplicaBackend,
    MemoryReplicaBackend,
    PutOutcome,
    ReplicaStore,
    configured_state_root)


class _View:
    def retired(self, _doc_id: str) -> None:
        return None

    def purge_listed(self, _digest: str) -> bool:
        return False

    def snapshot_point(self, _doc_id: str) -> None:
        return None


@pytest.mark.parametrize("kind", ["memory", "fs"])
def test_put_absent_creates_and_same_bytes_idempotent(
    kind: str, tmp_path: Path
) -> None:
    backend = (
        MemoryReplicaBackend()
        if kind == "memory"
        else FsReplicaBackend("space", tmp_path)
    )
    key = "log/space/doc-a/writer-a/0"

    assert backend.put(key, b"envelope") is PutOutcome.CREATED
    assert backend.put(key, b"envelope") is PutOutcome.IDEMPOTENT
    assert backend.get(key) == b"envelope"


def test_put_conflict_different_bytes_rejected_and_event() -> None:
    backend = MemoryReplicaBackend()
    events: list[tuple[str, dict[str, object]]] = []
    notices: list[tuple[str, dict[str, object]]] = []
    replica = ReplicaStore(
        "space",
        backend,
        _View(),
        event_callback=lambda kind, details: events.append((kind, dict(details))),
        owner_notifier=lambda kind, details: notices.append((kind, dict(details))),
    )
    key = "log/space/doc-a/writer-a/0"
    assert replica.store_envelope(key, b"first") is PutOutcome.CREATED

    with pytest.raises(OrgFsError) as conflict:
        replica.store_envelope(key, b"different")

    assert conflict.value.code == "log-key-conflict"
    assert backend.get(key) == b"first"
    assert events == [("log-key-conflict", {"spaceId": "space", "key": key})]
    assert notices == events


def test_fs_backend_concurrent_put_single_winner(tmp_path: Path) -> None:
    backend = FsReplicaBackend("space", tmp_path)
    key = "log/space/doc-a/writer-a/0"
    payloads = tuple(b"alpha" if index % 2 else b"beta" for index in range(64))

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = tuple(pool.map(lambda payload: backend.put(key, payload), payloads))

    assert outcomes.count(PutOutcome.CREATED) == 1
    assert set(outcomes) <= {
        PutOutcome.CREATED,
        PutOutcome.IDEMPOTENT,
        PutOutcome.CONFLICT,
    }
    winner = backend.get(key)
    assert winner in {b"alpha", b"beta"}
    for payload, outcome in zip(payloads, outcomes, strict=True):
        assert outcome is not PutOutcome.IDEMPOTENT or payload == winner
        assert outcome is not PutOutcome.CONFLICT or payload != winner


def test_fs_memory_backend_byte_equivalence(tmp_path: Path) -> None:
    memory = MemoryReplicaBackend()
    filesystem = FsReplicaBackend("space", tmp_path)
    operations = (
        ("log/space/doc-a/writer-a/0", b"zero"),
        ("log/space/doc-a/writer-a/1", b"one"),
        ("snapshot/space/doc-a/snapshot-a", b"snapshot"),
        ("blob/" + "a" * 64, b"blob"),
    )
    for key, data in operations:
        assert memory.put(key, data) is filesystem.put(key, data)
        assert memory.exists(key) is filesystem.exists(key) is True
    memory.delete(operations[1][0])
    filesystem.delete(operations[1][0])

    deleted_key = operations[1][0]
    assert memory.exists(deleted_key) is filesystem.exists(deleted_key) is False

    assert tuple(memory.keys("")) == tuple(filesystem.keys(""))
    for key in memory.keys(""):
        assert memory.get(key) == filesystem.get(key)


def test_memory_backend_reports_not_durable() -> None:
    assert MemoryReplicaBackend().durable() is False


def test_fs_backend_uses_selected_state_root_and_rejects_unsafe_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "selected-state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    backend = FsReplicaBackend("space")
    key = "log/space/doc-a/writer-a/0"

    assert backend.put(key, b"value") is PutOutcome.CREATED
    stored = state / "orgfs" / "replica" / "space" / "log" / "space"
    assert (stored / document_file_stem("doc-a") / "writer-a" / "0").read_bytes() == b"value"
    with pytest.raises(ValueError):
        backend.put("log/space/../writer-a/0", b"escape")


def test_configured_state_root_does_not_import_private_cli_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "hyprial.cli":
            raise AssertionError("replica state selection must not import hyprial.cli")
        return real_import(name, *args, **kwargs)

    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert configured_state_root() == state.resolve()
    monkeypatch.delenv("HARNESS_STATE_DIR")
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    assert configured_state_root() == (tmp_path / "home" / "state").resolve()


@pytest.mark.parametrize("kind", ["memory", "fs"])
def test_backend_rejects_leading_dot_segments(kind: str, tmp_path: Path) -> None:
    backend = (
        MemoryReplicaBackend()
        if kind == "memory"
        else FsReplicaBackend("space", tmp_path)
    )

    with pytest.raises(ValueError, match="safe key segment"):
        backend.put("log/space/.hidden/writer-a/0", b"invisible")


def test_fs_backend_cleans_crash_orphaned_temporary_files(tmp_path: Path) -> None:
    parent = tmp_path / "orgfs" / "replica" / "space" / "log" / "space" / "doc-a"
    parent.mkdir(parents=True)
    orphan = parent / ".0.tmp-abandoned"
    orphan.write_bytes(b"not committed")

    FsReplicaBackend("space", tmp_path)

    assert not orphan.exists()


@pytest.mark.parametrize("kind", ["memory", "fs"])
def test_m8_backends_pin_log_and_snapshot_keys_to_their_space(
    kind: str, tmp_path: Path
) -> None:
    backend = (
        MemoryReplicaBackend("space-a")
        if kind == "memory"
        else FsReplicaBackend("space-a", tmp_path)
    )

    with pytest.raises(ValueError, match="another space"):
        backend.put("log/space-b/doc-a/writer-a/0", b"wrong space")
    with pytest.raises(ValueError, match="another space"):
        backend.put("snapshot/space-b/doc-a/snapshot-a", b"wrong space")


@pytest.mark.parametrize("kind", ["memory", "fs"])
def test_m8_backend_prefixes_stop_at_path_boundaries(kind: str, tmp_path: Path) -> None:
    backend = (
        MemoryReplicaBackend("space")
        if kind == "memory"
        else FsReplicaBackend("space", tmp_path)
    )
    exact = "log/space/doc-a/writer-a/0"
    neighbor = "log/space/doc-ab/writer-a/0"
    backend.put(exact, b"exact")
    backend.put(neighbor, b"neighbor")

    assert tuple(backend.keys("log/space/doc-a")) == (exact,)
