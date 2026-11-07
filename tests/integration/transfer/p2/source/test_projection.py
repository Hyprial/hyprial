"""Read-only custody uses the short journal lane, not SQLite in a mailbox."""

import threading
import inspect
import json
from pathlib import Path

from hyprial.daemon.impl.transfer.p2.actor import AdmissionActor
from hyprial.daemon.impl.transfer.p2.projection import CustodyProjection, CustodyQuery, SourceCustody


def test_custody_hydration_distinguishes_missing_pending_and_held(ledger, binding):
    store, _, _ = ledger
    authority = AdmissionActor(store)
    try:
        query = getattr(authority, "source_custody", None)
        assert callable(query), "P2 has no read-only source custody projection"
        token = binding.source_entity_token
        assert query("source", expected_entity_token=token).state == "unavailable"
        first = authority.hydrate_custody("source", expected_entity_token=token).result(5)
        assert first.state == "clear"
        store.prepare("move", binding)
        start = store.journal.begin("move", step="quiesce", fingerprint="fp", actor="source")
        pending = authority.hydrate_custody("source", expected_entity_token=token).result(5)
        assert pending.state == "unavailable", "intent is not a proven hold ack"
        assert pending.operations == ("move",)
        assert [(hold.operation_id, hold.generation) for hold in pending.holds] == [("move", 1)]
        assert pending.journal_version > first.journal_version
        store.journal.finish("move", expected=start.state, receipt="stopped", entity_token=token)
        held = authority.hydrate_custody("source", expected_entity_token=token).result(5)
        assert held.state == "held"
        assert held.holds == pending.holds
        assert held.journal_version > pending.journal_version
        assert held.version > pending.version
        replacement = authority.hydrate_custody("source", expected_entity_token="replacement").result(5)
        assert replacement.state == "clear"
    finally:
        assert authority.close(timeout=5)


def test_custody_query_is_pure_memory_and_failed_refresh_is_unavailable(ledger, monkeypatch):
    store, _, database = ledger
    authority = AdmissionActor(store)
    try:
        query = getattr(authority, "source_custody", None)
        assert callable(query), "P2 has no read-only source custody projection"
        first = authority.hydrate_custody("source", expected_entity_token="token").result(5)
        assert first.state == "clear"

        def broken():
            raise OSError("database unavailable")

        monkeypatch.setattr(database, "read", broken)
        assert query("source", expected_entity_token="token") == first
        failed = authority.hydrate_custody("source", expected_entity_token="token").result(5)
        assert failed.state == "unavailable"
        assert query("source", expected_entity_token="token") == failed
    finally:
        assert authority.close(timeout=5)


def test_old_completion_cannot_overwrite_newer_unavailability():
    projection = CustodyProjection()
    old = SourceCustody("source", "token", "clear", reason="no-record", journal_version=2)
    newer = SourceCustody("source", "token", reason="query-failed")
    published = projection.publish(newer, sequence=2)
    assert projection.publish(old, sequence=1) == published
    assert projection.read(CustodyQuery("source", "token")).state == "unavailable"


def test_pending_refresh_does_not_keep_an_old_clear_authorization(ledger, monkeypatch):
    store, _, _ = ledger
    authority = AdmissionActor(store)
    entered, release = threading.Event(), threading.Event()
    try:
        first = authority.hydrate_custody("source", expected_entity_token="token").result(5)
        assert first.state == "clear"
        original = store.journal.custody_snapshot

        def blocked(actor, token):
            entered.set()
            assert release.wait(5)
            return original(actor, token)

        monkeypatch.setattr(store.journal, "custody_snapshot", blocked)
        future = authority.hydrate_custody("source", expected_entity_token="token")
        assert entered.wait(5)
        assert authority.source_custody("source", expected_entity_token="token").state == "unavailable"
        assert future.cancel(), "observer cancellation should not cancel accepted custody"
        release.set()
    finally:
        release.set()
        assert authority.close(timeout=5)


def test_custody_budgets_match_declared_contract():
    from hyprial.daemon.impl.application.actors.transfer_p2 import SOURCE_CUSTODY_QUERY_SECONDS

    root = Path(__file__).resolve().parents[5]
    bounds = json.loads((root / "contract/daemon-lifecycle/p2-source-bounds.json").read_text())
    assert SOURCE_CUSTODY_QUERY_SECONDS == bounds["source_custody_query_seconds"]
    assert inspect.signature(CustodyProjection).parameters["capacity"].default == (
        bounds["source_custody_projection_capacity"]
    )
