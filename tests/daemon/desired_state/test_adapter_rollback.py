"""Actual pre-split codecs must refuse v3 and accept offline restoration."""
import hashlib
import json

import pytest

from hyprial.daemon.impl.adapters.rollback import downgrade_adapter_state
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import DesiredStateError
from .test_adapter_model import legacy_document

from .compat import base_runtime, golden_documents


def base_reader(monkeypatch):
    runtime = base_runtime(monkeypatch)
    return runtime.DesiredState, runtime.DesiredStateSqliteShadow


def seed_agent_pins(tmp_path):
    from hyprial.identity.impl.agents.registry import AgentRegistry
    path = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(path, owner="owner", machine="node", hyprial_home=tmp_path / "home")
    try:
        source = tmp_path / "personality"
        source.mkdir()
        registry.create("X", config={"sources": [{"path": str(source), "required": True}],
                                     "discovery": "explicit-only"})
        registry.pin("X", "X")
    finally:
        registry.close()
    return path


def seed(tmp_path):
    path = tmp_path / "desired-state.json"
    path.with_suffix(".json.v1").write_text(json.dumps(legacy_document()))
    return DesiredStateStore(path)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("source", ["sqlite", "v1"])
def test_restore_matches_actual_base_golden_without_git_history(tmp_path, version, source):
    import sqlite3
    from hyprial.daemon.impl.state_db import StateDatabase
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow

    golden = golden_documents()
    original = golden[f"schema{version}"]
    path = tmp_path / "desired-state.json"
    database = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    shadow = DesiredStateSqliteShadow(database)
    if source == "sqlite":
        shadow.write_document(original)
    else:
        path.with_suffix(".json.v1").write_text(json.dumps(original))
    pins = seed_agent_pins(tmp_path)
    before = pins.read_bytes()
    store = DesiredStateStore(path)
    assert [s.harness for s in store.load().harnesses] == ["pi", "codex"]
    assert [s.name for s in store.load().adapters] == ["X", "other"]
    downgrade_adapter_state(tmp_path)
    assert shadow.read_document() == golden["schema2Restored"]
    assert pins.read_bytes() == before
    with sqlite3.connect(pins) as db:
        rows = db.execute("SELECT adapter,agent FROM pins ORDER BY adapter").fetchall()
    assert json.dumps(rows, separators=(",", ":")) == golden["pinsRowsJson"]


def test_restore_then_actual_base_reader_keeps_adapters_and_pins(tmp_path, monkeypatch):
    old, old_shadow = base_reader(monkeypatch)
    store = seed(tmp_path)
    with pytest.raises(DesiredStateError, match="unsupported desired-state schema version 3"):
        old.from_json(old_shadow(store.state_db).read_document())
    result = downgrade_adapter_state(tmp_path)
    assert result["schemaVersion"] == 2
    document = old_shadow(store.state_db).read_document()
    loaded = old.from_json(document)
    assert {(s.harness, s.name) for s in loaded.harnesses} == {
        ("pi", "X"), ("codex", "Y"), ("lark", "X"), ("lark", "other")}
    assert dict(loaded.channel_pins) == legacy_document()["channelPins"]


def test_backup_is_first_copy_and_has_verified_digest(tmp_path):
    store = seed(tmp_path)
    def backup():
        with store.state_db.read() as db:
            return tuple(db.execute("SELECT schema_version,sha256,document_json FROM adapter_model_backup").fetchone())
    first = backup()
    assert first[0] == 1
    assert first[1] == hashlib.sha256(first[2].encode()).hexdigest()
    DesiredStateStore(tmp_path / "desired-state.json")
    assert backup() == first
    downgrade_adapter_state(tmp_path)
    DesiredStateStore(tmp_path / "desired-state.json")
    assert backup() == first


