"""RED contract for one daemon-owned full-body inbox batch.

Session-liveness renewal (2026-11-03, workflow wf-2b3f130c #implDF, work item
0088): test_successor_has_no_predecessor_member_count_ceiling seeds 300
durable rows between session.register and the first message.pending.batch.
The interactive fence (_fence_live_principal_session) requires a fresh
carrier heartbeat (AGENT_HEARTBEAT_TTL_SECONDS = 12.0), and ordinary inbox
traffic deliberately never renews it. Under CI shard load the seeding can
exceed that TTL, so the batch was refused with STALE_SESSION ("interactive
session session-1 is not live"; red on #2327 run 45572, #2153 run 45880,
#2334 run 46037; passes locally). The product renewal verbs are not usable
here: session.heartbeat's route ensure dispatches pending rows and drained
the fixture inbox in a local slow-seed reproduction, which would defeat the
count-ceiling property under test. The fixture therefore renews the
liveness signal directly -- the same touch the register/bind path performs
-- stating the real state (a live carrier) the batch contract assumes.
Local slow-seed reproduction (50 ms per receive, ~15 s of seeding) failed
with the exact CI error before the fix and passes after it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import InboxService
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import ipc_errors
from tests.kernel.actors.identity.support import PI_WORKER, application as identity_application
from ...support import (
    LoopbackTransport,
    _close_application_domains as _close_application_domains,
    durable_application,
    register_actor,
)


def _wire_batch_error(app, *, request_id: str, params: dict[str, str]):  # type: ignore[no-untyped-def]
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    worker = threading.Thread(target=app._serve_client, args=(server,), daemon=True)
    worker.start()
    try:
        client.sendall(
            json.dumps(
                {
                    "version": 1,
                    "id": request_id,
                    "method": "message.pending.batch",
                    "params": params,
                }
            ).encode()
            + b"\n"
        )
        client.settimeout(5)
        response = json.loads(client.makefile("rb").readline())
        worker.join(timeout=5)
        assert not worker.is_alive()
        return response
    finally:
        client.close()


@pytest.fixture(autouse=True)
def _isolated_daemon_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "isolated.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")


def _message(
    *,
    message_id: str,
    sender: str = "agent:op:node-1:driver",
    intent: str = "request",
    summary: str | None = None,
    body: str | None = None,
    created_at_ms: int = 1,
    idempotency_key: str | None = None,
    recipient: str = "agent:op:node-1:reader",
) -> InboxMessage:
    text = body or f"body-{message_id}"
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conversation-{message_id}",
        sender=sender,
        recipient=recipient,
        payload=json.dumps({"message": text}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
        idempotency_key=idempotency_key,
        summary=summary or f"summary-{message_id}",
    )


def _reader(app) -> str:  # type: ignore[no-untyped-def]
    """The reader's official address: rows are queued at its id URI."""

    return app.agents.require("reader").uri


def _seed(
    app,  # type: ignore[no-untyped-def]
    *,
    message_id: str,
    sender: str = "agent:op:node-1:driver",
    intent: str = "request",
    summary: str | None = None,
    body: str | None = None,
    created_at_ms: int = 1,
    idempotency_key: str | None = None,
) -> None:
    result = app._inbox.receive(  # noqa: SLF001 - durable authority fixture seam
        _message(
            message_id=message_id,
            sender=sender,
            intent=intent,
            summary=summary,
            body=body,
            created_at_ms=created_at_ms,
            idempotency_key=idempotency_key,
            recipient=_reader(app),
        )
    )
    assert result.accepted is True


def _seed_notice(
    app, *, message_id: str, text: str | None = None, created_at_ms: int = 1
) -> None:  # type: ignore[no-untyped-def]
    accepted = app._inbox.receive_system_notice(  # noqa: SLF001
        InboxMessage(
            message_id=message_id,
            conversation_id=f"conversation-{message_id}",
            sender="system:hyprial:node-1",
            recipient=_reader(app),
            payload=json.dumps(
                {"message": text or f"notice-{message_id}", "systemNotice": True}
            ).encode(),
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            created_at_ms=created_at_ms,
            summary=text or f"notice-{message_id}",
        )
    )
    assert accepted is True


def _batch(
    app, *, cursor: str | None = None, after_cursor: str | None = None
):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {
        "actor": "reader",
        "sessionRef": "session-1",
    }
    if cursor is not None:
        params["cursor"] = cursor
    if after_cursor is not None:
        params["afterCursor"] = after_cursor
    assert not (cursor is not None and after_cursor is not None)
    return app.handle("message.pending.batch", params)


