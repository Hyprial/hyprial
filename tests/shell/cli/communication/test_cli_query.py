"""`hyprial message inbox` / `message outbox --actor` read and change nothing.

The consumer surface, ``message.pending.list``, drains the actor's system
notices on every call -- with or without the fetch marker.  A person "just
looking" through it would eat notices meant for the agent.  ``message.query``
must leave the agent's next read exactly as it would have been.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate, permissive_admission_slot
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

READER = "agent:op:node-1:reader"


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


def _app(tmp_path: Path) -> DaemonApplication:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    # Explicit permissive premise: the seeded rows are agent-to-agent and this
    # test is about message.query, not link admission (review item 3).
    app._inbox = InboxService(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        link_admission=permissive_admission_slot(),
    )
    app.link_admission.bind(PermissiveLinkGate())
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()
    return app


def _message(
    message_id: str, *, sender: str, recipient: str, text: str, intent: str = "request"
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"c-{message_id}",
        sender=sender,
        recipient=recipient,
        payload=json.dumps({"message": text}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _seed(app: DaemonApplication) -> None:
    app._inbox.receive(
        _message(
            "m-1", sender="agent:op:node-1:asker", recipient=READER, text="hello reader"
        )
    )
    assert app._inbox.receive_system_notice(
        _message(
            "n-1",
            sender="system",
            recipient=READER,
            text="delivery failed",
            intent="system",
        )
    )


def test_inbox_shows_pending_messages_and_notices_with_text(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _seed(app)
    result = app.handle("message.query", {"from": READER, "view": "inbox"})
    assert result["ok"] is True
    assert result["view"] == "inbox"
    rows = {row["messageId"]: row for row in result["entries"]}
    assert rows["m-1"]["kind"] == "message"
    assert rows["m-1"]["message"] == "hello reader"
    assert rows["m-1"]["from"] == "agent:op:node-1:asker"
    assert rows["n-1"]["kind"] == "notice"
    assert rows["n-1"]["message"] == "delivery failed"


def test_query_changes_nothing_the_agent_will_read(tmp_path: Path) -> None:
    """The whole point: query twice, then the agent's own read is unchanged."""

    app = _app(tmp_path)
    _seed(app)
    app.handle("message.query", {"from": READER, "view": "inbox"})
    app.handle("message.query", {"from": READER, "view": "inbox"})
    assert [notice.message_id for notice in app._inbox.system_notices(READER)] == [
        "n-1"
    ]
    consumer = app.handle("message.pending.list", {"from": READER})
    assert {row["messageId"] for row in consumer["messages"]} == {"m-1", "n-1"}


def test_the_consumer_surface_peeks_the_notice_until_an_explicit_ack(
    tmp_path: Path,
) -> None:
    """Doors bells never drain: the same notice survives a list observation.

    Design (contract/wire/inbox-batch-v1.md): only the per-id ack (or the batch
    read's fetched stamp) consumes a notice, so a poll cannot delete it before
    the batch snapshot includes it.
    """

    app = _app(tmp_path)
    _seed(app)
    app.handle("message.pending.list", {"from": READER})
    app.handle("message.pending.list", {"from": READER})
    assert [n.message_id for n in app._inbox.system_notices(READER)] == ["n-1"]
    app.handle("message.ack", {"from": READER, "messageId": "n-1"})
    assert app._inbox.system_notices(READER) == ()


def test_outbox_lists_only_this_actors_queued_sends(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app._inbox.submit(
        _message(
            "o-1",
            sender=READER,
            recipient="agent:op:other-node:peer",
            text="are you there",
        ),
        now_ms=0,
    )
    app._inbox.submit(
        _message(
            "o-2",
            sender="agent:op:node-1:someone-else",
            recipient="agent:op:other-node:peer",
            text="not mine",
        ),
        now_ms=0,
    )
    result = app.handle("message.query", {"from": READER, "view": "outbox"})
    ids = [row["messageId"] for row in result["entries"]]
    assert ids == ["o-1"]
    # F7: the query outbox view serializes EXACTLY like `outbox list` --
    # one entry function, so the field set is the list schema (no
    # query-specific kind/from/to/message columns anymore).
    row = result["entries"][0]
    assert set(row) == {
        "messageId",
        "conversationId",
        "sender",
        "recipient",
        "intent",
        "createdAtMs",
        "attempts",
        "nextAttemptMs",
        "expiresAtMs",
        "undeliverableScheme",
        "failedAttempts",
    }
    assert row["recipient"] == "agent:op:other-node:peer"


def test_outbox_two_entries_serialize_identically(tmp_path: Path) -> None:
    """F7 判据:同一封 outbox,两个入口逐字段一致。

    A7 的问题就是"同一数据源两套序列化";这条把"一份"钉死 -- 抹掉任何
    一侧改字段的变异,本测试必须红。
    """

    app = _app(tmp_path)
    app._inbox.submit(
        _message("o-1", sender=READER, recipient="agent:op:other-node:peer", text="x"),
        now_ms=0,
    )
    listed = app.handle("outbox.list", {})
    queried = app.handle("message.query", {"from": READER, "view": "outbox"})
    assert listed["entries"] == queried["entries"]


def test_unknown_view_is_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with pytest.raises(DaemonRequestError):
        app.handle("message.query", {"from": READER, "view": "everything"})


def test_cli_inbox_sends_the_read_only_method(monkeypatch) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def fake(method: str, params: dict[str, object], **_: object) -> dict[str, object]:
        seen.append((method, params))
        return {"ok": True, "actor": READER, "view": "inbox", "entries": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    result = CliRunner().invoke(_cli_root_app, ["message", "inbox", "reader", "--json"])
    assert result.exit_code == 0, result.output
    assert seen == [("message.query", {"from": "reader", "view": "inbox"})]


def test_cli_outbox_without_actor_reads_the_whole_outbox(monkeypatch) -> None:
    seen: list[str] = []

    def fake(method: str, *_args: object, **_kwargs: object) -> dict[str, object]:
        seen.append(method)
        return {"ok": True, "entries": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    result = CliRunner().invoke(_cli_root_app, ["message", "outbox", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert seen == ["outbox.list"]


def test_cli_outbox_actor_filters_through_the_read_only_query(monkeypatch) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def fake(method: str, params: dict[str, object], **_: object) -> dict[str, object]:
        seen.append((method, params))
        return {"ok": True, "actor": READER, "view": "outbox", "entries": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    result = CliRunner().invoke(
        _cli_root_app, ["message", "outbox", "list", "--actor", "reader", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [("message.query", {"from": "reader", "view": "outbox"})]
