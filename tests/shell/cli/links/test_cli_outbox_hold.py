"""WP6: ``hyprial message outbox list`` renders the wait reason and wait duration.

The renderer reads the daemon's ``holdReason`` (falling back to the legacy
free-text ``reason``) and either the daemon-supplied wait or ``now -
createdAtMs``. An expired entry keeps its terminal state; observing the queue
never triggers a resend, mint, add or prune.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.message import outbox as outbox_module
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SOURCE = "agent:op:node-1:alice"
TARGET = "agent:op:node-1:bob"
FROZEN = 1_800_000_000_000
DASH = "\u2014"


def _record(
    monkeypatch: pytest.MonkeyPatch, *, answer: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(
        method: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> Any:
        calls.append((method, {} if params is None else params))
        return {"ok": True, "deliveries": []} if answer is None else answer

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return calls


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(_cli_root_app, list(args))


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox_module, "_now_ms", lambda: FROZEN, raising=False)


def test_outbox_list_renders_reason_and_wait_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        {
            "messageId": "m-minting",
            "recipient": TARGET,
            "createdAtMs": FROZEN - 180_000,
            "holdReason": ipc_errors.LINK_MINTING,
        },
        {
            "messageId": "m-window",
            "recipient": TARGET,
            "createdAtMs": FROZEN - 60_000,
            "holdReason": ipc_errors.LINK_WINDOW_FULL,
        },
        {
            "messageId": "m-legacy",
            "recipient": TARGET,
            "createdAtMs": FROZEN - 1_000,
            "reason": "target-not-visible",
        },
        {"messageId": "m-bare", "recipient": TARGET},
    ]
    calls = _record(monkeypatch, answer={"ok": True, "entries": entries})
    _freeze(monkeypatch)

    result = _invoke("message", "outbox", "list")

    assert result.exit_code == 0, result.stdout
    assert calls == [("outbox.list", {})]
    out = result.stdout
    assert ipc_errors.LINK_MINTING in out
    assert ipc_errors.LINK_WINDOW_FULL in out
    assert "target-not-visible" in out
    assert "3m" in out
    assert "1m" in out
    assert DASH in out


def test_outbox_wait_prefers_the_daemon_supplied_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        {
            "messageId": "m-1",
            "recipient": TARGET,
            "createdAtMs": 0,
            "waitMs": 42_000,
            "holdReason": ipc_errors.LINK_WINDOW_FULL,
        }
    ]
    _record(monkeypatch, answer={"ok": True, "entries": entries})
    _freeze(monkeypatch)

    result = _invoke("message", "outbox", "list")

    assert result.exit_code == 0, result.stdout
    assert "42s" in result.stdout


def test_outbox_marks_an_expired_entry_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        {
            "messageId": "m-expired",
            "recipient": TARGET,
            "createdAtMs": FROZEN - 10_000,
            "expiresAtMs": FROZEN - 1,
            "holdReason": ipc_errors.LINK_PEER_FULL,
        }
    ]
    _record(monkeypatch, answer={"ok": True, "entries": entries})
    _freeze(monkeypatch)

    result = _invoke("message", "outbox", "list")

    assert result.exit_code == 0, result.stdout
    row = next(line for line in result.stdout.splitlines() if "m-expired" in line)
    assert "expired" in row
    # An expired entry is not advertised as still re-evaluable.
    assert "do not resend" not in result.stdout


def test_outbox_without_a_timestamp_shows_a_dash_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(
        monkeypatch,
        answer={
            "ok": True,
            "entries": [{"messageId": "m-1", "recipient": TARGET}],
        },
    )
    _freeze(monkeypatch)

    result = _invoke("message", "outbox", "list")

    assert result.exit_code == 0, result.stdout
    row = next(line for line in result.stdout.splitlines() if "m-1" in line)
    assert DASH in row


def test_outbox_json_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {
            "messageId": "m-1",
            "recipient": TARGET,
            "createdAtMs": FROZEN - 60_000,
            "holdReason": ipc_errors.LINK_MINTING,
        }
    ]
    _record(monkeypatch, answer={"ok": True, "entries": entries})
    _freeze(monkeypatch)

    result = _invoke("message", "outbox", "list", "--json")

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["entries"] == entries
    assert all("waitMs" not in entry for entry in payload["entries"])


def test_observing_the_outbox_never_resends_a_held_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(
        method: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> Any:
        calls.append((method, {} if params is None else params))
        if method == "message.send":
            return {
                "ok": True,
                "deliveries": [
                    {
                        "target": TARGET,
                        "messageId": "m-1",
                        "accepted": True,
                        "queued": True,
                        "holdReason": ipc_errors.LINK_PEER_FULL,
                    }
                ],
            }
        return {
            "ok": True,
            "entries": [
                {
                    "messageId": "m-1",
                    "recipient": TARGET,
                    "createdAtMs": FROZEN - 1_000,
                    "holdReason": ipc_errors.LINK_PEER_FULL,
                }
            ],
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    _freeze(monkeypatch)

    send = _invoke(
        "message", "send", "--from", SOURCE, "--to", TARGET, "--summary", "s", "hello", "--json"
    )
    listed = _invoke("message", "outbox", "list")

    assert send.exit_code == 0, send.stdout
    assert listed.exit_code == 0, listed.stdout
    assert [method for method, _ in calls] == ["message.send", "outbox.list"]
