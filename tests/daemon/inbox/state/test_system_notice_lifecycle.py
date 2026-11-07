"""PR #1572 port: durable, recipient-scoped notice settlement and bounded storage."""

from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection
from hyprial.kernel import DaemonRequestError

ACTOR = "agent:owner:local:worker"
OTHER = "agent:owner:local:other"


def notice(mid="notice", recipient=ACTOR):
    return InboxMessage(
        message_id=mid,
        conversation_id="notices",
        sender="system:hyprial:node",
        recipient=recipient,
        payload=b"{}",
        intent="system",
        lifecycle=DeliveryLifecycle.ONLINE_ONLY,
        created_at_ms=0,
    )


@pytest.fixture
def store(tmp_path):
    service = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    yield service
    service.close()


@pytest.mark.parametrize("settle", ["ack", "drain", "dismiss"])
def test_notice_settlement_is_scoped_durable_and_replay_safe(store, settle):
    item = notice()
    assert store.receive_system_notice(item)
    assert not store.ack(OTHER, item.message_id).acknowledged
    assert not store.dismiss_system_notice(item.message_id, recipient=OTHER)
    if settle == "ack":
        assert store.ack(ACTOR, item.message_id).acknowledged
    elif settle == "drain":
        assert store.drain_system_notices(ACTOR) == (item,)
    else:
        assert store.dismiss_system_notice(item.message_id, recipient=ACTOR)
    assert store.system_notices(ACTOR) == ()
    assert store.ack(ACTOR, item.message_id).code == "MESSAGE_ALREADY_SETTLED"
    assert not store.ack(OTHER, item.message_id).acknowledged
    assert not store.ack(ACTOR, "unknown").acknowledged
    database = store.database
    store.close()
    reopened = InboxService(database, MemoryDeliveryTransport())
    try:
        assert reopened.receive_system_notice(item)
        assert reopened.system_notices(ACTOR) == ()
        assert reopened.ack(ACTOR, item.message_id).code == "MESSAGE_ALREADY_SETTLED"
    finally:
        reopened.close()


def test_reminder_replay_is_explicit_and_renews_tombstone(store, monkeypatch):
    monkeypatch.setattr(store, "_now_ms", lambda: 100)
    item = notice()
    store.receive_system_notice(item)
    assert store.ack(ACTOR, item.message_id).acknowledged
    monkeypatch.setattr(store, "_now_ms", lambda: 200)
    assert store.receive_system_notice(item, allow_replay=True)
    assert store.system_notices(ACTOR) == (item,)
    assert store.ack(ACTOR, item.message_id).acknowledged
    assert (
        store._db.execute("SELECT acked_at_ms FROM system_notice_acks").fetchone()[0]
        == 200
    )


def test_bounds_use_receive_clock_and_never_ack_pruned_rows(store, monkeypatch):
    monkeypatch.setattr(store, "_now_ms", lambda: 100)
    store.receive_system_notice(replace(notice("old"), created_at_ms=10**15))
    monkeypatch.setattr(store, "_now_ms", lambda: 200)
    store.receive_system_notice(notice("new"))
    assert store.prune_system_notices(now_ms=200, retention_ms=50) == (1, 0)
    assert not store.ack(ACTOR, "old").acknowledged
    store.receive_system_notice(notice("last"))
    assert store.prune_system_notices(now_ms=200, max_rows=1) == (0, 1)
    assert [n.message_id for n in store.system_notices(ACTOR)] == ["last"]
    assert not store.ack(ACTOR, "new").acknowledged
    assert store.ack(ACTOR, "last").acknowledged
    store.prune_system_notices(now_ms=300, retention_ms=50)
    assert not store.ack(ACTOR, "last").acknowledged


def test_receive_and_ack_apply_caps_without_an_explicit_sweep(store, monkeypatch):
    from hyprial.daemon.impl.inbox.service.delivery import system_notices

    monkeypatch.setattr(system_notices, "SYSTEM_NOTICE_MAX_ROWS", 2)
    for i in range(4):
        store.receive_system_notice(notice(str(i)))
    assert len(store.system_notices(ACTOR)) == 2
    for i in range(4, 8):
        store.receive_system_notice(notice(str(i)))
        assert store.ack(ACTOR, str(i)).acknowledged
    assert (
        store._db.execute("SELECT COUNT(*) FROM system_notice_acks").fetchone()[0] == 2
    )
    assert store.receive_system_notice(notice("4"))  # evicted tombstone ends dedup
    assert "4" in [n.message_id for n in store.system_notices(ACTOR)]


