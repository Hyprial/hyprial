from __future__ import annotations

import base64
import json
import pytest
from pycrdt import Doc, Map, Text
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.store import _decode_state_vector, _encode_state_vector

from ..support import (
    TREE_DOC_ID,
    _close_stores as _close_stores,
    _envelopes,
    _store,
)


def test_meta_ack_waits_for_earlier_writer_registration_envelope(tmp_path):
    """A reordered ACK is parked until its writer row can be validated."""
    owner = _store(tmp_path, "owner-ordered-ack", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:member", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    owner.commit(
        "meta",
        lambda doc: doc["purgePlans"].__setitem__("plan-1", Map({})),
        author="user:owner",
        actor=None,
    )
    owner.commit(
        "meta",
        lambda doc: doc["purgeAcks"].__setitem__(
            "node-owner",
            Map({"plan-1": Map({"ackedAt": "2026-09-27T12:00:00Z"})}),
        ),
        author="user:owner",
        actor=None,
    )
    member = _store(tmp_path, "member-ordered-ack", "node-member")
    receiver = _store(tmp_path, "receiver-ordered-ack", "node-owner")
    for envelope in _envelopes(owner):
        member.import_envelope(envelope, supplier="node-owner")
        receiver.import_envelope(envelope, supplier="node-owner")

    registration = member.commit(
        "meta", lambda _doc: None, author="user:member", actor=None
    )
    other_lane_registration = member.commit(
        "meta", lambda _doc: None, author="user:member", actor="node-member"
    )
    acknowledgement = member.commit(
        "meta",
        lambda doc: doc["purgeAcks"].__setitem__(
            "node-member",
            Map({"plan-1": Map({"ackedAt": "2026-09-27T12:00:00Z"})}),
        ),
        author="user:member",
        actor=None,
    )
    assert registration.seq == 0
    assert other_lane_registration.seq == 0
    assert acknowledgement.seq == 1

    parked = receiver.import_envelope(
        acknowledgement.envelope_bytes, supplier="node-member"
    )
    assert (parked.status, parked.code) == ("pending", "pending-dependency")
    accepted = receiver.import_envelope(
        registration.envelope_bytes, supplier="node-member"
    )
    assert accepted.status == "applied"
    assert receiver._db.execute(
        "SELECT 1 FROM pending WHERE writer = ? AND seq = ? AND doc_id = 'meta'",
        (acknowledgement.writer, acknowledgement.seq),
    ).fetchone() is not None
    accepted_other_lane = receiver.import_envelope(
        other_lane_registration.envelope_bytes, supplier="node-member"
    )
    assert accepted_other_lane.status == "applied"
    assert acknowledgement.envelope_bytes in (accepted.details or {}).get(
        "drained", ()
    ) or acknowledgement.envelope_bytes in (accepted_other_lane.details or {}).get(
        "drained", ()
    )
    assert (
        receiver._docs["meta"].get("purgeAcks", type=Map)["node-member"].to_py()[
            "plan-1"
        ]["ackedAt"]
        == "2026-09-27T12:00:00Z"
    )


def test_idle_runtime_tick_expires_pending_without_an_import(
    tmp_path, request, caplog
):
    """The maintenance tick cleans an idle space without a drain trigger."""

    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    runtime = OrgFsRuntime(
        tmp_path / "idle-runtime", node_id="node-receiver", author="user:owner"
    )
    request.addfinalizer(runtime.close)
    info = runtime.facade.create_space("group-idle")
    store = runtime.stores.get(info.space_id)
    store._db.execute(
        "INSERT INTO pending(writer, seq, doc_id, author, meta_frontier, "
        "envelope_bytes, supplier, held_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "stale-writer",
            0,
            runtime.facade._spaces[info.space_id].tree_doc_id,
            "user:member",
            _encode_state_vector({999: 1}),
            b"{}",
            "node-supplier",
            "2000-01-01T00:00:00+00:00",
        ),
    )
    store._db.commit()
    runtime._directory_sync_due = 0.0

    with caplog.at_level("WARNING"):
        runtime.anti_entropy_tick(1.0)

    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    event = next(
        record.message
        for record in caplog.records
        if "orgfs.pending_expired" in record.message
    )
    assert "org=group-idle" in event
    assert f"space={info.space_id}" in event
    assert "count=1" in event
    assert "oldestHeldAt=2000-01-01T00:00:00+00:00" in event
    assert "reason=age-after-revalidation" in event


