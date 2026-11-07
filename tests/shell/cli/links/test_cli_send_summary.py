"""WP6: ``--summary`` is required and validated locally before any send IPC.

The byte budget is measured on the RAW value (never on a stripped copy), the
value reaches the daemon verbatim, and a missing, blank or non-UTF-8 summary
fails with SUMMARY_REQUIRED without touching ``message.send``/``message.reply``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.support import CliError
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SOURCE = "agent:op:node-1:alice"
TARGET = "agent:op:node-1:bob"
HAN = "\u6c49"  # 3 UTF-8 bytes


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


def test_send_without_summary_fails_locally_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    result = _invoke("send", "--from", SOURCE, "--to", TARGET, "hello", "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == ipc_errors.SUMMARY_REQUIRED
    assert calls == []


def test_send_blank_summary_is_rejected_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    result = _invoke(
        "send", "--from", SOURCE, "--to", TARGET, "--summary", "   \t ", "hello", "--json"
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.SUMMARY_REQUIRED
    assert calls == []


def test_send_over_budget_summary_reports_the_raw_byte_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)
    summary = HAN * 171  # 513 UTF-8 bytes, 171 characters
    assert len(summary.encode("utf-8")) == 513

    result = _invoke(
        "send", "--from", SOURCE, "--to", TARGET, "--summary", summary, "hello", "--json"
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == ipc_errors.SUMMARY_REQUIRED
    assert payload["data"] == {"summaryBytes": 513, "maxBytes": 512}
    assert calls == []


def test_over_budget_raw_value_is_not_rescued_by_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)
    summary = " " + HAN * 170 + "  "  # 513 raw bytes; 510 after stripping
    assert len(summary.encode("utf-8")) == 513

    result = _invoke(
        "send", "--from", SOURCE, "--to", TARGET, "--summary", summary, "hello", "--json"
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["data"] == {"summaryBytes": 513, "maxBytes": 512}
    assert calls == []


def test_exactly_512_bytes_with_surrounding_whitespace_passes_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)
    summary = " " + HAN * 170 + " "  # 1 + 510 + 1 = 512 bytes
    assert len(summary.encode("utf-8")) == 512

    result = _invoke(
        "send", "--from", SOURCE, "--to", TARGET, "--summary", summary, "hello", "--json"
    )

    assert result.exit_code == 0, result.stdout
    assert calls == [
        (
            "message.send",
            {"from": SOURCE, "to": [TARGET], "message": "hello", "summary": summary},
        )
    ]


def test_a_non_utf8_summary_is_a_matchable_summary_required_error() -> None:
    from hyprial.shell.impl.cli.commands.message.send import _require_summary

    with pytest.raises(CliError) as caught:
        _require_summary("\ud800")

    assert caught.value.code == ipc_errors.SUMMARY_REQUIRED


def test_require_summary_returns_the_raw_value() -> None:
    from hyprial.shell.impl.cli.commands.message.send import _require_summary

    raw = "  padded summary  "
    assert _require_summary(raw) == raw


def test_send_carries_summary_and_the_existing_parameter_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    result = _invoke(
        "send", "--from", SOURCE, "--to", TARGET, "--summary", "s", "hello", "--json"
    )

    assert result.exit_code == 0, result.stdout
    assert calls == [
        (
            "message.send",
            {"from": SOURCE, "to": [TARGET], "message": "hello", "summary": "s"},
        )
    ]


def test_reply_without_summary_fails_locally_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    result = _invoke("reply", "m1", "--from", SOURCE, "done", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.SUMMARY_REQUIRED
    assert calls == []


def test_reply_carries_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)

    result = _invoke(
        "reply", "m1", "--from", SOURCE, "--summary", "s", "done", "--json"
    )

    assert result.exit_code == 0, result.stdout
    assert calls == [
        (
            "message.reply",
            {"from": SOURCE, "messageId": "m1", "message": "done", "summary": "s"},
        )
    ]


@pytest.mark.parametrize(
    "target",
    ["user:op", "user:someone-else", TARGET, "route:lark:ops"],
)
def test_send_preserves_the_daemon_verdict_for_every_target_kind(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": target,
                "messageId": "m-1",
                "accepted": True,
                "queued": True,
                "holdReason": ipc_errors.LINK_WINDOW_FULL,
            }
        ],
    }
    calls = _record(monkeypatch, answer=verdict)

    result = _invoke(
        "send", "--from", SOURCE, "--to", target, "--summary", "s", "hello", "--json"
    )

    assert result.exit_code == 0, result.stdout
    # The tool surface forwards to the daemon exactly once and never mints,
    # adds a link or self-authorizes on the caller's behalf.
    assert [method for method, _ in calls] == ["message.send"]
    payload = json.loads(result.stdout)
    assert payload["deliveries"][0]["holdReason"] == ipc_errors.LINK_WINDOW_FULL
    assert payload["queued"] is True


def test_a_plain_send_never_invents_a_reply_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {"target": TARGET, "messageId": "m-2", "accepted": True, "queued": False}
        ],
        "openRequestsFrom": ["m-1"],
    }
    calls = _record(monkeypatch, answer=verdict)

    result = _invoke(
        "send", "--from", SOURCE, "--to", TARGET, "--summary", "s", "hello", "--json"
    )

    assert result.exit_code == 0, result.stdout
    assert [method for method, _ in calls] == ["message.send"]
    assert "replyTo" not in calls[0][1]


def test_explicit_reply_to_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)

    result = _invoke(
        "send",
        "--from",
        SOURCE,
        "--to",
        TARGET,
        "--reply-to",
        "m-1",
        "--summary",
        "s",
        "hello",
        "--json",
    )

    assert result.exit_code == 0, result.stdout
    assert calls[0][1]["replyTo"] == "m-1"


@pytest.mark.parametrize(
    "code", [ipc_errors.LINK_RECLAIMED, ipc_errors.MESSAGE_REPLY_UNAVAILABLE]
)
def test_failed_reply_is_passed_through_without_a_follow_up(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    calls = _record(monkeypatch, answer={"ok": False, "code": code})

    result = _invoke(
        "reply", "m1", "--from", SOURCE, "--summary", "s", "done", "--json"
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == code
    # No ack, resend or link.add may follow a refused reply.
    assert [method for method, _ in calls] == ["message.reply"]


def test_a_full_window_still_submits_owner_sends_and_explicit_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": TARGET,
                "messageId": "m-3",
                "accepted": True,
                "queued": True,
                "holdReason": ipc_errors.LINK_SELF_FULL,
            }
        ],
    }
    calls = _record(monkeypatch, answer=verdict)

    send = _invoke(
        "send",
        "--from",
        "user:op",
        "--on-behalf-of",
        TARGET,
        "--to",
        TARGET,
        "--summary",
        "s",
        "hello",
        "--json",
    )
    reply = _invoke(
        "reply", "m-4", "--from", SOURCE, "--summary", "s", "done", "--json"
    )

    assert send.exit_code == 0, send.stdout
    assert reply.exit_code == 0, reply.stdout
    # The shell never reads k/W/cwnd and never refuses on its own.
    assert [method for method, _ in calls] == ["message.send", "message.reply"]