@pytest.mark.parametrize("actor", ["dispatch-coordinator", "_turn-hooks"])
def test_registered_batch_preserves_reserved_actor_error_over_real_ipc(
    tmp_path: Path, actor: str
) -> None:
    app = durable_application(tmp_path / actor)
    params = {"actor": actor, "sessionRef": "synthetic-session"}
    with pytest.raises(DaemonRequestError) as direct:
        app._ipc_message_pending_batch(  # noqa: SLF001
            "message.pending.batch", params, _trusted_message_origin="ipc"
        )
    assert direct.value.code == "INVALID_AGENT_NAME"

    response = _wire_batch_error(app, request_id="reserved-actor", params=params)
    assert response["error"]["code"] == "INVALID_AGENT_NAME"


def test_registered_batch_preserves_operator_provenance_over_real_ipc(
    tmp_path: Path,
) -> None:
    app = durable_application(tmp_path / "provenance")
    app.handle("agent.create", {"name": "worker-b"})
    register_actor(app, "reader")
    # Registered agents are addressed at their official (runtime-minted)
    # address; the URI minted from the name is retired under id-form minting.
    reader = app.agents.require("reader").uri
    app.handle(
        "message.send",
        {
            "from": "user:op",
            "onBehalfOf": "worker-b",
            "to": [reader],
            "summary": "operator relay",
            "message": "operator relay",
        },
        _trusted_message_origin="ipc",
    )
    response = _wire_batch_error(
        app,
        request_id="operator-provenance",
        params={"actor": "reader", "sessionRef": "session-1"},
    )
    assert response["result"]["items"][0]["origin"] == {
        "via": "operator",
        "onBehalfOf": app.agents.require("worker-b").uri,
    }


def test_registered_batch_rejects_forged_trusted_origin_parameter(
    tmp_path: Path,
) -> None:
    app = durable_application(tmp_path / "forged-origin")
    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "message.pending.batch",
            {
                "actor": "reader",
                "sessionRef": "session-1",
                "_trusted_message_origin": "ipc",
            },
            _trusted_message_origin="ipc",
        )
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_registered_batch_preserves_unresolved_sender_error_over_real_ipc(
    tmp_path: Path,
) -> None:
    app = identity_application(tmp_path / "unresolved")
    try:
        params = {"from": "user:op", "onBehalfOf": "nobody-here"}
        with pytest.raises(DaemonRequestError) as direct:
            app._ipc_message_pending_batch(  # noqa: SLF001
                "message.pending.batch", params, _trusted_message_origin="ipc"
            )
        assert direct.value.code == ipc_errors.SENDER_UNRESOLVED
        assert direct.value.data == {"sender": "nobody-here"}

        response = _wire_batch_error(
            app, request_id="unresolved-sender", params=params
        )
        assert response["error"]["code"] == ipc_errors.SENDER_UNRESOLVED
        assert response["error"]["data"] == direct.value.data
    finally:
        app._close()  # noqa: SLF001
        app._close_home_lease()  # noqa: SLF001


def test_registered_batch_preserves_ambiguous_target_error_over_real_ipc(
    tmp_path: Path,
) -> None:
    provider = dict(PI_WORKER)
    provider["name"] = "moved"
    provider["pinnedOwner"] = "someone-else"
    app = identity_application(tmp_path / "ambiguous", providers=(provider,))
    try:
        app.handle("agent.create", {"name": "moved"})
        params = {"from": "user:op", "onBehalfOf": "moved"}
        with pytest.raises(DaemonRequestError) as direct:
            app._ipc_message_pending_batch(  # noqa: SLF001
                "message.pending.batch", params, _trusted_message_origin="ipc"
            )
        assert direct.value.code == ipc_errors.AMBIGUOUS_TARGET
        assert direct.value.data["target"] == "moved"
        assert isinstance(direct.value.data["candidates"], list)
        assert len(direct.value.data["candidates"]) >= 2

        response = _wire_batch_error(
            app, request_id="ambiguous-target", params=params
        )
        assert response["error"]["code"] == ipc_errors.AMBIGUOUS_TARGET
        assert response["error"]["data"] == direct.value.data
    finally:
        app._close()  # noqa: SLF001
        app._close_home_lease()  # noqa: SLF001