def test_expiry_revalidates_an_admissible_member_envelope_before_discarding(
    tmp_path, caplog
):
    owner = _store(tmp_path, "expiry-owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:member", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    member = _store(tmp_path, "expiry-member", "node-member")
    receiver = _store(tmp_path, "expiry-receiver", "node-receiver")
    for envelope in _envelopes(owner):
        assert member.import_envelope(envelope, supplier="node-owner").status == "applied"
    member.commit("meta", lambda _doc: None, author="user:member", actor=None)
    write = member.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("survived-expiry", Text("accepted")),
        author="user:member",
        actor=None,
    )
    for envelope in _envelopes(member):
        if json.loads(envelope)["docId"] == "meta":
            receiver.import_envelope(envelope, supplier="node-member")

    value = json.loads(write.envelope_bytes)
    value["origin"]["metaFrontier"] = base64.b64encode(
        member._docs["meta"].get_state()
    ).decode()
    envelope = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    receiver._db.execute(
        "INSERT INTO pending(writer, seq, doc_id, author, meta_frontier, "
        "envelope_bytes, supplier, held_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            value["origin"]["writer"],
            value["seq"],
            value["docId"],
            value["origin"]["author"],
            base64.b64decode(value["origin"]["metaFrontier"]),
            envelope,
            "node-member",
            "2000-01-01T00:00:00+00:00",
        ),
    )
    receiver._db.commit()

    with caplog.at_level("WARNING"):
        receiver.expire_pending()

    assert receiver._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    event = next(
        record.message
        for record in caplog.records
        if "orgfs.pending_expired" in record.message
    )
    assert "admittedCount=1" in event
    assert "discardedCount=0" in event
    assert (
        receiver._docs[TREE_DOC_ID].get("survived-expiry", type=Text).to_py()
        == "accepted"
    )


def test_pending_meta_is_retried_after_meta_arrives(tmp_path):
    """review-p1 fix 14: imported owner metadata drains pending envelopes."""
    owner = _store(tmp_path, "owner", "node-owner")
    genesis = owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    write = owner.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("x", Text("x")),
        author="user:owner",
        actor="agent:x",
    )
    receiver = _store(tmp_path, "receiver", "node-receiver")
    assert (
        receiver.import_envelope(write.envelope_bytes, supplier="node-owner").status
        == "pending"
    )
    assert (
        receiver.import_envelope(genesis.envelope_bytes, supplier="node-owner").status
        == "applied"
    )
    registration = next(
        bytes(row[0])
        for row in owner._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id='meta' ORDER BY rowid"
        )
        if bytes(row[0]) != genesis.envelope_bytes
    )
    assert (
        receiver.import_envelope(registration, supplier="node-owner").status
        == "applied"
    )
    assert receiver._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    assert receiver.frontier(TREE_DOC_ID) == owner.frontier(TREE_DOC_ID)


@pytest.mark.parametrize("raw", [b"not-json", b"{}", b'{"schemaVersion":1'])
def test_undecodable_envelopes_are_rejected_not_pending(tmp_path, raw):
    """review-p1 fix 14: undecodable envelopes never enter pending-meta."""

    receiver = _store(tmp_path, "receiver", "node-receiver")
    result = receiver.import_envelope(raw, supplier="node-owner")
    assert result.status == "rejected"
    assert receiver._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def test_local_owner_meta_commit_drains_pending(tmp_path):
    """review-p1 fix 14: a local owner metadata commit also drains pending rows."""

    receiver = _store(tmp_path, "receiver", "node-receiver")
    receiver.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    writer = receiver._writer_for("user:owner", None)
    future = Doc(client_id=receiver._client_id(writer))
    future.apply_update(receiver._docs["meta"].get_update())
    future.get("members", type=Map)["user:new"] = Map({"mode": "rw"})
    receiver._advance_commit_clock(future, writer=writer, seq=1)
    future_token = receiver._project_frontier(future, "user:owner")

    source = _store(tmp_path, "source", "node-source")
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    change = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("pending", Text("applied")),
        author="user:owner",
        actor="agent:x",
    )
    value = json.loads(change.envelope_bytes)
    value["origin"]["metaFrontier"] = base64.b64encode(future_token).decode()
    pending = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    assert receiver.import_envelope(pending, supplier="node-source").status == "pending"
    receiver.commit(
        "meta",
        lambda doc: doc["members"].__setitem__("user:new", Map({"mode": "rw"})),
        author="user:owner",
        actor=None,
    )
    assert receiver._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    assert receiver._docs[TREE_DOC_ID].get("pending", type=Text).to_py() == "applied"


