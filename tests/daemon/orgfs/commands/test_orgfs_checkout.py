from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from hyprial.daemon.impl.orgfs import ChangeEvent, LocalOrgFs
from hyprial.daemon.impl.orgfs.projection.checkout import (
    ORGFS_CHECKOUT_STATUS_FILE,
    CheckoutManager)
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, TransportSample


class _BlobStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.unavailable: set[str] = set()

    def put(self, _space_id: str, data: bytes, *, reason: str) -> str:
        assert reason
        digest = hashlib.sha256(data).hexdigest()
        self.values[digest] = bytes(data)
        return digest

    def get(self, _space_id: str, digest: str) -> bytes:
        if digest in self.unavailable:
            raise FileNotFoundError(digest)
        return self.values[digest]

    def contains(self, digest: str) -> bool:
        return digest in self.values and digest not in self.unavailable


@pytest.fixture(autouse=True)
def _restore_checkout_permissions(tmp_path: Path):
    """M9: leave pytest able to remove read-only checkout projections."""

    yield
    for current, directories, files in os.walk(
        tmp_path, topdown=False, followlinks=False
    ):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o600)
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(0o700)
        current_path.chmod(0o700)


def _checkout(tmp_path: Path) -> tuple[LocalOrgFs, _BlobStore, str, CheckoutManager]:
    blobs = _BlobStore()
    facade = LocalOrgFs(blobs=blobs, author="user:owner")
    space_id = facade.create_space("shared").space_id
    root = tmp_path / "state" / "orgfs" / "spaces" / space_id / "checkout"
    return facade, blobs, space_id, CheckoutManager(facade, blobs, space_id, root)


def _latest(facade: LocalOrgFs, space_id: str) -> ChangeEvent:
    return facade.changes_since(space_id)[-1]


def _make_writable(path: Path) -> None:
    path.chmod(0o755)
    for parent in path.parents:
        if parent.name == "checkout":
            parent.chmod(0o755)
            break


