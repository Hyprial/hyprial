"""Removal intent persists in the shared state database before external effects."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

import pytest

from hyprial.daemon.impl.adapters.spec import AdapterSpec
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
from hyprial.daemon.impl.operations.management import AdapterRegistrySnapshot
from hyprial.daemon.impl.operations.adapter_removal.journal import (
    AdapterRemovalJournal, RemovalIntent, RemovalPhase, RemovalJournalConflict,
)
from hyprial.daemon.impl.state_db import StateDatabase
from tests.support.runtime.environment import isolated_environment


def intent(operation="preview-one", adapter="app"):
    return RemovalIntent(
        operation_id=operation, adapter=adapter, caller="user:alice", epoch="epoch-one",
        target_digest="a" * 64,
        channels_before=b'{"version": 1, "gateways": []}\n',
        channels_after=b'{"version":1,"gateways":[]}\n',
        registry=AdapterRegistrySnapshot(
            HarnessAdapterRegistrationProjection(adapter, AdapterSpec("lark", adapter), "agent:alice:node:one"),
            "agent:alice:node:one",
        ),
        agent_entity_token="entity-one",
        credential_ref=f"lark-{adapter}", credential_digest="b" * 64,
        remove_credential=True,
    )


def test_preimage_survives_reopen_without_advancing_or_storing_secret(tmp_path):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journal = AdapterRemovalJournal(state)
    prepared = journal.prepare(intent())
    assert prepared.phase is RemovalPhase.PREPARED
    reopened = AdapterRemovalJournal(state)
    assert reopened.pending() == (prepared,)
    assert reopened.get("preview-one") == prepared
    assert prepared.intent.channels_before != prepared.intent.channels_after
    assert prepared.intent.registry.agent_pin == "agent:alice:node:one"
    assert prepared.intent.registry.harness.spec == AdapterSpec("lark", "app")
    with state.read() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(adapter_removal_intents)")}
    assert "secret" not in columns
    assert list(tmp_path.iterdir()) == [state.path]
    assert state.path.stat().st_mode & 0o777 == 0o600


def test_intent_identity_is_immutable_and_different_operation_cannot_steal_target(tmp_path):
    journal = AdapterRemovalJournal(StateDatabase(tmp_path / "state.sqlite3"))
    first = journal.prepare(intent())
    assert journal.prepare(intent()) == first
    for changed in (
        replace(intent(), caller="user:bob"),
        replace(intent(), epoch="another-epoch"),
        replace(intent(), channels_before=b"changed"),
        replace(intent(), registry=replace(intent().registry, agent_pin=None), agent_entity_token=None),
        replace(intent(), agent_entity_token="entity-two"),
        intent("other-operation"),
    ):
        with pytest.raises(RemovalJournalConflict):
            journal.prepare(changed)
    assert journal.pending() == (first,)


@pytest.mark.parametrize("phase", [RemovalPhase.PREPARED, RemovalPhase.COMMITTED, RemovalPhase.COMPENSATING])
def test_pending_capacity_never_evicts_unsettled_intent(tmp_path, phase):
    journal = AdapterRemovalJournal(StateDatabase(tmp_path / "state.sqlite3"), capacity=1)
    journal.prepare(intent())
    if phase is not RemovalPhase.PREPARED:
        journal.advance("preview-one", RemovalPhase.PREPARED, phase)
    with pytest.raises(RemovalJournalConflict):
        journal.prepare(intent("other", "second"))
    assert journal.get("preview-one").phase is phase


def test_commit_is_a_one_way_boundary_and_terminal_receipt_is_retained(tmp_path):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journal = AdapterRemovalJournal(state, capacity=1)
    journal.prepare(intent())
    committed = journal.advance("preview-one", RemovalPhase.PREPARED, RemovalPhase.COMMITTED)
    assert journal.advance("preview-one", RemovalPhase.PREPARED, RemovalPhase.COMMITTED) == committed
    with pytest.raises(RemovalJournalConflict):
        journal.advance("preview-one", RemovalPhase.COMMITTED, RemovalPhase.COMPENSATING)
    completed = journal.advance("preview-one", RemovalPhase.COMMITTED, RemovalPhase.COMPLETED)
    assert journal.pending() == ()
    assert journal.prepare(intent()) == completed
    journal.prepare(intent("second"))
    assert AdapterRemovalJournal(state).get("preview-one") == completed


def test_compensation_cannot_turn_into_forward_commit(tmp_path):
    journal = AdapterRemovalJournal(StateDatabase(tmp_path / "state.sqlite3"))
    journal.prepare(intent())
    journal.advance("preview-one", RemovalPhase.PREPARED, RemovalPhase.COMPENSATING)
    with pytest.raises(RemovalJournalConflict):
        journal.advance("preview-one", RemovalPhase.PREPARED, RemovalPhase.COMMITTED)
    journal.advance("preview-one", RemovalPhase.COMPENSATING, RemovalPhase.COMPENSATED)
    assert journal.pending() == ()
    assert journal.get("preview-one").phase is RemovalPhase.COMPENSATED


def test_parallel_prepares_have_one_owner_for_the_target(tmp_path):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journals = (AdapterRemovalJournal(state), AdapterRemovalJournal(state))

    def prepare(index):
        try:
            return journals[index].prepare(intent(f"operation-{index}"))
        except RemovalJournalConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(journals[0].pending()) == 1


def test_sql_failure_does_not_create_a_partial_intent(tmp_path):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journal = AdapterRemovalJournal(state)
    with state.transaction() as connection:
        connection.execute("CREATE TRIGGER deny_removal BEFORE INSERT ON adapter_removal_intents BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        journal.prepare(intent())
    assert journal.pending() == ()
    assert journal.get("preview-one") is None


def test_failed_commit_marker_leaves_prepared_preimage_available(tmp_path):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journal = AdapterRemovalJournal(state)
    prepared = journal.prepare(intent())
    with state.transaction() as connection:
        connection.execute("CREATE TRIGGER deny_marker BEFORE UPDATE ON adapter_removal_intents BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        journal.advance("preview-one", RemovalPhase.PREPARED, RemovalPhase.COMMITTED)
    assert AdapterRemovalJournal(state).pending() == (prepared,)


def test_corrupt_preimage_is_not_silently_dropped_on_recovery(tmp_path):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journal = AdapterRemovalJournal(state)
    journal.prepare(intent())
    with state.transaction() as connection:
        connection.execute("UPDATE adapter_removal_intents SET request_json = '{}' WHERE operation_id = 'preview-one'")
    with pytest.raises(ValueError, match="checksum mismatch"):
        AdapterRemovalJournal(state).pending()


def test_pin_recovery_requires_the_original_entity_identity(tmp_path):
    journal = AdapterRemovalJournal(StateDatabase(tmp_path / "state.sqlite3"))
    with pytest.raises(ValueError, match="requires its Agent entity token"):
        journal.prepare(replace(intent(), agent_entity_token=None))
    assert journal.pending() == ()


@pytest.mark.parametrize("phase", [RemovalPhase.PREPARED, RemovalPhase.COMMITTED])
def test_new_process_reads_the_durable_phase_without_advancing_it(tmp_path, phase):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journal = AdapterRemovalJournal(state)
    journal.prepare(intent())
    if phase is not RemovalPhase.PREPARED:
        journal.advance("preview-one", RemovalPhase.PREPARED, phase)
    root = Path(__file__).resolve().parents[4]
    env = isolated_environment(tmp_path / "child")
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + str(root)
    probe = subprocess.run([
        sys.executable, "-c",
        "from pathlib import Path; import sys; "
        "from hyprial.daemon.impl.state_db import StateDatabase; "
        "from hyprial.daemon.impl.operations.adapter_removal.journal import AdapterRemovalJournal; "
        "j=AdapterRemovalJournal(StateDatabase(Path(sys.argv[1]))); "
        "r,=j.pending(); assert r.phase.value==sys.argv[2]; "
        "assert r.intent.agent_entity_token=='entity-one'; "
        "assert r.intent.registry.harness.spec.name=='app'; "
        "assert r.intent.channels_before.endswith(b'\\n')",
        str(state.path), phase.value,
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    assert journal.get("preview-one").phase is phase


def test_target_index_must_match_immutable_intent(tmp_path):
    state = StateDatabase(tmp_path / "state.sqlite3")
    journal = AdapterRemovalJournal(state)
    journal.prepare(intent())
    with state.transaction() as connection:
        connection.execute("UPDATE adapter_removal_intents SET adapter='other'")
    with pytest.raises(ValueError, match="identity mismatch"):
        journal.pending()


@pytest.mark.parametrize("metadata", [" { } ", '{"b": 2, "a": 1}'])
def test_registration_json_normalization_preserves_idempotence(tmp_path, metadata):
    journal = AdapterRemovalJournal(StateDatabase(tmp_path / "state.sqlite3"))
    registration = replace(intent().registry.harness, spec=AdapterSpec("lark", "app", legacy_launch=metadata))
    original = replace(intent(), registry=replace(intent().registry, harness=registration))
    saved = journal.prepare(original)
    assert journal.prepare(original) == saved
    assert journal.get(original.operation_id) == saved