def test_local_facade_meta_commit_delivers_drained_envelope(
    tmp_path, monkeypatch, request
):
    """review-p1-r2 R7: local invite drains pending into the open facade."""

    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    runtime = OrgFsRuntime(
        tmp_path / "r7-runtime", node_id="node-receiver", author="user:owner"
    )
    request.addfinalizer(runtime.close)
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.document.operations.spaces._now', lambda: "future")
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.document.projection._now', lambda: "future")
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.document.operations.purge._now', lambda: "future")
    info = runtime.facade.create_space("team")
    store = runtime.stores.get(info.space_id)
    space = runtime.facade._spaces[info.space_id]
    writer = store._writer_for("user:owner", None)
    facade_future = Doc(client_id=space.meta.client_id)
    facade_future.apply_update(space.meta.get_update())
    facade_future.get("members", type=Map)["user:new"] = Map(
        {"mode": "rw", "addedBy": "user:owner", "addedAt": "future", "removedAt": None}
    )
    future = Doc(client_id=store._client_id(writer))
    future.apply_update(store._docs["meta"].get_update())
    future.apply_update(facade_future.get_update())
    store._advance_commit_clock(future, writer=writer, seq=1)
    future_token = store._project_frontier(future, "user:owner")

    source = _store(tmp_path, "r7-source", "node-source")
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    change = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("pending-r7", Text("applied")),
        author="user:owner",
        actor="agent:x",
    )
    raw = json.loads(change.envelope_bytes)
    raw["spaceId"] = info.space_id
    raw["docId"] = space.tree_doc_id
    raw["origin"]["metaFrontier"] = base64.b64encode(future_token).decode()
    pending = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    assert store.import_envelope(pending, supplier="node-source").status == "pending"

    delivered: list[bytes] = []
    original = runtime.facade.apply_envelope

    def observed(space_id, envelope):
        delivered.append(envelope)
        return original(space_id, envelope)

    monkeypatch.setattr(runtime.facade, "apply_envelope", observed)
    runtime.facade.invite(info.space_id, "user:new")
    assert delivered == [pending]
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def test_batch_registration_delivers_drained_envelope_to_facade(
    tmp_path, monkeypatch, request
):
    """F2: commit_many consumes a registration-triggered drain via the facade."""

    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    runtime = OrgFsRuntime(
        tmp_path / "f2-runtime", node_id="node-receiver", author="user:owner"
    )
    request.addfinalizer(runtime.close)
    fs = runtime.facade
    info = fs.create_space("team")
    restored = fs.write_text(info.space_id, "restore.md", "before")
    fs.write_text(info.space_id, "restore.md", "after")
    remote = fs.write_text(info.space_id, "remote.md", "")
    assert remote.doc_id is not None

    source = _store(tmp_path, "f2-source", "node-source")
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    remote_change = source.commit(
        remote.doc_id,
        lambda doc: doc.get("text", type=Text).insert(0, "remote"),
        author="user:owner",
        actor="agent:remote",
    )

    store = runtime.stores.get(info.space_id)
    fs.actor = "agent:first-batch"
    original_commit_many = store.commit_many
    parked: list[bytes] = []
    predicted_tokens: list[bytes] = []

    def park_before_registration(operations, *, author, actor):
        items = tuple(operations)
        writer = store._writer_for(author, actor)
        mutation_ids = {
            client_id
            for doc_id, mutate in items
            for client_id in store._mutation_client_ids(doc_id, mutate, writer=writer)
        }
        future = store._new_doc("meta", client_id=store._client_id(writer))
        future.apply_update(store._docs["meta"].get_update())
        store._register_writer(
            future,
            writer=writer,
            author=author,
            actor=actor,
            peer_ids=mutation_ids,
        )
        seq = int(
            store._db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
                "WHERE writer = ? AND doc_id = 'meta'",
                (writer,),
            ).fetchone()[0]
        )
        store._advance_commit_clock(future, writer=writer, seq=seq)
        predicted = _encode_state_vector(_decode_state_vector(future.get_state()))
        predicted_tokens.append(predicted)
        raw = json.loads(remote_change.envelope_bytes)
        raw["spaceId"] = info.space_id
        raw["origin"]["metaFrontier"] = base64.b64encode(predicted).decode()
        pending = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        assert (
            store.import_envelope(pending, supplier="node-source").status == "pending"
        )
        parked.append(pending)
        return original_commit_many(items, author=author, actor=actor)

    monkeypatch.setattr(store, "commit_many", park_before_registration)
    fs.restore(info.space_id, "restore.md", restored.version)
    assert parked
    assert predicted_tokens == [store._meta_frontier_for_commit()]
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    assert fs.read_text(info.space_id, "remote.md")[0] == "remote"


