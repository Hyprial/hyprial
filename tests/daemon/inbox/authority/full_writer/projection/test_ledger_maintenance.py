"""Retention-only ledger updates publish actual deletes without dropping writes."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

import pytest

from hyprial.daemon.impl.inbox.contracts.api import LinkLedgerCensor, LinkLedgerOpen
from hyprial.daemon.impl.inbox.contracts.ports import UpdateLinkLedgerCommand
from hyprial.daemon.impl.inbox.service import InboxService

from ..support import authority as authority
from .test_no_change import _observe_global_reads, _stored, _wait_for_restart


def _row(key, state="open", expires=10):
    return LinkLedgerOpen("actor", key, "peer", "request", None, state, 1, expires)


def _update(**kwargs):
    return UpdateLinkLedgerCommand(correlation_id="maintenance-test", now_ms=100, **kwargs)


@pytest.mark.parametrize("cutoff", [None, 0, 100])
def test_no_match_retention_does_not_publish(authority, monkeypatch, cutoff):
    facade, coordinator, _, _ = authority
    facade.update_link_ledger(_update(open_outbound=(
        _row("active"), _row("retained", state="closed", expires=1000),
    )))
    before, version = _stored(coordinator), facade.version
    reads = _observe_global_reads(monkeypatch, coordinator)
    for _ in range(3):
        assert not any(facade.update_link_ledger(_update(purge_before_ms=cutoff)).as_mapping().values())
    assert _stored(coordinator) == before
    assert (facade.version, len(reads)) == (version, 0)


@pytest.mark.parametrize("state", ["closed", "acked", "expired", "drained", "released"])
def test_retention_deletes_only_expired_terminal_rows_and_wakes(authority, state):
    facade, _, _, _ = authority
    facade.update_link_ledger(_update(open_outbound=(
        _row("old", state), _row("active"), _row("future", state, expires=1000),
    )))
    version = facade.version
    seen, started = [], threading.Event()

    def wait():
        started.set()
        seen.append(facade.wait_for_version(version, 2))

    thread = threading.Thread(target=wait)
    thread.start()
    assert started.wait(1)
    result = facade.update_link_ledger(_update(purge_before_ms=100))
    thread.join(1)
    assert not thread.is_alive() and seen == [version + 1]
    assert not any(result.as_mapping().values())
    assert {row.message_id for row in facade.link_ledger_snapshot(0)} == {"active", "future"}
    facade.update_link_ledger(_update(purge_before_ms=100))
    assert facade.version == version + 1


@pytest.mark.parametrize("operands", [
    {"release": ("missing",)}, {"reasons": {"missing": "hold"}},
    {"fail": {"missing": "failure"}}, {"drain": {"actor": ("missing",)}},
    {"cancel_outbound": {"missing": "cancelled"}},
    {"release_outbound": (("actor", "missing"),)},
    {"censor": (LinkLedgerCensor("actor", "missing", 20, 30),)},
    {"sampled": (("actor", "missing"),)}, {"open_outbound": (_row("opened"),)},
    {"close_inbound": (("actor", "missing", 20),)},
    {"bind_tasks": (("actor", "peer", "task", 20),)},
    {"unbind_tasks": (("actor", "peer", "task"),)},
])
def test_other_ledger_operands_keep_existing_publication(authority, operands):
    facade, _, _, _ = authority
    version = facade.version
    facade.update_link_ledger(_update(**operands))
    assert facade.version == version + 1
    if "bind_tasks" in operands:
        assert facade.link_task_bindings() == operands["bind_tasks"]


def test_unrecognized_ledger_operand_remains_conservative(authority):
    @dataclass(frozen=True, slots=True)
    class FutureCommand(UpdateLinkLedgerCommand):
        future_operation: bool = False

    facade, _, _, _ = authority
    version = facade.version
    facade.update_link_ledger(FutureCommand(correlation_id="future", purge_before_ms=100))
    assert facade.version == version + 1


def test_failed_retention_commit_does_not_publish(authority):
    facade, coordinator, _, _ = authority
    facade.update_link_ledger(_update(open_outbound=(_row("old", "closed"),)))
    with sqlite3.connect(coordinator._database) as connection:
        connection.executescript("""
            CREATE TRIGGER reject_retention BEFORE DELETE ON link_ledger
            BEGIN SELECT RAISE(ABORT, 'injected retention rollback'); END;
        """)
    before, version = _stored(coordinator), facade.version
    with pytest.raises(RuntimeError, match="DELIVERY_COMMAND_FAILED"):
        facade.update_link_ledger(_update(purge_before_ms=100))
    _wait_for_restart(coordinator)
    assert _stored(coordinator) == before and facade.version == version


def test_retention_commit_is_published_before_later_failure(authority, monkeypatch):
    facade, coordinator, _, _ = authority
    facade.update_link_ledger(_update(open_outbound=(_row("old", "closed"),)))
    version = facade.version
    original = InboxService.update_link_ledger

    def fail_after_commit(self, command):
        original(self, command)
        raise RuntimeError("injected after retention commit")

    monkeypatch.setattr(InboxService, "update_link_ledger", fail_after_commit)
    with pytest.raises(RuntimeError, match="DELIVERY_COMMAND_FAILED"):
        facade.update_link_ledger(_update(purge_before_ms=100))
    _wait_for_restart(coordinator)
    assert facade.link_ledger_snapshot(0) == ()
    assert facade.version == version + 1
