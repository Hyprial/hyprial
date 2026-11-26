from dataclasses import replace

import pytest

from hyprial.daemon.impl.desired_state.store import DesiredStateStore
from hyprial.kernel import DesiredStateError, HarnessLaunchSpec
from .test_transactions import candidate, prepare


@pytest.mark.parametrize(
    "change",
    [
        {"model": " hy4-flash"},
        {"model": ""},
        {"apiKey": "never-store"},
        {"configRevision": True},
        {"nativeAuthRef": "extra"},
        {"authKind": "unknown"},
        {"intentDigest": "not-digest"},
    ],
)
def test_invalid_candidate_refused_before_database(tmp_path, change):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    with pytest.raises(DesiredStateError):
        store.model_binding_store.prepare(
            "a.fixture", "member", "op", 0, {**candidate(), **change}
        )
    assert not store.state_db.exists()


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"bindingRef": "r", "bindingRevision": True},
        {"bindingRef": "r", "bindingRevision": 0},
        {"bindingRef": "r", "bindingRevision": 1, "apiKey": "secret"},
    ],
)
def test_closed_launch_snapshot(snapshot):
    with pytest.raises(DesiredStateError):
        HarnessLaunchSpec("pi", "fixture", True, model_binding=snapshot)


def test_full_document_writer_retains_model_authority(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    prepare(store)
    state = store.load()
    store.save(replace(state, as_mailbox=True))
    assert store.load().model_binding_operations == state.model_binding_operations
    payload = store.load().to_json()
    payload["modelBindingOperations"][0]["candidate"]["key"] = "forbidden"
    with pytest.raises(DesiredStateError):
        type(state).from_json(payload)


def test_historical_receipt_does_not_claim_current_revision(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    first = prepare(store)
    api = store.model_binding_store
    api.finalize("a.fixture", "member-1", "op-1", first.request_digest, 0)
    second = prepare(store, operation="op-2", revision=1, model="new-model")
    api.finalize("a.fixture", "member-1", "op-2", second.request_digest, 1)
    assert prepare(store).result_revision == 1
    assert api.status("a.fixture", "member-1").binding.binding_revision == 2
    assert api.status("a.fixture", "member-1").binding.binding_ref == first.binding_ref


def test_optional_snapshot_column_does_not_repeat_ghost_resolution(
    tmp_path, monkeypatch
):
    from hyprial.daemon.impl.desired_state import sqlite

    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.save(store.load())
    with store.state_db.transaction() as db:
        db.execute("ALTER TABLE harnesses DROP COLUMN model_binding_json")

    def already_migrated(_db):
        raise AssertionError("historical ghost resolution must not run again")

    monkeypatch.setattr(sqlite, "_journal_proved_running", already_migrated)
    reopened = DesiredStateStore(store.legacy_path)
    assert reopened.load().schema_version == 3
    with reopened.state_db.read() as db:
        assert "model_binding_json" in {
            row[1] for row in db.execute("PRAGMA table_info(harnesses)")
        }


@pytest.mark.parametrize("field", ["authKind", "harness", "mode"])
def test_non_scalar_discriminator_is_a_safe_configuration_error(tmp_path, field):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    with pytest.raises(DesiredStateError, match="invalid-connection"):
        store.model_binding_store.prepare(
            "a.fixture", "member", "operation", 0, {**candidate(), field: []}
        )
    assert not store.state_db.exists()
