"""One orgfs write must cost the same however large the space is.

2026-10-06: every write rewrote and fsynced the state file of every document
in the space, and kept an in-memory copy of the whole space (every file's full
text) per version, never freed.  With 400 files one write cost ~443 fsyncs,
~281 KB on disk and ~408 KB of permanent memory, and filling the space took
minutes.  A timing test cannot catch that on a small CI space; counting what a
write touches can, because a whole-space rewrite scales with the space.
"""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.document.model import _SpaceSnapshot
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore


_HISTORY_DOC_ID = "doc-00000000-0000-0000-0000-000000000001"
_HISTORY_TREE_ID = "tree-00000000-0000-0000-0000-000000000001"


def _space_with_files(path: Path, count: int) -> tuple[OrgFsRuntime, str]:
    runtime = OrgFsRuntime(path, node_id="node-cost", author="user:cost")
    space_id = runtime.facade.create_space("cost").space_id
    for index in range(count):
        runtime.facade.write_text(space_id, f"file-{index}.md", "x" * 200)
    runtime.facade.write_text(space_id, "probe.md", "before")
    return runtime, space_id


def _write_cost(
    path: Path, monkeypatch: pytest.MonkeyPatch, files: int
) -> dict[str, tuple[int, int, int]]:
    runtime, space_id = _space_with_files(path, files)
    try:
        # Start both measurements from a fresh state-file checkpoint so a
        # batched refresh cannot land inside one of them.
        runtime.stores.get(space_id).checkpoint()
        counts = {"fsync": 0, "bytes": 0}
        real_fsync = os.fsync
        real_write_bytes = Path.write_bytes

        def counting_fsync(fd: int) -> None:
            counts["fsync"] += 1
            real_fsync(fd)

        def counting_write_bytes(self: Path, data: bytes) -> int:
            counts["bytes"] += len(data)
            return real_write_bytes(self, data)

        monkeypatch.setattr(os, "fsync", counting_fsync)
        monkeypatch.setattr(Path, "write_bytes", counting_write_bytes)
        space = runtime.facade._spaces[space_id]  # noqa: SLF001
        cost: dict[str, tuple[int, int, int]] = {}
        for name, write in (
            ("overwrite", lambda: runtime.facade.write_text(space_id, "probe.md", "after")),
            ("create", lambda: runtime.facade.write_text(space_id, "new.md", "fresh")),
        ):
            counts.update(fsync=0, bytes=0)
            node = write()
            layer = space.snapshots[node.version]
            assert isinstance(layer, _SpaceSnapshot)
            cost[name] = (
                counts["fsync"],
                counts["bytes"],
                len(layer._changes),  # noqa: SLF001
            )
        monkeypatch.undo()
        return cost
    finally:
        runtime.close()


def test_a_write_costs_the_same_in_a_small_and_a_large_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = _write_cost(tmp_path / "small", monkeypatch, 20)
    large = _write_cost(tmp_path / "large", monkeypatch, 200)

    # An overwrite touches no state file and records one changed node.
    assert small["overwrite"] == large["overwrite"] == (0, 0, 1), (small, large)
    # A new file writes only its own new state file.  Its size varies by a
    # byte or two with the random node id's client-id encoding, so bound it
    # instead of comparing exactly; a whole-space rewrite is ~1 KB per file.
    for cost in (small, large):
        fsyncs, written, entries = cost["create"]
        assert (fsyncs, entries) == (1, 1), (small, large)
        assert written < 256, (small, large)


def test_unchanged_files_share_their_snapshot_entry_across_versions(
    tmp_path: Path,
) -> None:
    runtime, space_id = _space_with_files(tmp_path, 5)
    try:
        space = runtime.facade._spaces[space_id]  # noqa: SLF001
        first = runtime.facade.write_text(space_id, "probe.md", "one")
        second = runtime.facade.write_text(space_id, "probe.md", "two")
        old, new = space.snapshots[first.version], space.snapshots[second.version]
        untouched = next(
            node_id for node_id in new if new[node_id].name == "file-0.md"
        )
        assert new[untouched] is old[untouched]
        assert old[first.node_id].content == b"one"
        assert new[second.node_id].content == b"two"
        assert runtime.facade.read_at(space_id, f"id:{first.node_id}", first.version) == b"one"
    finally:
        runtime.close()


