"""A session effect write must cost the same however large desired state is.

Production 0.5.0, 2026-10-05: recording or completing one pending session
effect rewrote every row of every desired-state table (DELETE all + INSERT
all), so each write cost ~0.5 s with ~630 lifecycle resources, and at one
heartbeat per second per session the single state writer saturated.  A
timing test cannot catch that on a small CI database; counting the rows a
write changes can, because a whole-document rewrite scales with the data.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import DesiredStateError
from hyprial.daemon.impl.desired_state.documents import PendingSessionAgentEffect
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import StoredLifecycleResource
from hyprial.daemon.impl.state_db import StateDatabase


def _store_with_resources(path: Path, count: int) -> DesiredStateStore:
    store = DesiredStateStore(path / "desired.json", StateDatabase(path / "state.sqlite3"))
    resources = tuple(
        StoredLifecycleResource(
            domain="harness",
            resource_key=f"harness:pi:old-{index}",
            resource_token=f"token-{index}",
            active=False,
            payload={},
        )
        for index in range(count)
    )
    store.save(replace(store.load(), lifecycle_resources=resources))
    return store


def _changed_rows(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Rows changed by each write transaction, via the connection's counter."""

    changes: list[int] = []
    original = StateDatabase.transaction

    @contextlib.contextmanager
    def counted(self, schema=None):  # type: ignore[no-untyped-def]
        with original(self, schema) as db:
            before = db.total_changes
            yield db
            changes.append(db.total_changes - before)

    monkeypatch.setattr(StateDatabase, "transaction", counted)
    return changes


def _effect(index: int) -> PendingSessionAgentEffect:
    return PendingSessionAgentEffect(
        effect_id=f"effect-{index:04d}",
        correlation_id=f"session:heartbeat:{index}",
        operation="bind",
        actor="agent:op:node-1:worker",
        harness="claude",
        runtime="interactive",
        session_id="session-1",
    )


def _write_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resources: int) -> list[int]:
    store = _store_with_resources(tmp_path, resources)
    changes = _changed_rows(monkeypatch)
    store.record_session_agent_effects((_effect(1),))
    store.complete_session_agent_effect("effect-0001")
    monkeypatch.undo()
    return changes


def test_recording_and_completing_an_effect_does_not_scale_with_desired_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = _write_cost(tmp_path / "small", monkeypatch, 100)
    large = _write_cost(tmp_path / "large", monkeypatch, 1000)

    assert small == large, (small, large)
    # One row inserted, one row deleted.
    assert small == [1, 1], small


def test_row_written_effects_read_back_like_whole_document_ones(tmp_path: Path) -> None:
    store = _store_with_resources(tmp_path, 3)
    store.record_session_agent_effects((_effect(3), _effect(1)))
    store.record_session_agent_effects((_effect(2),))

    loaded = store.load().pending_session_agent_effects
    assert [effect.effect_id for effect in loaded] == ["effect-0001", "effect-0002", "effect-0003"]
    assert store.complete_session_agent_effect("effect-0002") is True
    assert store.complete_session_agent_effect("effect-0002") is False
    assert [e.effect_id for e in store.load().pending_session_agent_effects] == [
        "effect-0001",
        "effect-0003",
    ]


def test_a_fresh_home_still_gets_its_document_root(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json", StateDatabase(tmp_path / "state.sqlite3"))
    store.record_session_agent_effects((_effect(1),))
    assert [e.effect_id for e in store.load().pending_session_agent_effects] == ["effect-0001"]


def test_a_row_write_refuses_an_effect_the_document_write_would_refuse(tmp_path: Path) -> None:
    # Review 818 on #1144 (blocking): the whole-document write validated
    # every effect; a row write did not, so a bind without harness/runtime
    # was stored and every later load() raised -- unrecoverable on restart.
    store = _store_with_resources(tmp_path, 3)
    invalid = PendingSessionAgentEffect(
        effect_id="effect-bad",
        correlation_id="session:heartbeat:bad",
        operation="bind",
        actor="agent:op:node-1:worker",
    )
    with pytest.raises(DesiredStateError):
        store.record_session_agent_effects((invalid,))
    assert store.load().pending_session_agent_effects == ()


def test_a_refused_row_is_labelled_by_its_place_in_the_document(tmp_path: Path) -> None:
    # Follow-up to #1144: the label used the position in the call's argument
    # list, which names the wrong document entry once effects are merged.
    store = _store_with_resources(tmp_path, 3)
    store.record_session_agent_effects((_effect(1), _effect(3)))
    invalid = PendingSessionAgentEffect(
        effect_id="effect-0002",
        correlation_id="session:heartbeat:bad",
        operation="bind",
        actor="agent:op:node-1:worker",
    )
    with pytest.raises(DesiredStateError, match=r"pendingSessionAgentEffects\[1\]"):
        store.record_session_agent_effects((invalid,))


def test_a_replaced_database_gets_the_whole_document_not_orphan_rows(tmp_path: Path) -> None:
    # Follow-up to #1144: has_document() is cached once true.  If the state
    # database is replaced underneath (a restore), row inserts would land in
    # a database with no document root, and the effect would vanish on load.
    store = _store_with_resources(tmp_path, 3)
    store.record_session_agent_effects((_effect(1),))
    with store._sqlite_shadow._state_db.transaction() as db:
        db.execute("DELETE FROM root_state")

    store.record_session_agent_effects((_effect(2),))

    loaded = store.load().pending_session_agent_effects
    assert "effect-0002" in [effect.effect_id for effect in loaded]