def test_final_m1_failed_batch_preserves_drain_across_intervening_meta_commit(
    tmp_path, monkeypatch, request
):
    """m1: a failed registered batch cannot orphan its drained envelopes."""

    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    runtime = OrgFsRuntime(
        tmp_path / "m1-runtime", node_id="node-receiver", author="user:owner"
    )
    request.addfinalizer(runtime.close)
    fs = runtime.facade
    info = fs.create_space("team")
    fs.mkdir(info.space_id, "before")
    fs.mkdir(info.space_id, "after")
    restored = fs.write_text(info.space_id, "before/restore.md", "before")
    fs.move(info.space_id, f"id:{restored.node_id}", "after/renamed.md")
    fs.write_text(info.space_id, f"id:{restored.node_id}", "after")
    remote = fs.write_text(info.space_id, "remote.md", "")
    assert remote.doc_id is not None

    source = _store(tmp_path, "m1-source", "node-source")
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    remote_change = source.commit(
        remote.doc_id,
        lambda doc: doc.get("text", type=Text).insert(0, "remote"),
        author="user:owner",
        actor="agent:remote",
    )

    store = runtime.stores.get(info.space_id)
    fs.actor = "agent:failed-batch"
    original_commit_many = store.commit_many
    predicted_tokens: list[bytes] = []
    post_registration_tokens: list[bytes] = []
    intervening_commits: list[object] = []

    def fail_after_registration(operations, *, author, actor):
        items = tuple(operations)
        writer = store._writer_for(author, actor)
        mutation_ids = {
            client_id
            for doc_id, mutate in items
            for client_id in store._mutation_client_ids(doc_id, mutate, writer=writer)
        }
        future = store._new_doc("meta", client_id=store._client_id(writer))
        future.apply_update(store._docs["meta"].get_update())
        store._register_writer(
            future,
            writer=writer,
            author=author,
            actor=actor,
            peer_ids=mutation_ids,
        )
        seq = int(
            store._db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM commits "
                "WHERE writer = ? AND doc_id = 'meta'",
                (writer,),
            ).fetchone()[0]
        )
        store._advance_commit_clock(future, writer=writer, seq=seq)
        predicted = _encode_state_vector(_decode_state_vector(future.get_state()))
        predicted_tokens.append(predicted)
        raw = json.loads(remote_change.envelope_bytes)
        raw["spaceId"] = info.space_id
        raw["origin"]["metaFrontier"] = base64.b64encode(predicted).decode()
        pending = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        assert (
            store.import_envelope(pending, supplier="node-source").status == "pending"
        )
        try:
            return original_commit_many(items, author=author, actor=actor)
        except Exception:
            post_registration_tokens.append(store._meta_frontier_for_commit())
            intervening_commits.append(
                store.commit(
                    "meta",
                    lambda doc: doc["space"].__setitem__("m1", "intervening"),
                    author="user:owner",
                    actor=None,
                )
            )
            raise

    monkeypatch.setattr(store, "commit_many", fail_after_registration)
    store._db.execute(
        "CREATE TRIGGER fail_m1_batch BEFORE INSERT ON commits "
        f"WHEN NEW.doc_id = '{fs._space(info.space_id).tree_doc_id}' "
        "BEGIN SELECT RAISE(ABORT, 'm1-batch'); END"
    )
    store._db.commit()

    with pytest.raises(OrgFsError, match="m1-batch"):
        fs.restore(info.space_id, f"id:{restored.node_id}", restored.version)
    assert predicted_tokens == post_registration_tokens
    assert intervening_commits
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    assert fs.read_text(info.space_id, "remote.md")[0] == "remote"


