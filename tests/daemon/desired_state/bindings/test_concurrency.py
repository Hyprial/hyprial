from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from hyprial.daemon.impl.desired_state.bindings.models import (
    Configuration,
    Operation,
    request_digest,
)
from hyprial.daemon.impl.desired_state.bindings.schema import insert_operation
from hyprial.daemon.impl.desired_state.store import DesiredStateStore
from hyprial.kernel import DesiredStateError
from .test_transactions import candidate, prepare


def seed_receipts(store, count):
    initial = prepare(store)
    store.model_binding_store.cancel(
        "a.fixture", "member-1", "op-1", initial.request_digest, 0
    )
    config = Configuration.from_json(candidate())
    with store.state_db.transaction() as db:
        for index in range(1, count):
            insert_operation(
                db,
                Operation(
                    "a.fixture",
                    "member-1",
                    f"seed-{index}",
                    request_digest("a.fixture", "member-1", 0, config),
                    0,
                    initial.binding_ref,
                    config,
                    "cancelled",
                    0,
                ),
            )


def test_capacity_and_priority_allow_recovery_and_explicit_increase(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    seed_receipts(store, 9999)
    last = prepare(store, operation="last")
    with pytest.raises(DesiredStateError, match="operation-in-progress"):
        prepare(store, operation="overflow")
    assert prepare(store, operation="last", operation_limit=None) == last
    with pytest.raises(DesiredStateError, match="operation-digest-mismatch"):
        prepare(store, operation="last", model="changed")
    store.model_binding_store.cancel(
        "a.fixture", "member-1", "last", last.request_digest, 0
    )
    with pytest.raises(DesiredStateError, match="operation-capacity"):
        prepare(store, operation="overflow")
    extended = prepare(store, operation="overflow", operation_limit=10001)
    done = store.model_binding_store.finalize(
        "a.fixture", "member-1", "overflow", extended.request_digest, 0
    )
    assert done.result_revision == 1
    assert (
        len(store.model_binding_store.status("a.fixture", "member-1").operations)
        == 10001
    )


def compete(*actions):
    barrier = threading.Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=5)
        try:
            return action()
        except DesiredStateError as error:
            return error

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


def test_two_stores_compete_for_last_capacity_and_one_prepared(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    seed_receipts(store, 9999)
    other = DesiredStateStore(store.legacy_path)
    results = compete(
        lambda: prepare(store, operation="first"),
        lambda: prepare(other, operation="second"),
    )
    assert sum(isinstance(item, Operation) for item in results) == 1
    assert sum(isinstance(item, DesiredStateError) for item in results) == 1
    assert (
        len(store.model_binding_store.status("a.fixture", "member-1").operations)
        == 10000
    )


def test_cancel_finalize_race_persists_one_terminal_across_restart(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    first = prepare(store)
    other = DesiredStateStore(store.legacy_path)
    args = ("a.fixture", "member-1", "op-1", first.request_digest, 0)
    results = compete(
        lambda: store.model_binding_store.finalize(*args),
        lambda: other.model_binding_store.cancel(*args),
    )
    terminal = [item for item in results if isinstance(item, Operation)]
    assert len(terminal) == 1
    assert prepare(DesiredStateStore(store.legacy_path)) == terminal[0]


def test_marker_races_prepare_and_stale_cached_writer(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    stale = store.load()
    other = DesiredStateStore(store.legacy_path)
    compete(
        lambda: prepare(store),
        lambda: other.model_binding_store.mark_cleanup("a.fixture", "token", "destroy"),
    )
    with pytest.raises(DesiredStateError, match="destroy-cleanup-pending"):
        prepare(store, member="another")
    with pytest.raises(DesiredStateError):
        store.save(replace(stale, as_mailbox=True))


def test_write_failure_rolls_back_schema_and_prepare(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.save(store.load())
    with store.state_db.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_prepare BEFORE INSERT ON model_binding_operations BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        prepare(store)
    assert store.load().schema_version == 3
    assert store.load().model_binding_operations == ()


def test_purge_failure_retains_all_rows_and_agent_marker(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    prepare(store)
    api = store.model_binding_store
    api.mark_cleanup("a.fixture", "token", "attempt")
    before = store.load()
    with store.state_db.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_purge BEFORE DELETE ON model_agent_lifecycle BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        api.purge_destroyed_agent("a.fixture", "token", "attempt")
    assert store.load() == before


def test_incremental_writer_cannot_publish_an_old_binding_snapshot(
    tmp_path, monkeypatch
):
    from hyprial.daemon.impl.desired_state.documents import PendingSessionAgentEffect

    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.save(store.load())
    other = DesiredStateStore(store.legacy_path)
    original = store._sqlite_shadow.insert_session_agent_effects

    def interleaved(rows):
        prepare(other)
        return original(rows)

    monkeypatch.setattr(
        store._sqlite_shadow, "insert_session_agent_effects", interleaved
    )
    store.record_session_agent_effects(
        (
            PendingSessionAgentEffect(
                "effect", "correlation", "bind", "actor", "pi", "managed", "session"
            ),
        )
    )
    assert store.published_state() == store.load()


def test_failed_agent_marker_does_not_promote_legacy_root(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.save(store.load())
    with store.state_db.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_marker BEFORE INSERT ON model_agent_lifecycle BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        store.model_binding_store.mark_cleanup("a.fixture", "token", "destroy")
    assert store.load().schema_version == 3
    assert store.load().model_agent_lifecycle == ()


def test_post_write_corruption_rolls_back_inside_transaction(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    first = prepare(store)
    api = store.model_binding_store
    api.finalize("a.fixture", "member-1", "op-1", first.request_digest, 0)
    pending = prepare(store, operation="op-2", revision=1)
    before = store.load()
    with store.state_db.transaction() as db:
        db.execute(
            "CREATE TRIGGER corrupt_result AFTER UPDATE OF phase ON model_binding_operations WHEN NEW.phase='finalized' BEGIN UPDATE model_binding_operations SET binding_ref='mb.corrupt' WHERE operation_id=NEW.operation_id; END"
        )
    with pytest.raises(DesiredStateError):
        api.finalize("a.fixture", "member-1", "op-2", pending.request_digest, 1)
    assert store.load() == before
    assert DesiredStateStore(store.legacy_path).load() == before