def test_legacy_schema_and_text_payload_remain_readable(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""CREATE TABLE system_notices (
            message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            sender TEXT NOT NULL, recipient TEXT NOT NULL, payload BLOB NOT NULL,
            intent TEXT NOT NULL, lifecycle TEXT NOT NULL, idempotency_key TEXT,
            created_at_ms INTEGER NOT NULL);
        """)
        db.execute(
            "INSERT INTO system_notices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "conv",
                "system:hyprial:old",
                "old-node",
                "{}",
                "system",
                DeliveryLifecycle.ONLINE_ONLY.value,
                None,
                0,
            ),
        )
        db.commit()
    service = InboxService(path, MemoryDeliveryTransport())
    try:
        assert service.prune_system_notices(now_ms=10**15) == (0, 0)
        assert service.system_notices("old-node")[0].payload == b"{}"
        assert InboxReadProjection(path).system_notices("old-node")[0].payload == b"{}"
        assert (
            service._db.execute("SELECT received_at_ms FROM system_notices").fetchone()[
                0
            ]
            is None
        )
    finally:
        service.close()


def test_migration_resolves_only_known_local_agents(store):
    for key in ("known", "missing", "ambiguous", "remote", "old-node", store.node_id):
        store.receive_system_notice(notice(key, key))
    database = store.database
    store.close()

    def resolve(key):
        if key == "known":
            return ACTOR
        if key == "remote":
            return "agent:owner:remote:worker"
        if key == "ambiguous":
            raise DaemonRequestError("AMBIGUOUS_TARGET", "two matches")
        return None

    service = InboxService(
        database, MemoryDeliveryTransport(), notice_recipient_resolver=resolve
    )
    try:
        assert service.ack(ACTOR, "known").acknowledged
        for key in ("missing", "ambiguous", "remote", "old-node", service.node_id):
            assert len(service.system_notices(key)) == 1
    finally:
        service.close()


def test_migration_failure_rolls_back_whole_pass_then_retries(store):
    store.receive_system_notice(notice("first", "a-known"))
    store.receive_system_notice(notice("second", "z-failure"))
    database = store.database
    store.close()

    def resolve(key):
        if key == "z-failure":
            raise OSError("identity source unavailable")
        return ACTOR

    service = InboxService(
        database, MemoryDeliveryTransport(), notice_recipient_resolver=resolve
    )
    try:
        assert len(service.system_notices("a-known")) == 1
        assert len(service.system_notices("z-failure")) == 1
        assert service.system_notices(ACTOR) == ()
    finally:
        service.close()
    service = InboxService(
        database, MemoryDeliveryTransport(), notice_recipient_resolver=lambda _: ACTOR
    )
    try:
        assert len(service.system_notices(ACTOR)) == 2
    finally:
        service.close()


def test_transactional_failure_notice_path_obeys_replay_and_bounds(store, monkeypatch):
    from hyprial.daemon.impl.inbox.service.delivery import system_notices

    monkeypatch.setattr(system_notices, "SYSTEM_NOTICE_MAX_ROWS", 2)
    store.receive_system_notice(notice("acked"))
    assert store.ack(ACTOR, "acked").acknowledged
    # The DLQ path calls the locked insert inside its own transaction.
    with store._db:
        store._persist_system_notice_locked(notice("acked"))
        for index in range(4):
            store._persist_system_notice_locked(notice(str(index)))
    assert [n.message_id for n in store.system_notices(ACTOR)] == ["2", "3"]


def test_notice_delete_and_tombstone_roll_back_together(store):
    store.receive_system_notice(notice())
    store._db.execute("""CREATE TRIGGER reject_notice_ack BEFORE INSERT ON system_notice_acks
        BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test write failure"):
        store.ack(ACTOR, "notice")
    assert len(store.system_notices(ACTOR)) == 1
    assert (
        store._db.execute("SELECT COUNT(*) FROM system_notice_acks").fetchone()[0] == 0
    )
    store._db.execute("DROP TRIGGER reject_notice_ack")
    assert store.ack(ACTOR, "notice").acknowledged


@pytest.mark.parametrize(
    "replay_path", ["receive", "locked", "unresolved", "source-failure"]
)
def test_migrated_ack_survives_old_key_replay_and_second_restart(store, replay_path):
    legacy = notice("legacy-mid", "worker")
    store.receive_system_notice(legacy)
    database = store.database
    store.close()

    def reopen():
        return InboxService(
            database,
            MemoryDeliveryTransport(),
            notice_recipient_resolver=lambda _: ACTOR,
        )

    service = reopen()
    try:
        assert service.ack(ACTOR, legacy.message_id).acknowledged
        if replay_path == "unresolved":
            # An older writer or temporarily unavailable identity mapping can
            # leave a bare row alongside the canonical tombstone on disk.
            service._notice_recipient_resolver = None
        if replay_path == "source-failure":

            def unavailable(_):
                raise OSError("identity source unavailable")

            service._notice_recipient_resolver = unavailable
        if replay_path == "locked":
            with service._db:
                service._persist_system_notice_locked(legacy)
        else:
            assert service.receive_system_notice(legacy)
    finally:
        service.close()
    service = reopen()
    try:
        assert service.system_notices(ACTOR) == ()
        assert service.system_notices("worker") == ()
        assert service.ack(ACTOR, legacy.message_id).code == "MESSAGE_ALREADY_SETTLED"
    finally:
        service.close()


@pytest.mark.parametrize("allow_replay", [False, True])
def test_bare_notice_admission_checks_canonical_tombstone(store, allow_replay):
    store.receive_system_notice(notice())
    assert store.ack(ACTOR, "notice").acknowledged
    store._notice_recipient_resolver = lambda _: ACTOR
    assert store.receive_system_notice(
        notice(recipient="worker"), allow_replay=allow_replay
    )
    assert store.system_notices("worker") == ()
    assert store.system_notices(ACTOR) == ((notice(),) if allow_replay else ())
    assert store.ack(ACTOR, "notice").code == (
        None if allow_replay else "MESSAGE_ALREADY_SETTLED"
    )


@pytest.mark.parametrize("tombstone", ["active", "expired", "other-recipient"])
def test_migration_suppresses_only_live_matching_canonical_tombstones(store, tombstone):
    from hyprial.daemon.impl.inbox.service.delivery.system_notices import (
        SYSTEM_NOTICE_RETENTION_MS,
    )

    recipient = OTHER if tombstone == "other-recipient" else ACTOR
    store.receive_system_notice(notice(recipient=recipient))
    assert store.ack(recipient, "notice").acknowledged
    store.receive_system_notice(notice(recipient="worker"))
    if tombstone == "expired":
        with store._db:
            store._db.execute(
                "UPDATE system_notice_acks SET acked_at_ms = ?",
                (store._now_ms() - SYSTEM_NOTICE_RETENTION_MS - 1,),
            )
    store._notice_recipient_resolver = lambda _: ACTOR
    store.migrate_system_notice_recipients()
    assert store.system_notices("worker") == ()
    assert store.system_notices(ACTOR) == (() if tombstone == "active" else (notice(),))
    assert store.ack(ACTOR, "notice").code == (
        "MESSAGE_ALREADY_SETTLED" if tombstone == "active" else None
    )


@pytest.mark.parametrize("mapping", ["node", "unknown", "ambiguous", "remote"])
def test_notice_admission_preserves_unconfirmed_and_node_keys(store, mapping):
    recipient = store.node_id if mapping == "node" else "worker"

    def resolve(_):
        if mapping == "ambiguous":
            raise DaemonRequestError("AMBIGUOUS_TARGET", "two matches")
        return {"node": ACTOR, "unknown": None, "remote": "agent:owner:remote:worker"}[
            mapping
        ]

    store._notice_recipient_resolver = resolve
    item = notice(recipient=recipient)
    assert store.receive_system_notice(item)
    assert store.system_notices(recipient) == (item,)
    assert store.system_notices(ACTOR) == ()


def test_migration_source_failure_rolls_back_replay_suppression(store):
    store.receive_system_notice(notice())
    assert store.ack(ACTOR, "notice").acknowledged
    store.receive_system_notice(notice(recipient="a-known"))
    store.receive_system_notice(notice("second", "z-failure"))

    def resolve(key):
        if key == "z-failure":
            raise OSError("identity source unavailable")
        return ACTOR

    store._notice_recipient_resolver = resolve
    assert store.migrate_system_notice_recipients() == (0, 0)
    assert len(store.system_notices("a-known")) == 1
    assert len(store.system_notices("z-failure")) == 1
    assert store.ack(ACTOR, "notice").code == "MESSAGE_ALREADY_SETTLED"


def test_concurrent_ack_and_drain_do_not_lose_settlement(store):
    from concurrent.futures import ThreadPoolExecutor

    for index in range(40):
        store.receive_system_notice(notice(str(index)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(store.ack, ACTOR, str(index % 40)) for index in range(80)
        ]
        drain = pool.submit(store.drain_system_notices, ACTOR)
        assert all(future.result().acknowledged for future in futures)
        drain.result()
    assert store.system_notices(ACTOR) == ()