def test_a_delete_only_content_change_is_not_shared_from_the_old_version(
    tmp_path: Path,
) -> None:
    # Reuse is decided per node from its metadata and a content fingerprint.
    # The CRDT state vector does not move on a delete-only edit, so change the
    # text that way with the node's metadata untouched: the next version must
    # still carry the new text, not the previous entry.
    runtime, space_id = _space_with_files(tmp_path, 1)
    try:
        facade = runtime.facade
        space = facade._spaces[space_id]  # noqa: SLF001
        node = facade.write_text(space_id, "probe.md", "hello world")
        del space.contents[node.doc_id].text[0:6]

        layer = facade._snapshot(space)  # noqa: SLF001

        assert layer[node.node_id].content == b"world"
    finally:
        runtime.close()


class _CommitRecorder:
    def __init__(self) -> None:
        self.commits: list[str] = []

    def commit(self, doc_id, mutate, *, author, actor):  # type: ignore[no-untyped-def]
        del author, actor
        self.commits.append(doc_id)
        mutate(None)
        return None

    def commit_many(self, mutations, *, author, actor):  # type: ignore[no-untyped-def]
        del author, actor
        self.commits.extend(doc_id for doc_id, _mutate in mutations)
        return ()


class _HoldProbe:
    def __init__(self, lock: threading.RLock) -> None:
        self.lock = lock
        self._state_lock = threading.Lock()
        self._started: dict[int, list[float]] = {}
        self._holder_threads: dict[int, threading.Thread] = {}
        self.holds: list[float] = []
        self.waiters = 0
        self.holders = 0
        self.holder_alive = False
        self.max_waiters = 0
        self.max_holders = 0

    def snapshot(self) -> tuple[int, int, bool]:
        with self._state_lock:
            return self.waiters, self.holders, self.holder_alive

    def __enter__(self):
        with self._state_lock:
            self.waiters += 1
            self.max_waiters = max(self.max_waiters, self.waiters)
        self.lock.acquire()
        ident = threading.get_ident()
        with self._state_lock:
            self.waiters -= 1
            self._started.setdefault(ident, []).append(time.perf_counter())
            self._holder_threads[ident] = threading.current_thread()
            self.holders = len(self._holder_threads)
            self.max_holders = max(self.max_holders, self.holders)
            self.holder_alive = all(
                thread.is_alive() for thread in self._holder_threads.values()
            )
        return self

    def __exit__(self, exc_type, exc, traceback):  # type: ignore[no-untyped-def]
        del exc_type, exc, traceback
        ident = threading.get_ident()
        with self._state_lock:
            started = self._started[ident].pop()
            self.holds.append(time.perf_counter() - started)
            if not self._started[ident]:
                del self._started[ident]
                del self._holder_threads[ident]
            self.holders = len(self._holder_threads)
            self.holder_alive = bool(self._holder_threads) and all(
                thread.is_alive() for thread in self._holder_threads.values()
            )
        self.lock.release()


@pytest.mark.perf_bound
def test_identical_directory_documents_create_no_tree_commits() -> None:
    store = _CommitRecorder()
    facade = LocalOrgFs(stores=store, author="user:cost")
    try:
        space_id = facade.create_space("noop-writes").space_id
        documents = {
            "links/device.js": "export default { device: 'node-a' };\n",
            "directory/users.js": "export default ['user:owner'];\n",
            "directory/routes.js": "export default { hq: 'chat-1' };\n",
        }
        facade.mkdir(space_id, "links")
        facade.mkdir(space_id, "directory")
        for path, content in documents.items():
            facade.write_text(space_id, path, content)
        before = tuple(store.commits)
        for path, content in documents.items():
            facade.write_text(space_id, path, content)
        assert tuple(store.commits) == before
    finally:
        assert facade.close_effects()


