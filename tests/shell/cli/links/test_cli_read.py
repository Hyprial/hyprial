"""WP6: ``hyprial message read`` consumes one body without acking or replying.

The command is the pipe-friendly counterpart of the MCP ``harness_read_body``
tool: stdout carries the raw body only, ``--json`` passes the daemon result
through untouched, and a non-text body is named instead of guessed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon import IPC_MESSAGE_READ_BODY
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SOURCE = "agent:op:node-1:alice"


def _record(
    monkeypatch: pytest.MonkeyPatch, *, answer: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        calls.append((method, params))
        return {"ok": True, "deliveries": []} if answer is None else answer

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return calls


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(_cli_root_app, ["message", *args])


def test_read_calls_the_body_method_and_prints_only_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "line one\nline two\n\u4e2d\u6587\u6b63\u6587"
    answer = {
        "ok": True,
        "messageId": "m1",
        "sender": SOURCE,
        "summary": "short summary",
        "body": body,
        "bodyBytes": len(body.encode("utf-8")),
        "fetched": True,
    }
    calls = _record(monkeypatch, answer=answer)

    result = _invoke("read", "m1", "--from", SOURCE)

    assert result.exit_code == 0, result.stderr
    assert calls == [(IPC_MESSAGE_READ_BODY, {"from": SOURCE, "messageId": "m1"})]
    assert result.stdout == body + "\n"
    assert "short summary" not in result.stdout


def test_read_json_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {
        "ok": True,
        "messageId": "m1",
        "sender": SOURCE,
        "summary": "s",
        "body": "body",
        "bodyBytes": 4,
        "fetched": True,
    }
    calls = _record(monkeypatch, answer=answer)

    result = _invoke("read", "m1", "--from", SOURCE, "--json")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == answer
    assert calls == [(IPC_MESSAGE_READ_BODY, {"from": SOURCE, "messageId": "m1"})]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("abc", "abc\n"),
        ("abc\n", "abc\n"),
        ("abc\n\n", "abc\n\n"),
        ("", "\n"),
    ],
)
def test_read_preserves_the_body_line_endings(
    monkeypatch: pytest.MonkeyPatch, body: str, expected: str
) -> None:
    _record(monkeypatch, answer={"ok": True, "body": body, "bodyBytes": len(body)})

    result = _invoke("read", "m1", "--from", SOURCE)

    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"ok": True, "bodyBytes": 9}, "<body not text: 9 bytes; use --json>\n"),
        ({"ok": True}, "<body not text: ? bytes; use --json>\n"),
    ],
)
def test_a_non_text_body_is_named_not_guessed(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any], expected: str
) -> None:
    _record(monkeypatch, answer=answer)

    result = _invoke("read", "m1", "--from", SOURCE)

    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected


def test_an_unavailable_body_keeps_the_daemon_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(
        monkeypatch, answer={"ok": False, "code": ipc_errors.MESSAGE_BODY_UNAVAILABLE}
    )

    result = _invoke("read", "m1", "--from", SOURCE, "--json")

    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == ipc_errors.MESSAGE_BODY_UNAVAILABLE
    # No ack, reply or send is attempted after a refused read.
    assert [method for method, _ in calls] == [IPC_MESSAGE_READ_BODY]


def test_read_never_acks_or_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, answer={"ok": True, "body": "x", "bodyBytes": 1})

    result = _invoke("read", "m1", "--from", SOURCE)

    assert result.exit_code == 0, result.stderr
    assert {method for method, _ in calls} == {IPC_MESSAGE_READ_BODY}


def test_the_read_only_query_command_never_reads_a_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, answer={"ok": True, "messages": []})

    result = _invoke("inbox", SOURCE, "--json")

    assert result.exit_code == 0, result.stdout
    assert [method for method, _ in calls] == ["message.query"]
    assert IPC_MESSAGE_READ_BODY not in {method for method, _ in calls}


def test_read_help_names_the_consumption_contract() -> None:
    result = CliRunner().invoke(_cli_root_app, ["message", "read", "--help"])

    assert result.exit_code == 0, result.output
    assert "fetched" in result.stdout
    assert "reply or ack" in result.stdout


def test_a_managed_worker_read_carries_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:op:node-1:worker")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session-worker")
    calls = _record(monkeypatch, answer={"ok": True, "body": "x", "bodyBytes": 1})

    result = _invoke("read", "m1", "--from", "agent:op:node-1:worker")

    assert result.exit_code == 0, result.stderr
    assert calls == [
        (
            IPC_MESSAGE_READ_BODY,
            {
                "from": "agent:op:node-1:worker",
                "sessionRef": "session-worker",
                "messageId": "m1",
            },
        )
    ]
