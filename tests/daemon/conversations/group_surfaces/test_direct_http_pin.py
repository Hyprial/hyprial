"""A direct ``message.send`` over HTTP keeps its published contract (slice GH).

Written on the base commit before any GH code, and green there: the HTTP
projection of ``message.send`` (its OpenAPI operation and its method entry,
as canonical JSON digests) and the listener's answers for a direct send, a
body without ``to``, a body with ``mentions`` or ``replyTo``, and ``to``
with a group id.  Group posts over HTTP must not move a byte of any of these.

2026-11-21 起投影里多了 x-hyprial-mock-echo（预览引擎的回显声明），摘要随之更新。
2026-11-24（I7a）：``PROXY_SEND_REFUSED`` 退出 ``message.send`` 声明的错误，摘要随之更新。
2026-11-24（I4b）：``route:`` 入口的错误码与 ``routeDelivery``/``chatId``/``fanoutOf`` 退出声明，摘要随之更新。
2026-11-24（channel 能力退役）：``CHANNEL_NOT_AUTHORIZED`` 退出 ``message.send`` 声明的错误，摘要随之更新。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hyprial.daemon import daemon_openapi
from tests.daemon.conversations.group_messages.group_message_support import make_group
from tests.daemon.conversations.group_requests.group_request_support import group_daemon
from tests.daemon.conversations.group_surfaces.surface_support import (
    OWNER_ADDRESS,
    http_call,
    http_server,
)
from tests.support.golden.app import golden_daemon as golden_daemon

OPERATION_SHA256 = "369681939b5c66901e5f7219d2f8a112a4fcff92b50f26dc3c9df3cec97eb91c"
METHOD_ENTRY_SHA256 = "c0735049f997806860acaae499d89ffbcb8db1ee7cfc0f68922d218446522a3d"
KEY = "direct-http-pin-key-0001"


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_the_message_send_http_projection_is_unchanged() -> None:
    document = daemon_openapi()
    operation = document["paths"]["/daemon/request/message.send"]
    [entry] = [item for item in document["x-hyprial-methods"] if item["name"] == "message.send"]
    schema = operation["post"]["requestBody"]["content"]["application/json"]["schema"]

    assert sorted(schema["properties"]) == [
        "actor", "conversationId", "idempotencyKey", "message", "summary", "to",
    ]
    assert schema["required"] == ["to", "message", "summary", "idempotencyKey"]
    assert (_digest(operation), _digest(entry)) == (OPERATION_SHA256, METHOD_ENTRY_SHA256)


def _direct(booted, **extra: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {"to": [booted.address("alpha")], "message": "hello alpha",
            "summary": "hello alpha", "idempotencyKey": KEY, **extra}


def test_a_direct_http_send_is_answered_as_before(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    server = http_server(booted)

    status, payload = http_call(server, "message.send", _direct(booted))

    assert status == 200, payload
    result = payload["result"]
    [delivery] = result["deliveries"]
    assert (result["ok"], result["actingPrincipal"], result["operationId"]) == (
        True, OWNER_ADDRESS, KEY,
    )
    assert (delivery["target"], delivery["accepted"]) == (booted.address("alpha"), True)
    assert sorted(payload) == ["dispatch", "method", "ok", "requestId", "result", "schemaVersion"]
    assert payload["dispatch"] == "dispatched"


def _refusal(message: str, *, dispatch: str = "not-dispatched") -> tuple[int, dict[str, Any]]:
    return 400, {"schemaVersion": 1, "ok": False, "requestId": "req-1",
                 "code": "INVALID_ARGUMENT", "message": message, "dispatch": dispatch,
                 "method": "message.send"}


def test_the_direct_http_refusals_are_unchanged(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    server = http_server(booted)
    group = make_group(booted)
    without_to = {key: value for key, value in _direct(booted).items() if key != "to"}

    answers = {
        "no-to": http_call(server, "message.send", without_to),
        "no-to-group": http_call(server, "message.send", {**without_to, "conversationId": group}),
        "mentions": http_call(server, "message.send", _direct(booted, mentions=[OWNER_ADDRESS])),
        "reply-to": http_call(server, "message.send", _direct(booted, replyTo="m-1")),
        "to-and-group": http_call(server, "message.send", _direct(booted, conversationId=group)),
    }

    assert answers == {
        "no-to": _refusal("message.send params do not match the declared schema at <root>"),
        "no-to-group": _refusal("message.send params do not match the declared schema at <root>"),
        "mentions": _refusal("daemon session identity is not accepted over HTTP: mentions"),
        "reply-to": _refusal("daemon session identity is not accepted over HTTP: replyTo"),
        "to-and-group": _refusal("the daemon refused the request", dispatch="dispatched"),
    }
