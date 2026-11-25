# Decision record (S3a fix, coordinator ruling P1-2; no inline comments by
# repository rule):
# - The legacy receivers protected ids are decode-only and read-only.  A store
#   written by an older build is rebuilt here by letting the projection
#   produce the pre-S3a ids for receivers.json (the formula of a9252d78e5,
#   checked against LEGACY_DOC_ID) with admission lifted, exactly what that
#   build committed.  The guard is then live for every operation.
# - This node never authors a write, removal or move of the legacy document or
#   tree row, in any space.  A peer's such update is refused in an
#   org-directory space (group-*, the synthetic column of the S3a critic
#   table).  In a leftover personal-* space, where these documents really
#   exist, peer history keeps replicating: refusing it would strand later
#   commits of the same writer on a fresh replica.
"""Writes to the legacy receivers document or tree row are refused everywhere."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.document import projection
from hyprial.daemon.impl.orgfs.document.operations import spaces
from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.admission import StoreAdmission
from hyprial.identity import legacy_receivers_node_id
from hyprial.identity.impl.org_directory import models
from tests.daemon.org_membership.rework.test_receivers_ordinary_path import (
    LEGACY_DOC_ID,
    LEGACY_OWNER,
    LEGACY_SPACE,
)

AUTHOR = LEGACY_OWNER
SPACE = "00000000-0000-0000-0000-0000000000ab"
OLD_TEXT = '{"preferred": {"machine": "node-a"}}'


def _encoded(prefix: str, *values: str) -> str:
    payload = "".join(f"{len(value)}:{value}" for value in values).encode("utf-8")
    return prefix + models._encode_base32(payload)


def legacy_doc_id(space_id: str, owner: str) -> str:
    principal = models.directory_owner_principal(owner)
    return _encoded(models._PROTECTED_DOC_ID_PREFIX, space_id, "receivers", principal, "receivers.json")


def legacy_node_id(space_id: str, owner: str) -> str:
    principal = models.directory_owner_principal(owner)
    return _encoded(models._PROTECTED_NODE_ID_PREFIX, space_id, principal, "receivers.json")


def _content_commits(store: FileSpaceStore) -> int:
    return store._db.execute("SELECT COUNT(*) FROM commits WHERE doc_id != 'meta'").fetchone()[0]


def _envelopes(store: FileSpaceStore) -> list[bytes]:
    return [bytes(row[0]) for row in store._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid")]


def _older_build(patch) -> None:
    doc_ids = projection.protected_directory_doc_id
    node_ids = projection.protected_directory_node_id
    org_name = projection.org_from_space_name

    def doc_id(space_id, path, *, space_owner=None):
        if path == "receivers.json":
            return legacy_doc_id(space_id, space_owner)
        return doc_ids(space_id, path, space_owner=space_owner)

    def node_id(space_id, path, *, space_owner):
        if path == "receivers.json":
            return legacy_node_id(space_id, space_owner)
        return node_ids(space_id, path, space_owner=space_owner)

    patch.setattr(projection, "protected_directory_doc_id", doc_id)
    patch.setattr(projection, "protected_directory_node_id", node_id)
    patch.setattr(projection, "org_from_space_name",
                  lambda name: org_name(name) or (name if name.startswith("personal-") else None))
    patch.setattr(StoreAdmission, "_org_device_update_allowed", lambda self, *a, **k: True)


def _build_legacy_store(name, tmp_path, monkeypatch, *, older_build=True):
    monkeypatch.setattr(spaces, "uuid", SimpleNamespace(uuid4=lambda: UUID(SPACE), UUID=UUID))
    source = FileSpaceStore(tmp_path / "source", SPACE, node_id="node-a")
    fs = LocalOrgFs(stores=source, author=AUTHOR)
    try:
        with monkeypatch.context() as patch:
            if older_build:
                _older_build(patch)
            space = fs.create_space(name)
            fs.write_text(space.space_id, "receivers.json", OLD_TEXT)
    finally:
        fs.close_effects()
        source.close()
    return SimpleNamespace(root=tmp_path, name=name, monkeypatch=monkeypatch)


@pytest.fixture(params=["personal-owner", "group-work"])
def legacy_store(request, tmp_path, monkeypatch):
    return _build_legacy_store(request.param, tmp_path, monkeypatch)


def _reopen(root, name="source", node="node-a"):
    store = FileSpaceStore(root / name, SPACE, node_id=node)
    fs = LocalOrgFs(stores=store, author=AUTHOR)
    fs.load_space(SPACE)
    return store, fs


def test_the_legacy_formula_matches_the_pre_s3a_document_id() -> None:
    assert legacy_doc_id(LEGACY_SPACE, LEGACY_OWNER) == LEGACY_DOC_ID


def test_the_public_legacy_node_id_matches_the_pre_s3a_formula() -> None:
    assert legacy_receivers_node_id(SPACE, AUTHOR) == legacy_node_id(SPACE, AUTHOR)
    assert legacy_receivers_node_id(LEGACY_SPACE, LEGACY_OWNER) == legacy_node_id(
        LEGACY_SPACE, LEGACY_OWNER)


def _count_probes(monkeypatch) -> list[int]:
    calls = [0]
    probe = StoreAdmission._tree_touches_legacy_receivers

    def counted(self, baseline, update):
        calls[0] += 1
        return probe(self, baseline, update)

    monkeypatch.setattr(StoreAdmission, "_tree_touches_legacy_receivers", counted)
    return calls


def test_this_build_never_writes_receivers_json_under_the_legacy_node_id(
    tmp_path, monkeypatch
) -> None:
    built = _build_legacy_store("personal-owner", tmp_path, monkeypatch, older_build=False)
    store, fs = _reopen(built.root)
    try:
        nodes = fs.resolve(SPACE, "receivers.json")
        assert len(nodes) == 1
        assert nodes[0].node_id != legacy_receivers_node_id(SPACE, AUTHOR)
        assert fs.read_text(SPACE, "receivers.json")[0] == OLD_TEXT
    finally:
        fs.close_effects()
        store.close()


def test_the_tree_probe_is_skipped_locally_without_a_legacy_row(
    tmp_path, monkeypatch
) -> None:
    built = _build_legacy_store("personal-owner", tmp_path, monkeypatch, older_build=False)
    calls = _count_probes(monkeypatch)
    store, fs = _reopen(built.root)
    try:
        fs.write_text(SPACE, "notes.txt", "one")
        fs.write_text(SPACE, "receivers.json", "{}")
        fs.move(SPACE, "notes.txt", "moved.txt")
        fs.remove(SPACE, "moved.txt")
        assert calls[0] == 0
    finally:
        fs.close_effects()
        store.close()


def test_the_tree_probe_still_runs_and_refuses_in_a_legacy_personal_store(
    tmp_path, monkeypatch
) -> None:
    built = _build_legacy_store("personal-owner", tmp_path, monkeypatch)
    calls = _count_probes(monkeypatch)
    store, fs = _reopen(built.root)
    try:
        fs.write_text(SPACE, "notes.txt", "one")
        assert calls[0] > 0
        before = calls[0]
        for operation in ("remove", "move"):
            with pytest.raises(OrgFsError):
                _operate(fs, operation)
        assert calls[0] > before
        assert [node.node_id for node in fs.resolve(SPACE, "receivers.json")] == [
            legacy_node_id(SPACE, AUTHOR)]
    finally:
        fs.close_effects()
        store.close()


def test_the_rebuilt_store_holds_the_legacy_ids(legacy_store) -> None:
    store, fs = _reopen(legacy_store.root)
    try:
        nodes = fs.resolve(SPACE, "receivers.json")
        assert [node.node_id for node in nodes] == [legacy_node_id(SPACE, AUTHOR)]
        doc_ids = {row[0] for row in store._db.execute("SELECT DISTINCT doc_id FROM commits")}
        assert legacy_doc_id(SPACE, AUTHOR) in doc_ids
        fs.read_text(SPACE, "receivers.json")
    finally:
        fs.close_effects()
        store.close()


@pytest.mark.parametrize("operation", ["write_text", "write_bytes", "remove", "move"])
def test_every_write_to_the_legacy_document_is_refused_locally(legacy_store, operation) -> None:
    store, fs = _reopen(legacy_store.root)
    try:
        before = _content_commits(store)
        with pytest.raises(OrgFsError):
            _operate(fs, operation)
        assert _content_commits(store) == before
        assert [node.node_id for node in fs.resolve(SPACE, "receivers.json")] == [
            legacy_node_id(SPACE, AUTHOR)]
    finally:
        fs.close_effects()
        store.close()


def _operate(fs, operation) -> None:
    if operation == "write_text":
        fs.write_text(SPACE, "receivers.json", "{}")
    elif operation == "write_bytes":
        fs.write_bytes(SPACE, "receivers.json", b"replacement")
    elif operation == "remove":
        fs.remove(SPACE, "receivers.json")
    else:
        fs.move(SPACE, "receivers.json", "moved.json")


@pytest.mark.parametrize("operation", ["write_text", "write_bytes", "remove", "move"])
def test_a_peer_write_to_the_legacy_document(legacy_store, operation) -> None:
    store, fs = _reopen(legacy_store.root)
    target = FileSpaceStore(legacy_store.root / "target", SPACE, node_id="node-b")
    try:
        with legacy_store.monkeypatch.context() as patch:
            _older_build(patch)
            for envelope in _envelopes(store):
                assert target.import_envelope(envelope, supplier="node-a").status == "applied"
            known = len(_envelopes(store))
            if legacy_store.name.startswith("group-") and operation == "move":
                with pytest.raises(OrgFsError):
                    _operate(fs, operation)
                return
            _operate(fs, operation)
        fresh = _envelopes(store)[known:]
        assert fresh
        decisions = []
        for envelope in fresh:
            decisions.append((json.loads(envelope)["docId"], target.admission(envelope)))
            target.import_envelope(envelope, supplier="node-a")
        if legacy_store.name.startswith("group-"):
            refused = [doc_id for doc_id, decision in decisions if decision == "not-a-member"]
            assert refused, decisions
            assert all(doc_id == legacy_doc_id(SPACE, AUTHOR) or doc_id.startswith("tree-")
                       for doc_id in refused)
        else:
            assert all(decision == "ok" for _doc_id, decision in decisions), decisions
    finally:
        fs.close_effects()
        store.close()
        target.close()


def test_personal_legacy_history_replicates_with_later_commits_of_the_writer(
    tmp_path, monkeypatch
) -> None:
    built = _build_legacy_store("personal-owner", tmp_path, monkeypatch)
    store, fs = _reopen(built.root)
    try:
        fs.write_text(SPACE, "notes.txt", "after the legacy document")
    finally:
        fs.close_effects()
        store.close()
    store, _fs = _reopen(built.root)
    target = FileSpaceStore(tmp_path / "target", SPACE, node_id="node-b")
    try:
        results = [target.import_envelope(envelope, supplier="node-a") for envelope in _envelopes(store)]
        assert all(result.status == "applied" for result in results), results
    finally:
        _fs.close_effects()
        store.close()
        target.close()
    replica_store, replica = _reopen(tmp_path, "target", "node-b")
    try:
        assert replica.read_text(SPACE, "notes.txt")[0] == "after the legacy document"
        assert [node.node_id for node in replica.resolve(SPACE, "receivers.json")] == [
            legacy_node_id(SPACE, AUTHOR)]
    finally:
        replica.close_effects()
        replica_store.close()