def _ready(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = durable_application(tmp_path)
    register_actor(app, "reader")
    return app


def _renew_session_liveness(app) -> None:  # type: ignore[no-untyped-def]
    agent = app.agents.get("reader")
    assert agent is not None
    app._agent_liveness.touch(agent.uri)  # noqa: SLF001 - liveness fixture seam


def test_successor_has_no_predecessor_member_count_ceiling(tmp_path: Path) -> None:
    app = _ready(tmp_path)
    for index in range(300):
        _seed(app, message_id=f"predecessor-{index:03d}")
    _renew_session_liveness(app)
    first = _batch(app)
    assert first["itemCount"] == 300

    _seed(app, message_id="new-after-large-predecessor")
    _renew_session_liveness(app)
    successor = _batch(app, after_cursor=first["cursor"])

    assert [item["messageId"] for item in successor["items"]] == [
        "new-after-large-predecessor"
    ]
    assert "excludeMessageIds" not in successor


def test_batch_orders_class_then_durable_fifo(tmp_path: Path) -> None:
    app = _ready(tmp_path)
    # Deliberately insert in reverse class order; FIFO is preserved within class.
    _seed(app, message_id="system-1", intent="event", created_at_ms=40)
    _seed(
        app,
        message_id="workflow-1",
        idempotency_key="pac-notify:wf-1:node",
        created_at_ms=30,
    )
    _seed(app, message_id="request-1", created_at_ms=20)
    _seed(app, message_id="human-1", sender="user:op", created_at_ms=10)
    _seed(app, message_id="human-2", sender="adapter:lark:e2e", created_at_ms=0)
    _seed_notice(app, message_id="notice-1", created_at_ms=-100)

    items = _batch(app)["items"]

    assert [(item["messageId"], item["class"]) for item in items] == [
        ("human-1", "human"),
        ("human-2", "human"),
        ("request-1", "request"),
        ("workflow-1", "workflow"),
        ("system-1", "system"),
        ("notice-1", "system"),
    ]
    assert [item["expectedAction"] for item in items] == [
        "reply",
        "reply",
        "reply",
        "ack",
        "ack",
        "ack",
    ]
    assert items[-1]["kind"] == "notice"


def test_notice_first_wins_fifo_within_the_shared_system_class(
    tmp_path: Path,
) -> None:
    app = _ready(tmp_path)
    _seed_notice(app, message_id="notice-first", created_at_ms=999_999)
    _seed(app, message_id="inbox-second", intent="event", created_at_ms=-999_999)

    items = _batch(app)["items"]

    assert [(item["messageId"], item["class"]) for item in items] == [
        ("notice-first", "system"),
        ("inbox-second", "system"),
    ]


def test_same_class_fifo_interleaves_both_physical_sources(
    tmp_path: Path,
) -> None:
    app = _ready(tmp_path)
    _seed(app, message_id="inbox-1", intent="event", created_at_ms=400)
    _seed_notice(app, message_id="notice-1", created_at_ms=300)
    _seed(app, message_id="inbox-2", intent="event", created_at_ms=200)
    _seed_notice(app, message_id="notice-2", created_at_ms=100)

    items = _batch(app)["items"]

    assert [(item["messageId"], item["class"]) for item in items] == [
        ("inbox-1", "system"),
        ("notice-1", "system"),
        ("inbox-2", "system"),
        ("notice-2", "system"),
    ]


def test_pending_list_does_not_drain_notice_and_ack_is_per_id(tmp_path: Path) -> None:
    app = _ready(tmp_path)
    _seed(app, message_id="keep")
    _seed_notice(app, message_id="notice-remove", text="watchdog notice")
    params = {"actor": "reader", "sessionRef": "session-1"}

    first_list = app.handle("message.pending.list", params)
    second_list = app.handle("message.pending.list", params)
    assert [row["messageId"] for row in first_list["messages"]] == [
        "notice-remove",
        "keep",
    ]
    assert [row["messageId"] for row in second_list["messages"]] == [
        "notice-remove",
        "keep",
    ]
    assert [item["messageId"] for item in _batch(app)["items"]] == [
        "keep",
        "notice-remove",
    ]

    acknowledged = app.handle(
        "message.ack",
        {**params, "messageId": "notice-remove"},
    )
    assert acknowledged["acknowledged"] is True
    assert app._inbox.system_notices(_reader(app)) == ()  # noqa: SLF001
    assert [item["messageId"] for item in _batch(app)["items"]] == ["keep"]


def test_old_cursor_never_rebinds_a_reused_message_id(tmp_path: Path) -> None:
    app = _ready(tmp_path)
    clock = [1_000_000]
    app._inbox._now_ms = lambda: clock[0]  # noqa: SLF001
    _seed(app, message_id="same-id", body="OLD")
    cursor = _batch(app)["cursor"]
    assert app.handle(
        "message.ack",
        {"actor": "reader", "sessionRef": "session-1", "messageId": "same-id"},
    )["acknowledged"] is True

    clock[0] += app._inbox.dedup_window_ms + 1  # noqa: SLF001
    _seed(app, message_id="same-id", body="NEW")

    assert _batch(app, cursor=cursor)["items"] == []


def test_same_message_id_within_dedup_window_stays_excluded(tmp_path: Path) -> None:
    app = _ready(tmp_path)
    clock = [2_000_000]
    app._inbox._now_ms = lambda: clock[0]  # noqa: SLF001
    _seed(app, message_id="same-id", body="OLD")
    cursor = _batch(app)["cursor"]
    assert app.handle(
        "message.ack",
        {"actor": "reader", "sessionRef": "session-1", "messageId": "same-id"},
    )["acknowledged"] is True

    clock[0] += app._inbox.dedup_window_ms - 1  # noqa: SLF001
    duplicate = app._inbox.receive(  # noqa: SLF001
        _message(message_id="same-id", body="RETRY")
    )

    assert duplicate.duplicate is True
    assert duplicate.accepted is False
    assert _batch(app, cursor=cursor)["items"] == []


def test_old_cursor_identity_survives_inbox_restart(tmp_path: Path) -> None:
    app = _ready(tmp_path)
    clock = [3_000_000]
    app._inbox._now_ms = lambda: clock[0]  # noqa: SLF001
    _seed(app, message_id="same-id", body="OLD")
    cursor = _batch(app)["cursor"]
    assert app.handle(
        "message.ack",
        {"actor": "reader", "sessionRef": "session-1", "messageId": "same-id"},
    )["acknowledged"] is True

    app._inbox.close()  # noqa: SLF001
    transport = LoopbackTransport()
    restarted = InboxService(
        tmp_path / "inbox.sqlite3",
        transport,
    )
    transport.service = restarted
    app._inbox = restarted  # type: ignore[assignment]  # noqa: SLF001
    restarted._now_ms = lambda: clock[0]  # noqa: SLF001
    try:
        clock[0] += restarted.dedup_window_ms + 1
        _seed(app, message_id="same-id", body="NEW")

        assert _batch(app, cursor=cursor)["items"] == []
    finally:
        restarted.close()


def test_cursor_replay_keeps_live_members_and_drops_settled_members(
    tmp_path: Path,
) -> None:
    app = _ready(tmp_path)
    _seed(app, message_id="settled")
    _seed(app, message_id="still-live")
    cursor = _batch(app)["cursor"]
    assert app.handle(
        "message.ack",
        {"actor": "reader", "sessionRef": "session-1", "messageId": "settled"},
    )["acknowledged"] is True

    replay = _batch(app, cursor=cursor)

    assert [item["messageId"] for item in replay["items"]] == ["still-live"]


def test_over_budget_batch_uses_one_private_full_body_spill(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_INBOX_BATCH_INLINE_BYTES", "1024")
    app = _ready(tmp_path)
    first_body = "alpha-秘密-" * 300
    second_body = "omega-" * 300
    _seed(app, message_id="large-1", body=first_body)
    _seed(app, message_id="large-2", body=second_body)

    result = _batch(app)

    delivery = result["delivery"]
    assert delivery["mode"] == "spill"
    assert all(item["body"] is None for item in result["items"])
    assert delivery["text"].count(delivery["spillPath"]) == 1
    spill = Path(delivery["spillPath"])
    actor_home = Path(app._agent_registry.home_receipt("reader").path)  # noqa: SLF001
    assert spill.parent == actor_home / "state" / "inbox-batches"
    assert spill.is_file() and not spill.is_symlink()
    assert os.stat(spill).st_mode & 0o777 == 0o600
    assert os.stat(spill.parent).st_mode & 0o777 == 0o700
    contents = spill.read_text()
    assert first_body in contents
    assert second_body in contents

    # Cleanup has no second ledger: each render prunes stale files and caps
    # the actor-private cache at 32 regular batch files.
    stale = spill.parent / f"batch-{'0' * 32}.md"
    for index in range(35):
        candidate = spill.parent / f"batch-{index:032x}.md"
        candidate.write_text("old spill")
        candidate.chmod(0o600)
    os.utime(stale, (1, 1))
    _seed(app, message_id="large-3", body="later-" * 300)
    _batch(app)
    assert not stale.exists()
    assert len(tuple(spill.parent.glob("batch-*.md"))) <= 32
