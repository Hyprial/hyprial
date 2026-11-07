from __future__ import annotations

import base64
import json
from pycrdt import Doc, Map, Text

from ..support import (
    TREE_DOC_ID,
    _close_stores as _close_stores,
    _envelopes,
    _store,
)


def test_envelope_pinned_after_remove_is_rejected_without_journal_write(tmp_path):
    """review-p1 fixes 1 and 9: post-remove author is rejected without journaling."""
    owner = _store(tmp_path, "owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:removed", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    owner.commit(
        "meta",
        lambda doc: doc["members"]["user:removed"].__setitem__(
            "removedAt", "2026-09-26T00:00:00Z"
        ),
        author="user:owner",
        actor=None,
    )
    late = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("late", Text("no")),
        author="user:owner",
        actor="agent:owner",
    )
    value = json.loads(late.envelope_bytes)
    value["origin"]["author"] = "user:removed"
    injected = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    receiver = _store(tmp_path, "receiver", "node-receiver")
    for envelope in _envelopes(owner):
        if envelope != late.envelope_bytes:
            receiver.import_envelope(envelope, supplier="node-owner")
    before = receiver._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    result = receiver.import_envelope(injected, supplier="node-owner")
    assert result.status == "rejected"
    assert result.code == "removed-author"
    assert receiver._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == before


def test_fresh_key_payload_tamper_is_rejected_without_partial_apply(tmp_path):
    """review-p1-r2 R11: fresh-key update payload tamper leaves no journal or doc change."""

    source = _store(tmp_path, "r11-source", "node-owner")
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    valid = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("x", Text("x")),
        author="user:owner",
        actor="agent:x",
    )
    target = _store(tmp_path, "r11-target", "node-target")
    for envelope in _envelopes(source):
        if json.loads(envelope)["docId"] == "meta":
            target.import_envelope(envelope, supplier="node-owner")
    forged = json.loads(valid.envelope_bytes)
    forged["origin"]["writer"] += ".tampered"
    forged["seq"] = 77
    forged["update"] = base64.b64encode(b"not-a-yjs-update").decode()
    raw = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
    before = target._doc(TREE_DOC_ID).get_update()
    before_rows = target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    result = target.import_envelope(raw, supplier="node-owner")
    assert (result.status, result.code) == ("rejected", "invalid-argument")
    assert target._docs[TREE_DOC_ID].get_update() == before
    assert (
        target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == before_rows
    )


def test_writer_row_content_must_match_origin_and_actual_client_id(tmp_path):
    """review-p1-r2 H1: forged writer attribution content is rejected."""

    owner = _store(tmp_path, "h1-owner", "node-owner")
    genesis = owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    target = _store(tmp_path, "h1-target", "node-target")
    assert (
        target.import_envelope(genesis.envelope_bytes, supplier="node-owner").status
        == "applied"
    )
    writer = "node-owner.forged.lane-0"
    client_id = 424242
    probe = Doc(client_id=client_id)
    probe.apply_update(target._docs["meta"].get_update())
    before = probe.get_state()
    probe.get("writers", type=Map)[writer] = Map(
        {
            "node": "node-owner",
            "peerId": client_id,
            "peerIds": [client_id],
            "author": "user:attacker",
            "actor": None,
            "startedAt": "future",
        }
    )
    envelope = {
        "schemaVersion": 1,
        "type": "orgfs-update",
        "spaceId": "space-1",
        "docId": "meta",
        "seq": 0,
        "origin": {
            "writer": writer,
            "node": "node-owner",
            "author": "user:owner",
            "actor": None,
            "committedAt": "2026-09-26T00:00:00Z",
            "metaFrontier": base64.b64encode(
                target._project_frontier(target._docs["meta"], "user:owner")
            ).decode(),
        },
        "update": base64.b64encode(probe.get_update(before)).decode(),
    }
    before_rows = target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    result = target.import_envelope(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
        supplier="node-owner",
    )
    assert (result.status, result.code) == ("rejected", "not-a-member")
    assert (
        target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == before_rows
    )
