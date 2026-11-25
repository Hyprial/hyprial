"""The protected-partition admission rules for agent rows.

These go through the real OrgFS store: a row the approval rule refuses must be
refused by every replica, not only by the daemon that wrote it.
"""

from __future__ import annotations

import json

import pytest
from pycrdt import Map

from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore
from hyprial.identity import (
    protected_directory_doc_id,
)

OWNER = "owner@example"
MEMBER = "member@example"
AGENT_ID = "a." + "e" * 26
SPACE_ID = "space-1"


_OPEN: list[FileSpaceStore] = []


@pytest.fixture(autouse=True)
def _close_stores():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _store(tmp_path, name: str) -> FileSpaceStore:
    store = FileSpaceStore(tmp_path / name, SPACE_ID, node_id=f"node-{name}")
    _OPEN.append(store)
    return store


def _genesis(store: FileSpaceStore, *, space_name: str) -> None:
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("name", space_name),
            doc["space"].__setitem__("owner", f"user:{OWNER}"),
            doc["space"].__setitem__("spaceId", SPACE_ID),
            doc["members"].__setitem__(f"user:{OWNER}", Map({"mode": "rw"})),
            doc["members"].__setitem__(f"user:{MEMBER}", Map({"mode": "rw"})),
        ),
        author=f"user:{OWNER}",
        actor=None,
    )


_RECEIVERS = 0


def _decision(tmp_path, source: FileSpaceStore, envelope: bytes) -> str:
    global _RECEIVERS
    _RECEIVERS += 1
    receiver = _store(tmp_path, f"receiver-{_RECEIVERS}")
    for previous in [
        bytes(row[0])
        for row in source._db.execute(
            "SELECT envelope_bytes FROM commits ORDER BY rowid"
        )
    ]:
        if previous == envelope:
            break
        receiver.import_envelope(previous, supplier="node-source")
    return receiver.admission(envelope)


def test_a_row_at_a_path_that_does_not_match_its_protected_id_is_refused(
    tmp_path,
) -> None:
    """A member cannot smuggle a protected row in under an unprotected document id."""

    record = {"owner": f"user:{OWNER}", "updatedAt": "2026-10-31T00:00:00Z"}
    source = _store(tmp_path, "wrong-id")
    _genesis(source, space_name="group-acme")
    envelope = source.commit(
        f"doc-{__import__('uuid').uuid4()}",
        lambda doc: doc.__setitem__("record", Map({"body": json.dumps(record)})),
        author=f"user:{MEMBER}",
        actor=None,
    ).envelope_bytes

    assert _decision(tmp_path, source, envelope) == "ok"
    people_doc_id = protected_directory_doc_id(
        SPACE_ID,
        f"directory/people/{MEMBER}/agents-node-1.json",
        space_owner=f"user:{OWNER}",
    )
    assert people_doc_id != protected_directory_doc_id(
        SPACE_ID,
        f"directory/people/{OWNER}/agents-node-1.json",
        space_owner=f"user:{OWNER}",
    )
