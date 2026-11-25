"""S7a ``delivery.bodyRetentionMs`` bound into the S5 facade (phase A, I1).

Composition hands delivery a per-subject retention source; delivery itself
never imports cells.  The subject of a grant is the row's local agent: the
recipient of an inbox row, the sender of a sender copy.  Without a snapshot,
with a raising read or without a subject the 7 d code default applies.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hyprial.daemon.impl.composition.delivery_params as retention_module
from hyprial.daemon.impl.cells.params import (
    DELIVERY_BODY_RETENTION_MS,
    CellParamSnapshot,
    ParamSource,
)
from hyprial.daemon.impl.composition.delivery_params import (
    bind_body_retention_params,
    body_retention_source,
)
from hyprial.daemon.impl.delivery import (
    DEFAULT_BODY_RETENTION_MS,
    MIN_BODY_RETENTION_MS,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    BodyGrant,
)
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
)
from tests.support.actor_hosts import new_actor_host

HOUR = MIN_BODY_RETENTION_MS
READER = "agent:op:node-1:reader"
DRIVER = "agent:op:node-1:driver"
KEY = DELIVERY_BODY_RETENTION_MS.key


def _snapshot(*sources: ParamSource) -> CellParamSnapshot:
    aliases: dict[str, set[str]] = {}
    for source in sources:
        for ref in source.refs:
            aliases.setdefault(ref, {ref}).add(source.cell_id)
    return CellParamSnapshot(
        generation=1,
        owner="user:op",
        sources=sources,
        aliases={ref: frozenset(group) for ref, group in aliases.items()},
    )


def _agent_source(refs: set[str], value: object, cell: str = "c.agent") -> ParamSource:
    return ParamSource(cell, "agent", 1, frozenset(refs), {KEY: value})


def _answer(monkeypatch: pytest.MonkeyPatch, value: Any) -> list[object]:
    calls: list[object] = []

    def cell_params(app: object) -> Any:
        calls.append(app)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(retention_module, "cell_params", cell_params)
    return calls


def test_source_resolves_the_subjects_min_combined_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, _snapshot(
        _agent_source({READER}, 3 * HOUR),
        _agent_source({READER, DRIVER}, 2 * HOUR, cell="c.agent-2"),
        _agent_source({"agent:op:node-1:other"}, HOUR, cell="c.agent-3"),
    ))
    source = body_retention_source(SimpleNamespace())
    assert source(READER) == 2 * HOUR
    assert source(DRIVER) == 2 * HOUR
    assert source("agent:op:node-1:nobody") == DEFAULT_BODY_RETENTION_MS


def test_source_clamps_through_the_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, _snapshot(_agent_source({READER}, 1)))
    assert body_retention_source(SimpleNamespace())(READER) == HOUR


@pytest.mark.parametrize(
    "value", [None, RuntimeError("cell runtime down")], ids=["no-snapshot", "raises"]
)
def test_source_falls_back_to_seven_days(
    monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    _answer(monkeypatch, value)
    assert body_retention_source(SimpleNamespace())(READER) == DEFAULT_BODY_RETENTION_MS


@pytest.mark.parametrize("subject", [None, "", 7])
def test_source_never_resolves_without_a_subject(
    monkeypatch: pytest.MonkeyPatch, subject: Any
) -> None:
    calls = _answer(monkeypatch, _snapshot(_agent_source({READER}, HOUR)))
    assert body_retention_source(SimpleNamespace())(subject) == DEFAULT_BODY_RETENTION_MS
    assert calls == []


def test_binding_without_an_inbox_is_a_no_op() -> None:
    assert bind_body_retention_params(SimpleNamespace(_inbox=None)) is False
    assert bind_body_retention_params(SimpleNamespace()) is False


class _Events:
    def publish(self, event: object) -> None:
        pass


@pytest.fixture
def facade(tmp_path: Path) -> Iterator[DeliveryCustodyFacade]:
    path = tmp_path / "inbox.sqlite3"
    from hyprial.daemon.impl.inbox import MemoryDeliveryTransport

    authority = DeliveryCustodyFacade(
        DeliveryCustodyCoordinator(
            path, MemoryDeliveryTransport(), _Events(),
            actor_host=new_actor_host(),
        ),
        path,
    )
    try:
        yield authority
    finally:
        authority.close()


def _received(facade: DeliveryCustodyFacade, message_id: str) -> None:
    message = InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender=DRIVER,
        recipient=READER,
        payload=json.dumps({"message": "hello", "origin": {"kind": "agent"}}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )
    assert facade.receive(message).accepted
    assert facade.ack(READER, message_id).acknowledged


def _stored_retention(facade: DeliveryCustodyFacade, message_id: str) -> int:
    with closing(sqlite3.connect(facade._database)) as db:
        return int(db.execute(
            "SELECT body_retention_ms FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()[0])


def test_bound_facade_resolves_retention_for_the_rows_agent(
    monkeypatch: pytest.MonkeyPatch, facade: DeliveryCustodyFacade
) -> None:
    _answer(monkeypatch, _snapshot(
        _agent_source({READER}, 2 * HOUR), _agent_source({DRIVER}, HOUR, cell="c.a2"),
    ))
    app = SimpleNamespace(_inbox=facade)
    assert bind_body_retention_params(app) is True
    _received(facade, "m-1")
    (row,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    grant = BodyGrant(row.source, row.position, row.payload_sha256, "rec-1")
    assert facade.grant_body_release(grant)
    assert _stored_retention(facade, "m-1") == 2 * HOUR


def test_an_explicit_grant_retention_still_wins(
    monkeypatch: pytest.MonkeyPatch, facade: DeliveryCustodyFacade
) -> None:
    calls = _answer(monkeypatch, _snapshot(_agent_source({READER}, 2 * HOUR)))
    bind_body_retention_params(SimpleNamespace(_inbox=facade))
    _received(facade, "m-1")
    (row,) = facade.interaction_rows_after(SOURCE_INBOX, 0).rows
    grant = BodyGrant(row.source, row.position, row.payload_sha256, "rec-1",
                      retention_ms=5 * HOUR)
    assert facade.grant_body_release(grant)
    assert _stored_retention(facade, "m-1") == 5 * HOUR
    assert calls == []


def test_a_sender_copy_grant_resolves_retention_for_the_sender(
    monkeypatch: pytest.MonkeyPatch, facade: DeliveryCustodyFacade
) -> None:
    _answer(monkeypatch, _snapshot(
        _agent_source({READER}, 2 * HOUR), _agent_source({DRIVER}, 3 * HOUR, cell="c.a2"),
    ))
    assert bind_body_retention_params(SimpleNamespace(_inbox=facade)) is True
    outgoing = InboxMessage(
        message_id="m-out",
        conversation_id="conversation-1",
        sender=DRIVER,
        recipient=READER,
        payload=json.dumps({"message": "hello", "origin": {"kind": "agent"}}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )
    assert facade.submit(outgoing).accepted
    (row,) = facade.interaction_rows_after(SOURCE_SENDER_COPY, 0).rows
    assert row.message.sender == DRIVER
    grant = BodyGrant(row.source, row.position, row.payload_sha256, "rec-out")
    assert facade.grant_body_release(grant)
    with closing(sqlite3.connect(facade._database)) as db:
        stored = db.execute(
            "SELECT body_retention_ms FROM sender_copies WHERE rowid = ?", (row.position,)
        ).fetchone()[0]
    assert stored == 3 * HOUR


def test_grant_subject_is_the_recipient_of_inbox_rows_and_the_sender_of_copies() -> None:
    from hyprial.daemon.impl.delivery import InteractionRow, grant_subject

    message = SimpleNamespace(sender=DRIVER, recipient=READER)
    digest = "0" * 64
    assert grant_subject(InteractionRow(SOURCE_INBOX, 1, message, digest)) == READER
    assert grant_subject(InteractionRow(SOURCE_SENDER_COPY, 1, message, digest)) == DRIVER
    assert grant_subject(None) is None


def test_startup_binds_retention_after_uri_migration_and_before_recorder() -> None:
    import ast
    import inspect
    import textwrap

    from hyprial.daemon.impl.application.wiring.services import _WiringServicesMixin

    source = textwrap.dedent(
        inspect.getsource(_WiringServicesMixin._startup_adopt_domain_services)
    )
    calls = [
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "bind_body_retention_params" in calls
    assert calls.index("bind_body_retention_params") < calls.index(
        "compose_conversation_recorder"
    )
