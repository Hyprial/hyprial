"""S2: the Casdoor webhook is a loopback signal; its payload is never content.

Record shapes follow infra-ops' production sample (image 8847a6e,
2026-10-08), with placeholder values.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.org.network.casdoor.webhook import (
    TOKEN_HEADER,
    CasdoorWebhookReceiver,
    Trigger,
    WebhookSettings,
    triggers_for,
    webhook_settings,
)

PATH = "/casdoor-webhook/p-123"
TOKEN = "tok-abc"


def _record(action: str, *, uri: str = "", body: object = "") -> dict[str, Any]:
    return {
        "id": 1,
        "owner": "built-in",
        "name": "rec",
        "organization": "built-in",
        "user": "admin",  # the caller, never the changed user
        "method": "POST",
        "requestUri": uri,
        "action": action,
        "object": body if isinstance(body, str) else json.dumps(body),
        "response": "{}",
        "statusCode": 200,
        "isTriggered": False,
    }


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_record("update-user", uri="/api/update-user?id=hyprial%2Fbob%40example&columns=x"), [Trigger("user", "bob@example")]),
        (_record("update-user", uri="/api/update-user?userId=abc&owner=hyprial"), [Trigger("all")]),
        (_record("update-user", uri="/api/update-user?id=other%2Fbob"), []),
        (_record("delete-user", body={"owner": "hyprial", "name": "bob@example"}), [Trigger("user", "bob@example")]),
        (_record("delete-user", body="not json"), [Trigger("all")]),
        (_record("delete-user", body={"owner": "built-in", "name": "x"}), []),
        (_record("update-group", uri="/api/update-group?id=hyprial%2Fgroup-acme"), [Trigger("org", "group-acme")]),
        (_record("add-application", uri="/api/add-application"), []),
        ("not a record", [Trigger("all")]),
    ],
)
def test_a_record_names_who_to_re_read_and_nothing_else(record: object, expected: list[Trigger]) -> None:
    assert triggers_for(record) == expected


def test_settings_take_the_path_from_the_url_casdoor_posts_to(tmp_path: Path) -> None:
    path = tmp_path / "casdoor.env"
    path.write_text(
        "CASDOOR_GROUP_SYNC_WEBHOOK_URL=http://host.docker.internal:18790/casdoor-webhook/p-123\n"
        f"CASDOOR_WEBHOOK_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    assert webhook_settings(path) == WebhookSettings(path=PATH, token=TOKEN)
    path.write_text("CASDOOR_GROUP_SYNC_ENDPOINT=http://x\n", encoding="utf-8")
    assert webhook_settings(path) is None


class _Logs:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, *args: Any, **fields: Any) -> None:
        self.lines.append(json.dumps([args, fields], default=str))


@pytest.fixture
def receiver():  # type: ignore[no-untyped-def]
    triggers: list[Trigger] = []
    logs = _Logs()
    server = CasdoorWebhookReceiver(
        WebhookSettings(path=PATH, token=TOKEN), triggers.append, log=logs, port=0
    )
    server.start()
    yield server, triggers, logs
    assert server.close()


def _post(port: int, path: str, body: bytes, token: str | None = TOKEN) -> int:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_a_valid_post_triggers_a_re_read_and_logs_no_payload(receiver) -> None:  # type: ignore[no-untyped-def]
    server, triggers, logs = receiver
    secret = "clientSecret-s3cret"
    record = _record(
        "update-user",
        uri="/api/update-user?id=hyprial%2Fbob%40example",
        body={"name": "bob@example", "clientSecret": secret},
    )

    status = _post(server.port, PATH, json.dumps(record).encode())

    assert status == 200
    assert triggers == [Trigger("user", "bob@example")]
    assert logs.lines and not any(secret in line or PATH in line for line in logs.lines)


def test_a_wrong_path_is_404_and_a_wrong_token_is_401(receiver) -> None:  # type: ignore[no-untyped-def]
    server, triggers, logs = receiver
    body = json.dumps(_record("update-group", uri="/api/update-group?id=hyprial%2Fg")).encode()

    assert _post(server.port, "/casdoor-webhook/guess", body) == 404
    assert _post(server.port, PATH, body, token="wrong") == 401
    assert _post(server.port, PATH, body, token=None) == 401
    assert triggers == []
    assert not any(TOKEN in line or "guess" in line for line in logs.lines)


def test_an_oversized_body_is_refused_without_reading_it(receiver) -> None:  # type: ignore[no-untyped-def]
    server, triggers, _logs = receiver
    # Only the headers are sent: a 413 that arrives while nothing of the
    # declared body was written proves the length is judged before any read.
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.putrequest("POST", PATH)
        connection.putheader(TOKEN_HEADER, TOKEN)
        connection.putheader("Content-Length", str(300 * 1024))
        connection.endheaders()
        status = connection.getresponse().status
    finally:
        connection.close()

    assert status == 413
    assert triggers == []


def test_the_listener_binds_loopback_only(receiver) -> None:  # type: ignore[no-untyped-def]
    server, _triggers, _logs = receiver
    assert server._server.server_address[0] == "127.0.0.1"


def test_a_name_that_is_not_a_user_identity_is_a_400(tmp_path: Path) -> None:
    def refuse(_trigger: Trigger) -> None:
        raise ValueError("not a user identity")

    server = CasdoorWebhookReceiver(WebhookSettings(path=PATH, token=TOKEN), refuse, port=0)
    server.start()
    try:
        body = json.dumps(_record("update-user", uri="/api/update-user?id=hyprial%2Fa%3Ab")).encode()
        assert _post(server.port, PATH, body) == 400
    finally:
        assert server.close()


def test_a_stalled_client_is_dropped_after_the_handler_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from hyprial.daemon.impl.org.network.casdoor import webhook as webhook_module

    monkeypatch.setattr(webhook_module, "HANDLER_TIMEOUT_SECONDS", 0.5)
    triggers: list[Trigger] = []
    server = CasdoorWebhookReceiver(WebhookSettings(path=PATH, token=TOKEN), triggers.append, port=0)
    server.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.putrequest("POST", PATH)
        connection.putheader(TOKEN_HEADER, TOKEN)
        connection.putheader("Content-Length", "10")  # the body never comes
        connection.endheaders()
        started = time.monotonic()
        with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError, http.client.BadStatusLine)):
            connection.getresponse()
        assert time.monotonic() - started < 5
    finally:
        connection.close()
        assert server.close()
    assert triggers == []
