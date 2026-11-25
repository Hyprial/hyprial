"""The state runtime opens a 25,000 document space without touching ``docs/``.

2026-11-25 (D2, brief item 6): the store-level tests in this package measure
``FileStateStore`` alone; this one measures the real assembly reachable from
the runtime (``OrgFsRuntime`` -> ``LocalOrgFs.load_space`` ->
``FileStateStore``).  The space is written with the C6 synthetic generators
(a 50,000 row tree, 25,000 content files) under a runtime state directory.
Asserted: constructing and closing the runtime opens no file under ``docs/``
and lists no directory under it, and the space and its rows are visible.
Wall time is recorded only.  ``ORGFS_SCALING_LARGE_DOCS`` shrinks the space.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
import uuid

from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.orgfs.state import Batch, FileStateStore
from pycrdt import Map

from ..fsprobe import FsProbe, fsync_disabled
from ..synthetic import AUTHOR, content_updates, synthetic_meta_doc, synthetic_tree_doc


def _build(state_dir: Path, documents: int) -> str:
    space_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"runtime-open-{documents}"))
    space_dir = state_dir / "orgfs" / "spaces" / space_id
    meta = synthetic_meta_doc()
    with meta.transaction():
        root = meta.get("space", type=Map)
        root["spaceId"] = space_id
        root["name"] = "large"
        root["createdAt"] = "2026-11-25T00:00:00Z"
    with fsync_disabled():
        store = FileStateStore.open(space_dir, create=True)
        store.commit(Batch(appends=(("meta", meta.get_update()),)))
        store.commit(Batch(appends=(("tree", synthetic_tree_doc(documents * 2).get_update()),)))
        for doc_id, update in content_updates(documents):
            store.commit(Batch(appends=((doc_id, update),)))
        store.close()
    return space_id


def test_the_runtime_opens_a_large_space_without_reading_any_content_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    documents = int(os.environ.get("ORGFS_SCALING_LARGE_DOCS", "25000"))
    state_dir = tmp_path / "node"
    space_id = _build(state_dir, documents)
    space_dir = state_dir / "orgfs" / "spaces" / space_id
    assert len(list((space_dir / "docs").iterdir())) >= documents

    probe = FsProbe(space_dir)
    started = time.perf_counter()
    with probe.active():
        runtime = OrgFsRuntime(state_dir, node_id="node-a", author=AUTHOR)
        try:
            opened = time.perf_counter() - started
            spaces = runtime.facade.spaces()
            assert [info.space_id for info in spaces] == [space_id]
            assert runtime.facade.space_meta(space_id)["name"] == "large"
        finally:
            runtime.close()

    content_opens = sorted(path for path in probe.files_opened if path.startswith("docs"))
    content_listings = [path for path in probe.listings if path.startswith("docs")]
    assert content_opens == [], content_opens[:5]
    assert content_listings == []
    assert probe.bytes_written == 0, probe.write_bytes
    print(f"runtime open of {documents} documents: {opened * 1000:.0f} ms")
