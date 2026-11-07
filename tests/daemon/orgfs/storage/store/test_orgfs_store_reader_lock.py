"""Store readers reachable from other threads hold the store lock.

pycrdt's ``Doc.transaction()`` hands a caller the doc's currently open
transaction, and that transaction is bound to the thread that opened it.  A
reader on another thread that enters while a writer's transaction is open
therefore trips pyo3's "Transaction is unsendable, but sent to another thread"
panic (seen from the zenoh blob-serve path: mesh._handle_blob ->
BlobStore.get -> purge checker -> store.purge_listed).  Writers hold
``LocalSpaceStore._lock``; these readers must hold it too.

Each case opens a meta transaction on thread A while holding the store lock,
then calls the reader on thread B.  Without the lock the reader panics inside
A's transaction; with it the reader waits for A and returns normally.
"""

from __future__ import annotations

import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore

READERS = {
    "purge_listed": lambda store: store.purge_listed("0" * 64),
    "snapshot_point": lambda store: store.snapshot_point("meta"),
    "retired": lambda store: store.retired("meta"),
    "active_tree_doc_id": lambda store: store.active_tree_doc_id(),
}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_reader_waits_for_an_open_meta_transaction(tmp_path, reader) -> None:
    store = LocalSpaceStore(tmp_path / "store", "space-1", node_id="node-a")
    opened, release = threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def writer() -> None:
        with store._lock, store._docs["meta"].transaction():
            opened.set()
            release.wait(5)
            outcome["released_at"] = time.monotonic()

    def read() -> None:
        try:
            outcome["value"] = READERS[reader](store)
        except BaseException as exc:  # pyo3 PanicException is a BaseException
            outcome["error"] = f"{type(exc).__module__}.{type(exc).__name__}: {exc}"
        outcome["returned_at"] = time.monotonic()

    holder = threading.Thread(target=writer)
    holder.start()
    try:
        assert opened.wait(5)
        caller = threading.Thread(target=read)
        caller.start()
        # Give the reader time to either panic or block on the lock.
        caller.join(0.3)
        release.set()
        caller.join(5)
        holder.join(5)
        assert not caller.is_alive() and not holder.is_alive()
        assert "error" not in outcome, (
            f"{reader} raised on another thread during an open meta "
            f"transaction: {outcome['error']}"
        )
        assert outcome["returned_at"] >= outcome["released_at"]
    finally:
        release.set()
        holder.join(5)
        store.close()
