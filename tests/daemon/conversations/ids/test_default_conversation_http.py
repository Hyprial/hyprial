"""HTTP ``message.send`` may omit ``conversationId`` exactly like the socket.

Plan slice G0.  The request goes through the real ``RequestHttpServer``
validation and owner-selector injection; the injected request is then handed
to the golden ``DaemonApplication`` (real ``message.send``, real custody
inbox), so the conversation id is read from the stored queue rows.
"""

from __future__ import annotations

from dataclasses import replace
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.daemon.impl.conversations.ids import default_conversation_id, endpoint_key
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.daemon.conversations.bypass.bypass_support import Booted, boot, stored
from tests.support.golden.app import (
    ALPHA,
    BETA,
    NODE,
    OWNER,
    OWNER_USER,
    golden_daemon as golden_daemon,
)


def _server(app: Any, seen: list[dict[str, Any]]) -> RequestHttpServer:
    config = RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"), owner=OWNER, actor=OWNER_USER,
        host="127.0.0.1", port=0, allowed_origins=(), allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=Path("/tmp/request-http-runtime"), source=Path("/tmp/config.json"),
    )

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        seen.append(dict(params))
        return app.handle(method, params)

    server = RequestHttpServer(config, request_once=request_once)
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item.update(declared=True, state="available" if entry["httpExposed"] else "disabled",
                    reason=None)
        item["callable"] = item["state"] == "available"
        entries.append(item)
    view = build_view(
        {"version": __version__, "nodeId": NODE, "owner": OWNER, "epoch": "epoch-1",
         "phase": "serving"},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "epoch-1",
         "packageVersion": __version__, "methods": entries},
        socket_path="/tmp/daemon.sock", local_entries=local_catalog()["methods"],
    )
    server._view = replace(view)
    server._startup_declaration = view.declaration_digest
    server._startup_http = view.http_contract_digest
    server._snapshot_at = time.monotonic()
    return server


def _post(server: RequestHttpServer, method: str, params: dict[str, Any]) -> Any:
    return server._invoke(method, "req-1", time.monotonic(), params, None)


def _send(to: str, key: str, **extra: Any) -> dict[str, Any]:
    return {"to": [to], "summary": f"s {key}", "message": f"m {key}",
            "idempotencyKey": f"http-key-{key}-padding-padding", **extra}


@pytest.fixture
def world(golden_daemon, monkeypatch) -> tuple[Booted, RequestHttpServer, list[dict[str, Any]]]:
    booted = boot(golden_daemon, monkeypatch)
    seen: list[dict[str, Any]] = []
    return booted, _server(booted.app, seen), seen


def _stored_conversations(booted: Booted, message_id: str) -> set[str]:
    rows = stored(booted.app, message_id)
    assert rows
    return {str(row["conversation_id"]) for row in rows.values()}


def test_omitted_id_lands_in_the_default_conversation_of_owner_and_target(world) -> None:
    booted, server, seen = world
    first = _post(server, "message.send", _send(BETA, "one"))
    second = _post(server, "message.send", _send(BETA, "two"))
    assert first.status == 200 and second.status == 200, (first.payload, second.payload)
    expected = default_conversation_id(endpoint_key(OWNER_USER), endpoint_key(BETA))
    results = [first.payload["result"], second.payload["result"]]
    for result in results:
        assert result["conversationId"] == expected
        assert [d["conversationId"] for d in result["deliveries"]] == [expected]
        assert _stored_conversations(booted, result["messageId"]) == {expected}
    assert results[0]["messageId"] != results[1]["messageId"]
    assert all(call["actor"] == OWNER_USER and "conversationId" not in call for call in seen)


def test_explicit_id_is_used_verbatim(world) -> None:
    booted, server, _seen = world
    answer = _post(server, "message.send", _send(BETA, "explicit", conversationId="chat-1"))
    assert answer.status == 200, answer.payload
    result = answer.payload["result"]
    assert result["conversationId"] == "chat-1"
    assert _stored_conversations(booted, result["messageId"]) == {"chat-1"}


def test_browser_supplied_sender_cannot_change_the_default_conversation(world) -> None:
    booted, server, _seen = world
    forged = _post(server, "message.send", _send(BETA, "forged", **{"from": ALPHA}))
    assert forged.status == 400
    other_actor = _post(server, "message.send", _send(BETA, "actor", actor=ALPHA))
    assert other_actor.status == 403
    honest = _post(server, "message.send", _send(BETA, "honest"))
    assert honest.payload["result"]["conversationId"] == default_conversation_id(
        endpoint_key(OWNER_USER), endpoint_key(BETA))


def test_lookup_with_the_returned_id_finds_an_id_less_send(world) -> None:
    booted, server, _seen = world
    params = _send(BETA, "lookup")
    sent = _post(server, "message.send", params).payload["result"]
    by_id = _post(server, "message.send.lookup", {
        "target": BETA, "text": params["message"],
        "conversationId": sent["conversationId"],
        "idempotencyKey": params["idempotencyKey"],
    })
    assert by_id.payload["result"]["status"] == "accepted", by_id.payload
    assert by_id.payload["result"]["messageId"] == sent["messageId"]
    by_key = _post(server, "message.send.key_lookup", {"operationKey": params["idempotencyKey"]})
    assert by_key.payload["result"]["conversationId"] == sent["conversationId"]
