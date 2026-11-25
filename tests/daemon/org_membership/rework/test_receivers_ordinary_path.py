# Decision record (S3a red tests R2; no inline comments by repository rule):
# - receivers.json stops being a protected section: protected_section_for_path
#   no longer names it and the identity vocabulary has no Receivers record or
#   receivers_path.  Checks go through getattr so a missing symbol is an
#   assertion about the surface, not an ImportError.
# - A replica that already holds a receivers.json written by an older build is
#   replicated like any text file.  The old register format is not reproduced
#   here (the register code is deleted; an old register document reads as its
#   plain text part, see the migration guide).  Nothing rewrites or deletes
#   the document on sync.
# - Stores written by older builds keep documents under the legacy receivers
#   protected document id.  LEGACY_DOC_ID is that id for LEGACY_SPACE and
#   LEGACY_OWNER, printed by protected_directory_doc_id on a9252d78e5 (before
#   S3a); such a store must still reopen and replicate.  This node refuses to
#   author that document (test_legacy_receivers_guard.py), so the fixture lifts
#   the local guard only while it writes the older build's commits; reopen and
#   import run under the live guard.
"""receivers.json is an ordinary OrgFS path."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

from pycrdt import Map

from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.document.operations import spaces
from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.admission import StoreAdmission
from hyprial.identity import OrgMembership
from hyprial.identity.impl.org_directory.models import protected_section_for_path

OWNER = "owner@example"
SPACE = "00000000-0000-0000-0000-000000000002"
OLD_TEXT = '{"squire": "agent:owner@example:node:squire"}\nnot a receiver record'
LEGACY_SPACE = "00000000-0000-0000-0000-0000000000aa"
LEGACY_OWNER = "user:owner@example"
LEGACY_DOC_ID = (
    "doc-orgdir-v3-gm3dumbqgaydambqgawtambqgawtambqgawtambqgawtambqgaydambqgaygcyjzhjzgky3fnf3gk4ttge4du5ltmvzdu33xnzsxeqdfpbqw24dmmuytiotsmvrwk2lwmvzhgltkonxw4"
)


def test_the_path_has_no_protected_section() -> None:
    assert protected_section_for_path("receivers.json") is None
    assert protected_section_for_path("receivers.json", space_owner=OWNER) is None


def test_the_identity_vocabulary_has_no_receivers_record() -> None:
    assert getattr(OrgMembership, "Receivers", None) is None
    assert getattr(OrgMembership, "receivers_path", None) is None


def test_a_replica_reads_old_receivers_text_and_nothing_is_rewritten(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        spaces, "uuid", SimpleNamespace(uuid4=lambda: UUID(SPACE), UUID=UUID)
    )
    source = FileSpaceStore(tmp_path / "source", SPACE, node_id="source")
    target = FileSpaceStore(tmp_path / "target", SPACE, node_id="target")
    fs = LocalOrgFs(stores=source, author="user:" + OWNER)
    replica = LocalOrgFs(stores=target, author="user:" + OWNER)
    try:
        space = fs.create_space("group-work")
        fs.write_text(space.space_id, "receivers.json", OLD_TEXT)
        envelopes = [
            bytes(row[0])
            for row in source._db.execute(
                "SELECT envelope_bytes FROM commits ORDER BY rowid"
            )
        ]
        for envelope in envelopes:
            assert target.admission(envelope) == "ok"
            target.import_envelope(envelope, supplier="source")
        before = target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        replica.load_space(SPACE)
        content, _version = replica.read_text(SPACE, "receivers.json")
        assert content == OLD_TEXT
        replica.load_space(SPACE)
        after = target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        assert after == before == len(envelopes)
        assert json.loads(envelopes[0])["docId"]
    finally:
        fs.close_effects()
        replica.close_effects()
        source.close()
        target.close()


def _commit_legacy_space(store: FileSpaceStore) -> None:
    entry = json.dumps({"revision": 1, "writer": "node-a", "parent": None, "value": "{}"})
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("name", "personal-owner"),
            doc["space"].__setitem__("owner", LEGACY_OWNER),
            doc["space"].__setitem__("spaceId", LEGACY_SPACE),
            doc["members"].__setitem__(LEGACY_OWNER, Map({"mode": "rw"})),
        ),
        author=LEGACY_OWNER,
        actor=None,
    )
    store.commit(
        LEGACY_DOC_ID,
        lambda doc: doc.__setitem__("__receiver_versions__", Map({"e1": entry})),
        author=LEGACY_OWNER,
        actor=None,
    )


def test_a_store_holding_a_legacy_receivers_document_reopens_and_replicates(
    tmp_path, monkeypatch
) -> None:
    source = FileSpaceStore(tmp_path / "source", LEGACY_SPACE, node_id="node-a")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(StoreAdmission, "_org_device_update_allowed", lambda self, *a, **k: True)
            _commit_legacy_space(source)
    finally:
        source.close()

    reopened = FileSpaceStore(tmp_path / "source", LEGACY_SPACE, node_id="node-a")
    target = FileSpaceStore(tmp_path / "target", LEGACY_SPACE, node_id="node-b")
    try:
        envelopes = [
            bytes(row[0])
            for row in reopened._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid")
        ]
        assert len(envelopes) == 2
        for envelope in envelopes:
            assert target.import_envelope(envelope, supplier="node-a").status == "applied"
        assert target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 2
    finally:
        reopened.close()
        target.close()