@pytest.mark.perf_bound
def test_space_authority_lock_hold_stays_below_250ms_during_list_write_mix() -> None:
    store = _CommitRecorder()
    facade = LocalOrgFs(stores=store, author="user:cost")
    try:
        space_id = facade.create_space("lock-hold").space_id
        facade.write_text(space_id, "probe.md", "seed")
        original = facade._space_locks[space_id]  # noqa: SLF001
        probe = _HoldProbe(original)
        facade._space_locks[space_id] = probe  # type: ignore[assignment]  # noqa: SLF001

        finished = threading.Event()

        def write() -> None:
            try:
                for index in range(25):
                    facade.write_text(space_id, "probe.md", f"value-{index}")
            finally:
                finished.set()

        writer = threading.Thread(target=write, name="orgfs-lock-hold-probe")
        writer.start()
        while not finished.wait(0.001):
            facade.spaces()
            waiters, holders, holder_alive = probe.snapshot()
            assert waiters >= 0
            assert holders in {0, 1}
            assert holder_alive is bool(holders)
        writer.join(2)
        assert not writer.is_alive()
        assert probe.snapshot() == (0, 0, False)
        assert probe.max_holders == 1
        assert probe.max_waiters >= 1
        assert probe.holds
        assert max(probe.holds) < 0.250, max(probe.holds)
    finally:
        assert facade.close_effects()


def _store_with_content_history(path: Path, commits: int) -> LocalSpaceStore:
    store = LocalSpaceStore(path, "space-history", node_id="node-history")
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:cost"),
            doc["space"].__setitem__("spaceId", "space-history"),
            doc["space"].__setitem__("name", "cost"),
            doc["members"].__setitem__(
                "user:cost", Map({"mode": "rw", "removedAt": None})
            ),
            doc["docs"].__setitem__(
                "tree",
                Map(
                    {
                        "active": _HISTORY_TREE_ID,
                        "activatedAt": "genesis",
                        "planId": None,
                    }
                ),
            ),
        ),
        author="user:cost",
        actor=None,
    )

    def replace(value: str):
        def mutate(doc: Doc) -> None:
            text = doc.get("text", type=Text)
            del text[:]
            text += value

        return mutate

    store.commit(
        _HISTORY_DOC_ID,
        lambda doc: doc.__setitem__("text", Text("0")),
        author="user:cost",
        actor="agent:history",
    )
    for index in range(commits):
        store.commit(
            _HISTORY_DOC_ID,
            replace(str(index + 1)),
            author="user:cost",
            actor="agent:history",
        )
    return store


def _next_commit_baseline_bytes(
    store: LocalSpaceStore, monkeypatch: pytest.MonkeyPatch
) -> int:
    applied = 0
    original = Doc.apply_update

    def count_applied(self: Doc, update: bytes) -> None:
        nonlocal applied
        applied += len(update)
        original(self, update)

    monkeypatch.setattr(Doc, "apply_update", count_applied)
    store.commit(
        _HISTORY_DOC_ID,
        lambda doc: doc.get("text", type=Text).__iadd__("x"),
        author="user:cost",
        actor="agent:history",
    )
    monkeypatch.undo()
    return applied


def test_steady_state_commit_does_not_reapply_document_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = _store_with_content_history(tmp_path / "history-small", 50)
    large = _store_with_content_history(tmp_path / "history-large", 500)
    try:
        small_bytes = _next_commit_baseline_bytes(small, monkeypatch)
        large_bytes = _next_commit_baseline_bytes(large, monkeypatch)
        assert large_bytes <= small_bytes + 256, (small_bytes, large_bytes)
    finally:
        small.close()
        large.close()


def test_writes_since_the_last_checkpoint_survive_a_crash(tmp_path: Path) -> None:
    # State files are refreshed in batches; the journal alone must carry every
    # write in between.  Simulate a crash: close the journal without the
    # checkpoint close() would run, then reopen from disk.
    runtime, space_id = _space_with_files(tmp_path, 3)
    runtime.stores.get(space_id).checkpoint()
    runtime.facade.write_text(space_id, "probe.md", "after the checkpoint")
    runtime.facade.write_text(space_id, "late.md", "created after it")
    store = runtime.stores.get(space_id)
    with store._lock:  # noqa: SLF001
        store._db.close()  # noqa: SLF001
        store._db = None  # noqa: SLF001
    runtime.close()

    reopened = OrgFsRuntime(tmp_path, node_id="node-cost", author="user:cost")
    try:
        assert reopened.facade.read_text(space_id, "probe.md")[0] == "after the checkpoint"
        assert reopened.facade.read_text(space_id, "late.md")[0] == "created after it"
    finally:
        reopened.close()