def test_restore_refuses_drift_and_bad_backup(tmp_path):
    store = seed(tmp_path)
    store.set_adapter("later", True)
    with pytest.raises(DesiredStateError, match="changed after migration"):
        downgrade_adapter_state(tmp_path)
    store.set_adapter("later", False)
    with store.state_db.transaction() as db:
        db.execute("UPDATE adapter_model_backup SET sha256='bad'")
    with pytest.raises(DesiredStateError, match="SHA256 mismatch"):
        downgrade_adapter_state(tmp_path)


def test_actual_base_sqlite_migrates_atomically_and_preserves_agent_pins(tmp_path, monkeypatch):
    from hyprial.daemon.impl.state_db import StateDatabase

    old, old_shadow = base_reader(monkeypatch)
    database = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    old_shadow(database).write_document(old.from_json(legacy_document()).to_json())
    pins = seed_agent_pins(tmp_path)
    before = pins.read_bytes()
    store = DesiredStateStore(tmp_path / "desired-state.json")
    assert [s.name for s in store.load().adapters] == ["X", "other"]
    with database.read() as db:
        assert db.execute("SELECT count(*) FROM harnesses WHERE harness='lark'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM adapter_model_backup").fetchone()[0] == 1
    downgrade_adapter_state(tmp_path)
    assert pins.read_bytes() == before
    assert len(old.from_json(old_shadow(database).read_document()).harnesses) == 4


def test_runtime_only_downgrade_refuses_schema3_without_mutation(tmp_path):
    from hyprial.daemon.impl.transfer.landing.runtime import downgrade_state, RuntimeDowngradeError
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow
    from hyprial.daemon.impl.state_db import StateDatabase
    shadow = DesiredStateSqliteShadow(StateDatabase(tmp_path / "lifecycle-operations.sqlite3"))
    shadow.write_document(golden_documents()["schema2WithRuntime"])
    DesiredStateStore(tmp_path / "desired-state.json")
    before = shadow.read_document()
    with pytest.raises(RuntimeDowngradeError, match="adapter downgrade-state") as caught:
        downgrade_state(tmp_path, tmp_path / "refused")
    assert caught.value.code == "RUNTIME_DOWNGRADE_REFUSED"
    assert not (tmp_path / "refused").exists()
    assert shadow.read_document() == before


def test_full_two_step_rollback_preserves_adapters_and_pins(tmp_path, monkeypatch):
    from hyprial.daemon.impl.transfer.landing.runtime import downgrade_state, RuntimeDowngradeError
    from tests.integration.smolvm.runtime.test_smolvm_runtime_state import vm_spec
    from hyprial.daemon.impl.state_db import StateDatabase

    runtime = base_runtime(monkeypatch)
    pins = seed_agent_pins(tmp_path)
    before = pins.read_bytes()
    database = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    document = legacy_document(2)
    document["providers"].append(vm_spec().to_json())
    runtime.DesiredStateSqliteShadow(database).write_document(runtime.DesiredState.from_json(document).to_json())
    store = DesiredStateStore(tmp_path / "desired-state.json")
    with pytest.raises(RuntimeDowngradeError, match="adapter downgrade-state") as caught:
        downgrade_state(tmp_path, tmp_path / "refused")
    assert caught.value.code == "RUNTIME_DOWNGRADE_REFUSED"
    assert not (tmp_path / "refused").exists()
    downgrade_adapter_state(tmp_path)
    result = runtime.downgrade_state(tmp_path, tmp_path / "export")
    assert result["schemaVersion"] == 1
    restored = runtime.DesiredState.from_json(runtime.DesiredStateSqliteShadow(store.state_db).read_document())
    assert {(s.harness, s.name) for s in restored.harnesses} == {
        ("pi", "X"), ("codex", "Y"), ("lark", "X"), ("lark", "other")}
    assert dict(restored.channel_pins) == legacy_document()["channelPins"]
    assert pins.read_bytes() == before


