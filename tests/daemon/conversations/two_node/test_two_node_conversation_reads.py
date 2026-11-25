"""Probe GC0: ``conversations.*`` on a second node for a conversation recorded on the first.

Pins today's behaviour (2026-11-18); write-up:
``docs/design/chat-space-two-node-probe-2026-11-18.md``.

Decision notes (slice GC0):

- Node B is a golden daemon whose conversation placement reads node B's
  OrgFS runtime, which is how a started daemon reads its composed runtime.
  Its delivery queue never saw the messages, so whatever it answers comes
  from the record.
- Node A is a bare OrgFS runtime of the same owner.  It writes the record
  lines with the real ``SegmentStore`` into the owner home the real
  ``CellSpaces`` rule picks, under its own writer id.  A's recorder (queue
  to record) is not driven here; it is covered single-node by the RM1 tests.
- The golden harness pins one node name for its agents, so B's agents carry
  the same addresses as the ones A recorded.  That stands in for "the same
  conversation, seen from a second machine"; per-node agent addresses are
  not modelled.
- ``conversations.list`` finds cells written by another node only through
  the periodic sweep (``SWEEP_S``) or after a history read of that cell;
  the test drives the read model's clock instead of waiting for the sweep.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.daemon.impl.conversations.document import SWEEP_S
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX
from hyprial.daemon.impl.conversations.keys import direct_key
from tests.daemon.conversations.document.document_support import caller, cell_of, ids
from tests.daemon.conversations.two_node.support import (
    NODE_A,
    announce_until_held,
    close,
    entry,
    eventually,
    record,
    start_pair,
    updates_dropped,
)
from tests.support.golden.app import ALPHA, BETA, golden_daemon as golden_daemon

LEGACY = "conv-two-node"


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _lines(cell: str, message_id: str, at_ms: int) -> list[Any]:
    key = direct_key(ALPHA, BETA)
    thread = None if key is None else key.id
    assert thread is not None
    sent = entry(cell, message_id, at_ms, sender=ALPHA, recipient=BETA,
                 conversation_id=LEGACY, thread_id=thread)
    received = sent.__class__(**{
        **{name: getattr(sent, name) for name in sent.__dataclass_fields__},
        "key": f"{SOURCE_INBOX}:{at_ms}:{message_id}", "source": SOURCE_INBOX,
    })
    return [sent, received]


def _reader_daemon(golden_daemon, monkeypatch, fs: Any) -> Any:
    daemon = golden_daemon()
    runtime = SimpleNamespace(facade=fs, close=lambda: None)
    monkeypatch.setattr(daemon.app, "_require_orgfs_runtime", lambda: runtime, raising=False)
    monkeypatch.setattr(daemon.app, "_orgfs_runtime", runtime, raising=False)
    assert compose_conversation_recorder(
        daemon.app, fs=fs, start=False, clock_ms=daemon.clock.ms
    ) is not None
    return daemon.app


def _history(app: Any, cell: str) -> dict[str, Any]:
    return METHOD_REGISTRY.invoke(app, "conversations.history", {**caller(ALPHA), "conversationId": cell})


def _listed(app: Any) -> set[str]:
    page = METHOD_REGISTRY.invoke(app, "conversations.list", caller(ALPHA))
    assert page["recordAvailable"] is True
    return {head["conversationId"] for head in page["conversations"]}


def test_second_node_reads_first_nodes_conversation_only_after_an_explicit_join(
    tmp_path, golden_daemon, monkeypatch
) -> None:
    _bus, a, b = start_pair(tmp_path)
    try:
        reader = _reader_daemon(golden_daemon, monkeypatch, b.fs)
        cell = cell_of(ALPHA, BETA, LEGACY)
        home = record(a, cell, _lines(cell, "m1", 1))
        announce_until_held(a, home, b)
        b.presence.emit_online(NODE_A)

        before = _history(reader, cell)
        assert before["recordAvailable"] is True
        assert ids(before) == []
        assert cell not in _listed(reader)

        b.fs.join(home)

        assert cell not in _listed(reader)
        documents = reader._conversation_documents
        swept_at = documents._swept_at
        documents._clock = lambda: swept_at + SWEEP_S
        assert cell in _listed(reader)
        assert ids(_history(reader, cell)) == ["m1"]
    finally:
        close(b, a)


def test_second_node_reads_a_later_first_node_write_once_it_holds_the_home(
    tmp_path, golden_daemon, monkeypatch
) -> None:
    _bus, a, b = start_pair(tmp_path)
    try:
        reader = _reader_daemon(golden_daemon, monkeypatch, b.fs)
        cell = cell_of(ALPHA, BETA, LEGACY)
        home = record(a, cell, _lines(cell, "m1", 1))
        announce_until_held(a, home, b)
        b.fs.join(home)
        assert ids(_history(reader, cell)) == ["m1"]

        record(a, cell, _lines(cell, "m2", 2), seq=0)

        eventually(lambda: _expect_ids(reader, cell, ["m1", "m2"]))
    finally:
        close(b, a)


def test_a_history_read_on_the_second_node_puts_the_cell_into_its_list(
    tmp_path, golden_daemon, monkeypatch
) -> None:
    _bus, a, b = start_pair(tmp_path)
    try:
        reader = _reader_daemon(golden_daemon, monkeypatch, b.fs)
        cell = cell_of(ALPHA, BETA, LEGACY)
        home = record(a, cell, _lines(cell, "m1", 1))
        announce_until_held(a, home, b)
        assert cell not in _listed(reader)
        b.fs.join(home)
        assert cell not in _listed(reader)

        assert ids(_history(reader, cell)) == ["m1"]
        assert cell in _listed(reader)
    finally:
        close(b, a)


def test_second_node_history_misses_a_lost_live_update_until_the_online_edge(
    tmp_path, golden_daemon, monkeypatch
) -> None:
    bus, a, b = start_pair(tmp_path)
    try:
        reader = _reader_daemon(golden_daemon, monkeypatch, b.fs)
        cell = cell_of(ALPHA, BETA, LEGACY)
        home = record(a, cell, _lines(cell, "m1", 1))
        announce_until_held(a, home, b)
        b.fs.join(home)
        assert ids(_history(reader, cell)) == ["m1"]

        bus.drop_updates = True
        record(a, cell, _lines(cell, "m2", 2), seq=0)
        eventually(lambda: updates_dropped(bus, 1))
        bus.drop_updates = False
        assert ids(_history(reader, cell)) == ["m1"]

        b.presence.emit_online(NODE_A)
        eventually(lambda: _expect_ids(reader, cell, ["m1", "m2"]))
    finally:
        close(b, a)


def _expect_ids(app: Any, cell: str, expected: list[str]) -> None:
    assert ids(_history(app, cell)) == expected