def test_pending_update_blob_survives_missing_blob_during_drain(tmp_path):
    """n5/R9: a missing-blob pending row survives and applies on a later trigger."""

    class Blobs:
        def __init__(self):
            self.values = {}

        def put(self, space_id, data, *, reason):
            digest = __import__("hashlib").sha256(data).hexdigest()
            self.values[(space_id, digest)] = data
            return digest

        def get(self, space_id, digest):
            return self.values[(space_id, digest)]

    source_blobs = Blobs()
    source = _store(tmp_path, "r9-source", "node-source", blob_store=source_blobs)
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    large = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("large", Text("x" * 520_000)),
        author="user:owner",
        actor="agent:x",
    )
    future = source.commit(
        "meta",
        lambda doc: doc["members"].__setitem__("user:new", Map({"mode": "rw"})),
        author="user:owner",
        actor=None,
    )
    future_token = source.frontier("meta")
    raw = json.loads(large.envelope_bytes)
    raw["origin"]["metaFrontier"] = base64.b64encode(
        source._project_frontier(source._docs["meta"], "user:owner")
    ).decode()
    # The exact frontier is the one materialized by the future metadata commit.
    raw["origin"]["metaFrontier"] = base64.b64encode(future_token).decode()
    pending = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    digest = str(raw["updateBlob"])

    target_blobs = Blobs()
    target_blobs.values[("space-1", digest)] = source_blobs.values[("space-1", digest)]
    target = _store(tmp_path, "r9-target", "node-target", blob_store=target_blobs)
    for envelope in _envelopes(source):
        value = json.loads(envelope)
        if value["docId"] == "meta" and envelope != future.envelope_bytes:
            assert (
                target.import_envelope(envelope, supplier="node-source").status
                == "applied"
            )
    assert target.import_envelope(pending, supplier="node-source").status == "pending"
    del target_blobs.values[("space-1", digest)]
    assert (
        target.import_envelope(future.envelope_bytes, supplier="node-source").status
        == "applied"
    )
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 1
    assert (
        target._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id='meta'"
        ).fetchone()[0]
        == 3
    )
    target_blobs.values[("space-1", digest)] = source_blobs.values[("space-1", digest)]
    retry = source.commit(
        "meta",
        lambda doc: doc["space"].__setitem__("n5", "retry"),
        author="user:owner",
        actor=None,
    )
    assert (
        target.import_envelope(retry.envelope_bytes, supplier="node-source").status
        == "applied"
    )
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
    assert target._docs[TREE_DOC_ID].get("large", type=Text).to_py() == "x" * 520_000


def test_pending_delete_failure_is_deferred_out_of_durable_trigger(tmp_path, caplog):
    """n1: every non-StoreError drain failure is logged and leaves the row retryable."""

    source = _store(tmp_path, "n1-source", "node-source")
    source.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
        ),
        author="user:owner",
        actor=None,
    )
    change = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("n1", Text("applied")),
        author="user:owner",
        actor="agent:x",
    )
    future = source.commit(
        "meta",
        lambda doc: doc["members"].__setitem__("user:new", Map({"mode": "rw"})),
        author="user:owner",
        actor=None,
    )
    raw = json.loads(change.envelope_bytes)
    raw["origin"]["metaFrontier"] = base64.b64encode(source.frontier("meta")).decode()
    pending = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()

    target = _store(tmp_path, "n1-target", "node-target")
    for envelope in _envelopes(source):
        if (
            json.loads(envelope)["docId"] == "meta"
            and envelope != future.envelope_bytes
        ):
            assert (
                target.import_envelope(envelope, supplier="node-source").status
                == "applied"
            )
    assert target.import_envelope(pending, supplier="node-source").status == "pending"
    target._db.execute(
        "CREATE TRIGGER fail_pending_delete BEFORE DELETE ON pending "
        "BEGIN SELECT RAISE(ABORT, 'pending-delete'); END"
    )
    target._db.commit()
    with caplog.at_level("ERROR", logger='hyprial.daemon.impl.orgfs.storage.store'):
        assert (
            target.import_envelope(future.envelope_bytes, supplier="node-source").status
            == "applied"
        )
    assert "durable-trigger failure" in caplog.text
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 1
    assert target._docs[TREE_DOC_ID].get("n1", type=Text).to_py() == "applied"

    target._db.execute("DROP TRIGGER fail_pending_delete")
    target._db.commit()
    retry = source.commit(
        "meta",
        lambda doc: doc["space"].__setitem__("n1", "retry"),
        author="user:owner",
        actor=None,
    )
    assert (
        target.import_envelope(retry.envelope_bytes, supplier="node-source").status
        == "applied"
    )
    assert target._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