def test_migration_write_failure_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    from hyprial.daemon.impl.adapters import storage
    from hyprial.daemon.impl.state_db import StateDatabase
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow
    database = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    original = golden_documents()["schema2"]
    DesiredStateSqliteShadow(database).write_document(original)
    write = storage.insert_adapters

    def fail_after_write(connection, document):
        write(connection, document)
        raise OSError("injected disk failure")

    monkeypatch.setattr(storage, "insert_adapters", fail_after_write)
    with pytest.raises(OSError, match="injected disk failure"):
        DesiredStateStore(tmp_path / "desired-state.json")
    assert DesiredStateSqliteShadow(database).read_document() == original
    with database.read() as db:
        assert db.execute("SELECT count(*) FROM adapter_model_backup").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM adapters").fetchone()[0] == 0
    monkeypatch.setattr(storage, "insert_adapters", write)
    store = DesiredStateStore(tmp_path / "desired-state.json")
    assert len(store.load().adapters) == 2
    assert store.load().schema_version == 3


@pytest.mark.parametrize("extra", [
    {"turnTimeoutSeconds": 19}, {"containerized": False}, {"nickname": None},
    {"turnTimeoutSeconds": 19.5}, {},
    {"args": [], "ownership": "managed", "command": [], "containerized": False,
     "nickname": None, "cwd": None, "endpoint": None, "sessionRef": None,
     "turnTimeoutSeconds": None, "idleTimeoutSeconds": None, "pinnedOwner": None,
     "containerImage": None, "executionRuntime": None, "modelProvider": None, "model": None},
    {"idleTimeoutSeconds": 19},
])
@pytest.mark.parametrize("old_save", [False, True], ids=["sqlite", "actual-base-save"])
def test_repeated_restore_preserves_semantic_legacy_intent(tmp_path, monkeypatch, extra, old_save):
    old = base_runtime(monkeypatch) if old_save else None
    path = tmp_path / "desired-state.json"
    document = {"schemaVersion": 1, "interactiveSessions": [], "providers": [
        {"provider": "lark", "name": "alerts", "headless": True, **extra},
    ]}
    path.with_suffix(".json.v1").write_text(json.dumps(document))
    for _ in range(3):
        store = DesiredStateStore(path)
        assert [adapter.name for adapter in store.load().adapters] == ["alerts"]
        assert downgrade_adapter_state(tmp_path)["schemaVersion"] == 2
        if old is not None:
            old_store = old.DesiredStateStore(path)
            old_store.save(old_store.load())


@pytest.mark.parametrize("changed", [
    {"turnTimeoutSeconds": 19.5}, {"nickname": "changed"},
    {"containerized": True}, {"unknownLegacyField": None},
])
def test_restore_refuses_real_legacy_metadata_change(tmp_path, changed):
    from dataclasses import replace

    path = tmp_path / "desired-state.json"
    document = {"schemaVersion": 1, "interactiveSessions": [], "providers": [
        {"provider": "lark", "name": "alerts", "headless": True,
         "turnTimeoutSeconds": 19, "containerized": False, "nickname": None},
    ]}
    path.with_suffix(".json.v1").write_text(json.dumps(document))
    DesiredStateStore(path)
    downgrade_adapter_state(tmp_path)
    store = DesiredStateStore(path)
    current = store.load()
    adapter = current.adapters[0]
    metadata = {**json.loads(adapter.legacy_launch), **changed}
    store.save(replace(current, adapters=(replace(adapter, legacy_launch=json.dumps(metadata)),)))
    with pytest.raises(DesiredStateError, match="changed after migration"):
        downgrade_adapter_state(tmp_path)


def test_restore_requires_daemon_stopped(tmp_path):
    from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence, DaemonOwnershipBusy
    seed(tmp_path)
    with DaemonStateOwnershipFence.acquire(tmp_path):
        with pytest.raises(DaemonOwnershipBusy):
            downgrade_adapter_state(tmp_path)