def test_materialize_full_tree_bytes_equal(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    facade.mkdir(space_id, "docs")
    facade.write_text(space_id, "docs/readme.md", "hello\n世界")
    facade.write_bytes(space_id, "payload.bin", b"\x00\xffpayload")

    checkout.materialize()

    assert (checkout.root / "docs" / "readme.md").read_bytes() == "hello\n世界".encode()
    assert (checkout.root / "payload.bin").read_bytes() == b"\x00\xffpayload"
    assert (
        json.loads((checkout.root / ORGFS_CHECKOUT_STATUS_FILE).read_text())[
            "unmaterialized"
        ]
        == []
    )


def test_checkout_files_are_read_only(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    facade.mkdir(space_id, "docs")
    facade.write_text(space_id, "docs/readme.md", "read only")
    checkout.materialize()

    target = checkout.root / "docs" / "readme.md"
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    assert stat.S_IMODE((checkout.root / "docs").stat().st_mode) == 0o555
    assert stat.S_IMODE(checkout.root.stat().st_mode) == 0o555
    if os.geteuid() == 0:
        pytest.skip("root bypasses write-denial mode bits; modes were asserted above")
    with pytest.raises(PermissionError):
        target.write_text("changed")


def test_apply_events_converges_with_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    original = b"a" * 500_000
    replacement = b"b" * 500_000
    node = facade.write_bytes(space_id, "large.bin", original)
    checkout.materialize()
    target = checkout.root / "large.bin"
    observed: list[bytes] = []
    stop = threading.Event()

    def read_repeatedly() -> None:
        while not stop.is_set():
            try:
                observed.append(target.read_bytes())
            except FileNotFoundError:
                pass

    reader = threading.Thread(target=read_repeatedly)
    reader.start()
    real_replace = os.replace
    target_replacements = 0

    def replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal target_replacements
        if Path(destination) == target:
            target_replacements += 1
        real_replace(source, destination)

    monkeypatch.setattr('hyprial.daemon.impl.orgfs.projection.checkout.os.replace', replace)
    facade.write_bytes(space_id, f"id:{node.node_id}", replacement)
    checkout.apply(_latest(facade, space_id))
    stop.set()
    reader.join(2)

    assert target_replacements == 1
    assert observed
    assert set(observed) <= {original, replacement}
    assert target.read_bytes() == replacement

    facade.mkdir(space_id, "archive")
    checkout.apply(_latest(facade, space_id))
    moved = facade.move(space_id, f"id:{node.node_id}", "archive/moved.bin")
    checkout.apply(_latest(facade, space_id))
    facade.remove(space_id, f"id:{node.node_id}")
    checkout.apply(_latest(facade, space_id))
    facade.restore(space_id, f"id:{node.node_id}", moved.version)
    checkout.apply(_latest(facade, space_id))
    assert not target.exists()
    assert (checkout.root / "archive" / "moved.bin").read_bytes() == replacement


def test_removed_node_disappears(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    node = facade.write_text(space_id, "gone.txt", "gone")
    checkout.materialize()
    facade.remove(space_id, f"id:{node.node_id}")

    checkout.apply(_latest(facade, space_id))

    assert not (checkout.root / "gone.txt").exists()


def test_r2_remove_and_move_nested_read_only_subtrees(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    removed = facade.mkdir(space_id, "removed")
    facade.mkdir(space_id, "removed/nested")
    facade.write_text(space_id, "removed/nested/file.txt", "remove me")
    moved = facade.mkdir(space_id, "moved")
    facade.mkdir(space_id, "moved/nested")
    facade.write_text(space_id, "moved/nested/file.txt", "move me")
    checkout.materialize()

    facade.remove(space_id, f"id:{removed.node_id}")
    checkout.apply(_latest(facade, space_id))
    assert not (checkout.root / "removed").exists()

    facade.move(space_id, f"id:{moved.node_id}", "renamed")
    checkout.apply(_latest(facade, space_id))
    assert not (checkout.root / "moved").exists()
    assert (checkout.root / "renamed" / "nested" / "file.txt").read_text() == (
        "move me"
    )


def test_r2_remove_path_makes_nested_read_only_subtree_writable(
    tmp_path: Path,
) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    facade.mkdir(space_id, "subtree")
    facade.mkdir(space_id, "subtree/nested")
    facade.write_text(space_id, "subtree/nested/file.txt", "content")
    checkout.materialize()
    subtree = checkout.root / "subtree"

    checkout._remove_path(subtree)

    assert not subtree.exists()


def test_m7_remove_path_refuses_target_outside_checkout(tmp_path: Path) -> None:
    _facade, _blobs, _space_id, checkout = _checkout(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    checkout._remove_path(outside)

    assert outside.read_text(encoding="utf-8") == "keep"


def test_move_file_out_preserves_still_projected_empty_directory(
    tmp_path: Path,
) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    facade.mkdir(space_id, "d")
    facade.mkdir(space_id, "elsewhere")
    node = facade.write_text(space_id, "d/f", "content")
    checkout.materialize()

    facade.move(space_id, f"id:{node.node_id}", "elsewhere/f")
    checkout.apply(_latest(facade, space_id))

    assert (checkout.root / "d").is_dir()
    assert list((checkout.root / "d").iterdir()) == []
    assert (checkout.root / "elsewhere" / "f").read_text() == "content"


def test_name_conflict_materialized_with_node_id_suffix(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    first = facade.write_text(space_id, "same.txt", "one")
    second = facade.write_text(space_id, "other.txt", "two")
    checkout.materialize()
    facade.move(space_id, f"id:{second.node_id}", "same.txt")
    checkout.apply(_latest(facade, space_id))

    assert not (checkout.root / "same.txt").exists()
    assert not (checkout.root / "other.txt").exists()
    assert (
        checkout.root / f"same.txt.conflict-{first.node_id[:8]}"
    ).read_text() == "one"
    assert (
        checkout.root / f"same.txt.conflict-{second.node_id[:8]}"
    ).read_text() == "two"


def test_hostile_name_never_escapes_checkout_root(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    hostile = facade.write_text(space_id, "safe.txt", "contained")
    facade._spaces[space_id].nodes[hostile.node_id].name = "../escaped.txt"
    outside = checkout.root.parent / "escaped.txt"

    checkout.materialize()

    assert not outside.exists()
    quarantine = checkout.root / f"orgfs-quarantine-{hostile.node_id[:8]}"
    assert quarantine.read_text() == "contained"
    status_value = json.loads((checkout.root / ORGFS_CHECKOUT_STATUS_FILE).read_text())
    assert status_value["unmaterialized"] == [
        {
            "nodeId": hostile.node_id,
            "path": quarantine.name,
            "reason": "unsafe-name-placeholder",
        }
    ]

    facade.mkdir(space_id, "link")
    checkout.apply(_latest(facade, space_id))
    _make_writable(checkout.root)
    projected_directory = checkout.root / "link"
    projected_directory.chmod(0o755)
    projected_directory.rmdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    projected_directory.symlink_to(outside_directory, target_is_directory=True)
    escaped = facade.write_text(space_id, "link/pwned.txt", "no")
    checkout.apply(_latest(facade, space_id))

    assert not (outside_directory / "pwned.txt").exists()
    status_value = json.loads((checkout.root / ORGFS_CHECKOUT_STATUS_FILE).read_text())
    assert any(
        item["nodeId"] == escaped.node_id and item["reason"] == "unsafe-path"
        for item in status_value["unmaterialized"]
    )
    checkout.disable()


def test_blob_unavailable_skips_and_records_status_not_hang(tmp_path: Path) -> None:
    facade, blobs, space_id, checkout = _checkout(tmp_path)
    blob = facade.write_bytes(space_id, "cold.bin", b"cold")
    assert blob.blob_hash is not None
    blobs.unavailable.add(blob.blob_hash)
    finished = threading.Event()

    thread = threading.Thread(target=lambda: (checkout.materialize(), finished.set()))
    thread.start()
    thread.join(1)

    assert finished.is_set()
    assert not (checkout.root / "cold.bin").exists()
    status_value = json.loads((checkout.root / ORGFS_CHECKOUT_STATUS_FILE).read_text())
    assert status_value["unmaterialized"] == [
        {
            "nodeId": blob.node_id,
            "path": "cold.bin",
            "reason": "blob-unavailable",
        }
    ]

    blobs.unavailable.clear()
    facade.write_text(space_id, "unrelated.txt", "event")
    checkout.apply(_latest(facade, space_id))
    assert (checkout.root / "cold.bin").read_bytes() == b"cold"


def test_reconcile_retries_unmaterialized_blob(tmp_path: Path) -> None:
    facade, blobs, space_id, checkout = _checkout(tmp_path)
    blob = facade.write_bytes(space_id, "cold.bin", b"cold")
    assert blob.blob_hash is not None
    blobs.unavailable.add(blob.blob_hash)
    checkout.materialize()
    assert not (checkout.root / "cold.bin").exists()

    blobs.unavailable.clear()
    checkout.reconcile()

    assert (checkout.root / "cold.bin").read_bytes() == b"cold"
    status_value = json.loads((checkout.root / ORGFS_CHECKOUT_STATUS_FILE).read_text())
    assert status_value["unmaterialized"] == []


def test_m7_peer_liveliness_reconciles_checkout(tmp_path: Path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:a")

    class _Checkout:
        calls = 0

        def reconcile(self) -> None:
            self.calls += 1

    checkout = _Checkout()
    runtime._checkouts["space-a"] = checkout
    try:
        runtime._on_peer_liveliness(
            TransportSample(KeySpace().actor_liveliness("node-b"), b"")
        )
        assert checkout.calls == 1
    finally:
        runtime.close()


def test_status_sidecar_omits_meaningless_last_sync_version(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    facade.write_text(space_id, "readme.md", "content")

    checkout.materialize()

    status_value = json.loads((checkout.root / ORGFS_CHECKOUT_STATUS_FILE).read_text())
    assert "lastSyncVersion" not in status_value


def test_disable_removes_checkout_dir(tmp_path: Path) -> None:
    facade, _blobs, space_id, checkout = _checkout(tmp_path)
    facade.write_text(space_id, "readme.md", "content")
    checkout.materialize()

    checkout.disable()

    assert not checkout.root.exists()
