"""G6: typed envelope decoding fails explicitly and never interprets reset."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner
from jsonschema import Draft202012Validator

from hyprial.identity import PacError
from hyprial.daemon.impl.pac.storage.journal import EVENT_TYPES, EventEnvelope, append_event
from hyprial.daemon.impl.pac.graphs.edits import close_graph
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.graphs.projection import ProjectionState, fold_event
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.graphs.subscription import events_since
from tests.support.pac.builders import build_allen_graph, principal_uri
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture()
def emitted():
    store, gid = build_allen_graph(Path(os.environ["HARNESS_STATE_DIR"]), principal="hyprial-unit-owner")
    PacReactor(store, clock=lambda: 100).set_flag(gid, "feat-collector", actor=principal_uri("feat-bot"), reason_ref="artifact#result")
    event = next(e for e in events_since(store.path, gid, 0)["events"] if e["type"] == "flag_set")
    yield store, gid, event
    store.close()


def test_every_event_type_is_pinned_by_the_public_contract():
    schema = json.loads((Path(__file__).resolve().parents[4] / 'contract/pac/events/schema.json').read_text())
    assert set(EVENT_TYPES) == set(schema["$defs"]["event"]["properties"]["type"]["enum"])


def test_all_public_tags_round_trip_against_schema_with_real_outcome_payloads(emitted, tmp_path):
    import sqlite3

    store, gid, _ = emitted
    reactor = PacReactor(store, sender=NullSender())
    reactor.resend_undelivered(gid)
    reactor.reset_flag(gid, "feat-collector", actor=principal_uri("feat-bot"))
    close_graph(store, gid, actor=principal_uri("hyprial-unit-owner"))
    events = events_since(store.path, gid, 0)["events"]
    by_type = {event["type"]: event for event in events}
    # Import the actual original-implementation fixture for a real baseline.
    old = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(old)
    connection.executescript((Path(__file__).resolve().parents[4] / 'tests/fixtures/pac/slice-1-real.sql').read_text())
    connection.close()
    legacy = PacGraphStore(old)
    try:
        baseline = legacy.journal_events(legacy._db.execute("SELECT graph_id FROM graphs").fetchone()[0])[-1]
        baseline["journalId"] = legacy._db.execute("SELECT journal_id FROM journal_meta").fetchone()[0]
        by_type["migration_baseline"] = baseline
    finally:
        legacy.close()
    # A real graph workflow produces its request event on the public journal.
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    workflow = GraphWorkflowService(state_dir=tmp_path, owner="test", machine="node",
                                    sender=NullSender(), clock_ms=lambda: 1000, start_thread=False)
    try:
        graph = workflow.start(yaml_text="version: 2\nname: envelope\nnodes: [{id: work, owner: user:test, task: Check.}]\n", sender="user:test")["graphId"]
        workflow._tick()
        by_type["workflow_changed"] = next(e for e in events_since(workflow.database, graph, 0)["events"] if e["type"] == "workflow_changed")
    finally:
        workflow.close()
    # Reserved lifecycle types exercise ENVELOPE ONLY, with opaque empty data.
    # They are not launcher/down response stubs or lifecycle acceptance evidence.
    reserved = {"launch_failed", "actor_up", "actor_down", "actor_restored", "actor_lost", "actor_unowned"}
    assert set(EVENT_TYPES) - set(by_type) == reserved
    cursor = events[-1]["seq"]
    with store.write() as db:
        for kind in sorted(reserved):
            append_event(db, graph_id=gid, version=store.graph(gid)["version"], type=kind, at=100, data={})
    by_type.update({event["type"]: event for event in events_since(store.path, gid, cursor)["events"]})
    schema = json.loads((Path(__file__).resolve().parents[4] / 'contract/pac/events/schema.json').read_text())
    validator = Draft202012Validator(schema)
    assert set(by_type) == set(EVENT_TYPES)
    for event in by_type.values():
        restored = EventEnvelope.from_json(json.loads(json.dumps(event))).to_json()
        assert restored == event
        validator.validate(restored)


def test_actual_public_envelope_round_trips_without_reinterpreting_payload(emitted):
    _store, _gid, event = emitted
    typed = EventEnvelope.from_json(json.loads(json.dumps(event)))
    assert typed.type == "flag_set" and typed.data["reasonRef"] == "artifact#result"
    assert json.loads(json.dumps(typed.to_json())) == event


@pytest.mark.parametrize("field,value", [
    ("schemaVersion", 2), ("schemaVersion", True), ("seq", True), ("seq", 0),
    ("version", 0), ("at", "yesterday"), ("eventId", ""), ("graphId", None),
    ("journalId", None), ("data", []),
])
def test_invalid_envelope_has_stable_error(emitted, field, value):
    _store, _gid, event = emitted
    with pytest.raises(PacError) as raised:
        EventEnvelope.from_json({**event, field: value})
    assert raised.value.code == "PAC_EVENT_ENVELOPE_INVALID"


def test_unknown_type_cannot_fall_through_to_flag_reset(emitted):
    store, gid, event = emitted
    changed = {**event, "type": "unknown_outcome"}  # corruption of actual output, not a launcher stub
    with pytest.raises(PacError) as raised:
        EventEnvelope.from_json(changed)
    assert raised.value.code == "PAC_EVENT_TYPE_UNKNOWN"
    state = ProjectionState(flags={"feat-collector": True})
    with pytest.raises(ValueError, match="explicit set/reset"):
        fold_event(state, {}, changed)
    assert state.flags == {"feat-collector": True} and state.events == []
    assert store.node(gid, "feat-collector").flag is True


@pytest.mark.parametrize("kind,data,code", [
    ("future_outcome", "{}", "PAC_EVENT_TYPE_UNKNOWN"),
    ("actor_lost", "not-json", "PAC_EVENT_ENVELOPE_INVALID"),
])
def test_corrupt_persisted_envelope_reports_json_error_without_mutating_flags(emitted, kind, data, code):
    store, gid, event = emitted
    cursor = store.journal_events(gid)[-1]["seq"]
    before = [node.to_json() for node in store.nodes(gid)]
    # Deliberately bypass SQLite CHECK for fault injection only. Normal writes
    # reject unknown types before SQL, and the table CHECK also rejects them.
    store._db.execute("PRAGMA ignore_check_constraints=ON")
    try:
        with store.write() as db:
            db.execute("INSERT INTO journal(event_id,graph_id,version,type,at,data_json) VALUES (?,?,?,?,?,?)",
                       ("injected-corruption", gid, event["version"], kind, 101, data))
    finally:
        store._db.execute("PRAGMA ignore_check_constraints=OFF")
    result = CliRunner().invoke(_cli_root_app, ["workflow", "events", gid, "--after", str(cursor), "--json"])
    assert result.exit_code == 1 and result.stdout == ""
    assert json.loads(result.stderr)["code"] == code
    assert [node.to_json() for node in store.nodes(gid)] == before
