# Decisions (phase D, package T3, 2026-11-25):
# - The scale-invariance, no-op-rewrite and lock-hold tests build the new
#   ``OrgFsRuntime`` / ``LocalOrgFs`` directly (independent of
#   ``HYPRIAL_ORGFS_IMPL``).  The new store has no state-file checkpoint and no
#   per-version snapshot layer, so the write cost is the fsync count and the
#   bytes ``os.write`` puts on disk for one overwrite and one create, compared
#   between a small and a large space.
# - The no-op-rewrite test records commits through the facade's
#   ``store_factory`` seam: a rewrite with identical content commits nothing.
# - The checkpoint-crash test was deleted: the new store's durability across a
#   process exit is C1's ``state/test_state_crash.py``
#   ``test_a_real_process_exit_keeps_confirmed_writes_and_batch_atomicity`` and
#   the runtime's recovery is ``runtime_state/test_startup.py``.
# - Package S (2026-11-25): the two snapshot-layer tests and the journal
#   history test were deleted with the journal store and its per-version
#   snapshots.
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

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.document.coordinator import MemoryStores, LocalOrgFs


def _state_space_with_files(path: Path, count: int) -> tuple[OrgFsRuntime, str]:
    runtime = OrgFsRuntime(path, node_id="node-cost", author="user:cost")
    space_id = runtime.facade.create_space("cost").space_id
    for index in range(count):
        runtime.facade.write_text(space_id, f"file-{index}.md", "x" * 200)
    runtime.facade.write_text(space_id, "probe.md", "before")
    return runtime, space_id


def _write_cost(
    path: Path, monkeypatch: pytest.MonkeyPatch, files: int
) -> dict[str, tuple[int, int]]:
    runtime, space_id = _state_space_with_files(path, files)
    try:
        counts = {"fsync": 0, "bytes": 0}
        real_fsync = os.fsync
        real_write = os.write

        def counting_fsync(fd: int) -> None:
            counts["fsync"] += 1
            real_fsync(fd)

        def counting_write(fd: int, data) -> int:
            written = real_write(fd, data)
            counts["bytes"] += written
            return written

        monkeypatch.setattr(os, "fsync", counting_fsync)
        monkeypatch.setattr(os, "write", counting_write)
        cost: dict[str, tuple[int, int]] = {}
        for name, write in (
            ("overwrite", lambda: runtime.facade.write_text(space_id, "probe.md", "after")),
            ("create", lambda: runtime.facade.write_text(space_id, "new.md", "fresh")),
        ):
            counts.update(fsync=0, bytes=0)
            write()
            assert runtime.facade.drain_broadcasts((space_id,), timeout=5.0)
            cost[name] = (counts["fsync"], counts["bytes"])
        monkeypatch.undo()
        return cost
    finally:
        runtime.close()


def test_a_write_costs_the_same_in_a_small_and_a_large_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = _write_cost(tmp_path / "small", monkeypatch, 20)
    large = _write_cost(tmp_path / "large", monkeypatch, 200)

    for name in ("overwrite", "create"):
        small_fsyncs, small_bytes = small[name]
        large_fsyncs, large_bytes = large[name]
        assert small_fsyncs == large_fsyncs, (small, large)
        assert abs(large_bytes - small_bytes) <= 64, (small, large)
        assert large_bytes < 4096, (small, large)


class _CommitRecorder:
    """A ``store_factory`` that records the documents each commit appends to."""

    def __init__(self) -> None:
        self.commits: list[str] = []
        self._stores = MemoryStores()
        self.discard = self._stores.discard

    def __call__(self, space_id: str, create: bool) -> "_RecordingStore":
        return _RecordingStore(self._stores(space_id, create), self.commits)


class _RecordingStore:
    def __init__(self, inner, commits: list[str]) -> None:
        self._inner = inner
        self._commits = commits

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def commit(self, batch, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._commits.extend(doc_id for doc_id, _update in batch.appends)
        return self._inner.commit(batch, *args, **kwargs)


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
    facade = LocalOrgFs(author="user:cost", store_factory=store, store_discard=store.discard)
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
        assert before
        for path, content in documents.items():
            facade.write_text(space_id, path, content)
        assert tuple(store.commits) == before
    finally:
        assert facade.close_effects()


@pytest.mark.perf_bound
def test_space_authority_lock_hold_stays_below_250ms_during_list_write_mix() -> None:
    facade = LocalOrgFs(author="user:cost")
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
