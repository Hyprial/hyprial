"""Fixtures for the new-implementation scaling contract (C6).

Decisions (C6, 2026-11-25):

* 2026-11-25 (closing): params are ``reference`` and ``state`` (C1's
  ``FileStateStore.open(space_dir, create=True)``); space directories are UUID
  named via ``space_path``; ``Batch`` is C1's.
* ``state_store_factory`` is the one seam.  A factory is
  ``factory(space_dir: Path) -> StateStore``.  Today the only parameter is the
  reference store; adding C1's store is one more entry in ``_FACTORIES``
  (``pytest.param("state", lambda space_dir: <C1 StateStore>(space_dir))``).
  ``ORGFS_SCALING_DEFECT`` picks a deliberately wrong reference store so the
  tests can be seen failing for the right reason; it is unset in normal runs.
* Spaces are populated with ``fsync`` disabled (setup only) and measured with
  ``FsProbe`` active; assertions compare counts and bytes across N, never time.
* ``ORGFS_SCALING_SIZES`` (comma list) overrides the N tiers, default
  ``100,1000,10000``.
"""

from __future__ import annotations

import base64
import uuid
import json
import os
from pathlib import Path
import struct
import binascii
from typing import Callable, Iterable

import pytest

from .fsprobe import FsProbe, fsync_disabled
from hyprial.daemon.impl.orgfs.state import Batch

from .reference_store import BATCH_MAGIC, ReferenceStateStore
from .synthetic import content_updates, synthetic_meta_doc, synthetic_tree_doc

StoreFactory = Callable[[Path], ReferenceStateStore]

_DEFECT = os.environ.get("ORGFS_SCALING_DEFECT") or None


def _reference(space_dir: Path) -> ReferenceStateStore:
    return ReferenceStateStore(space_dir, defect=_DEFECT)


def _state(space_dir: Path):
    from hyprial.daemon.impl.orgfs.state import FileStateStore

    return FileStateStore.open(space_dir, create=True)


_FACTORIES = [
    pytest.param(_reference, id="reference"),
    pytest.param(_state, id="state"),
]


def space_path(base: Path, name: str) -> Path:
    """A space directory named by a UUID (the real store requires it) under ``base/name``."""

    path = base / name / str(uuid.uuid5(uuid.NAMESPACE_URL, name))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(params=_FACTORIES)
def state_store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    return request.param


@pytest.fixture
def sizes() -> tuple[int, ...]:
    raw = os.environ.get("ORGFS_SCALING_SIZES", "100,1000,10000")
    return tuple(int(part) for part in raw.split(",") if part)


def build_space(
    factory: StoreFactory,
    space_dir: Path,
    documents: int,
    *,
    tree_rows: int = 40,
) -> list[str]:
    """Create a space with meta, a small tree and ``documents`` content documents."""

    ids: list[str] = []
    with fsync_disabled():
        store = factory(space_dir)
        tree = synthetic_tree_doc(tree_rows)
        store.commit(Batch(appends=(("tree", tree.get_update()),)))
        store.commit(Batch(appends=(("meta", synthetic_meta_doc().get_update()),)))
        for doc_id, update in content_updates(documents):
            store.commit(Batch(appends=((doc_id, update),)))
            ids.append(doc_id)
        store.close()
    return ids


@pytest.fixture
def probe_for() -> Callable[[Path], FsProbe]:
    return FsProbe


def write_pending_batch(
    space_dir: Path,
    appends: Iterable[tuple[str, bytes]],
    deletes: Iterable[str] = (),
    *,
    torn: bool = False,
) -> None:
    """Write ``batch.pending`` in the §2.2 format, as a crashed commit would leave it."""

    body = json.dumps(
        {
            "appends": [[doc, base64.b64encode(blob).decode("ascii")] for doc, blob in appends],
            "deletes": list(deletes),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    crc = binascii.crc32(body) & 0xFFFFFFFF
    if torn:
        crc ^= 1
    (space_dir / "batch.pending").write_bytes(BATCH_MAGIC + struct.pack(">II", len(body), crc) + body)


def assert_same_cost(costs: list[dict[str, object]], *, slack_bytes: int = 16) -> None:
    """Counts must be equal; byte totals may differ by ``slack_bytes`` (varint widths of ids)."""

    first = costs[0]
    for cost in costs[1:]:
        for key, value in first.items():
            if key.startswith("bytes_"):
                assert abs(int(cost[key]) - int(value)) <= slack_bytes, (key, costs)
            else:
                assert cost[key] == value, (key, costs)
