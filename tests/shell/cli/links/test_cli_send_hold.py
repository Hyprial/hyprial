"""WP6: the queued/hold projection and the retry guard for ``hyprial message send``.

``holdReason`` (never the legacy ``reason``) is the link hold key; the CLI
only projects what the daemon already decided, keeps every delivery intact in
fan-out, and never emails the caller into resending.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon import HOLD_REASONS
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SOURCE = "agent:op:node-1:alice"
TARGET = "agent:op:node-1:bob"
OTHER = "agent:op:node-1:carol"
GUARD = "the daemon re-evaluates this queued message; do not resend"


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


def _send(*, json_output: bool = True) -> list[str]:
    args = ["send", "--from", SOURCE, "--to", TARGET, "--summary", "s", "hello"]
    if json_output:
        args.append("--json")
    return args


def test_the_hold_reason_set_is_frozen_at_four() -> None:
    assert len(HOLD_REASONS) == 4
    assert HOLD_REASONS == {
        ipc_errors.LINK_MINTING,
        ipc_errors.LINK_PEER_FULL,
        ipc_errors.LINK_SELF_FULL,
        ipc_errors.LINK_WINDOW_FULL,
    }


@pytest.mark.parametrize("reason", sorted(HOLD_REASONS))
def test_every_hold_reason_is_a_successful_single_call_queued_result(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": TARGET,
                "messageId": "m-1",
                "accepted": True,
                "queued": True,
                "holdReason": reason,
            }
        ],
    }
    calls = _record(monkeypatch, answer=verdict)

    result = _invoke(*_send())

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["queued"] is True
    assert payload["holdReason"] == reason
    # One operation, one send: a hold never triggers a poll, resend or mint.
    assert [method for method, _ in calls] == ["message.send"]


def test_top_level_projection_is_idempotent() -> None:
    from hyprial.shell.impl.cli.commands.message.send import _lift_hold

    delivery = {
        "target": TARGET,
        "accepted": True,
        "queued": True,
        "holdReason": ipc_errors.LINK_PEER_FULL,
    }

    result = _lift_hold(
        {
            "ok": True,
            "queued": False,
            "holdReason": ipc_errors.LINK_SELF_FULL,
            "deliveries": [delivery],
        }
    )

    assert result["queued"] is False
    assert result["holdReason"] == ipc_errors.LINK_SELF_FULL
    assert result["deliveries"] == [delivery]


def test_free_text_reason_and_hold_reason_coexist() -> None:
    from hyprial.shell.impl.cli.commands.message.send import _lift_hold

    result = _lift_hold(
        {
            "ok": True,
            "deliveries": [
                {
                    "target": TARGET,
                    "accepted": True,
                    "queued": True,
                    "reason": "target-not-visible",
                    "holdReason": ipc_errors.LINK_PEER_FULL,
                }
            ],
        }
    )

    assert result["queued"] is True
    assert result["holdReason"] == ipc_errors.LINK_PEER_FULL
    assert result["reason"] == "target-not-visible"


def test_multi_target_hold_does_not_generalize_one_delivery_reason() -> None:
    from hyprial.shell.impl.cli.commands.message.send import _lift_hold

    result = _lift_hold(
        {
            "ok": True,
            "deliveries": [
                {
                    "target": TARGET,
                    "accepted": True,
                    "queued": True,
                    "holdReason": ipc_errors.LINK_PEER_FULL,
                },
                {"target": OTHER, "accepted": True, "queued": False},
            ],
        }
    )

    assert result["queued"] is True
    assert "holdReason" not in result
    assert result["deliveries"][0]["holdReason"] == ipc_errors.LINK_PEER_FULL
    assert result["deliveries"][1]["queued"] is False


def test_partial_hold_keeps_both_deliveries_and_their_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": TARGET,
                "messageId": "m-held",
                "accepted": True,
                "queued": True,
                "holdReason": ipc_errors.LINK_MINTING,
            },
            {
                "target": OTHER,
                "messageId": "m-sent",
                "accepted": True,
                "queued": False,
            },
        ],
    }
    calls = _record(monkeypatch, answer=verdict)

    result = _invoke(
        "send",
        "--from",
        SOURCE,
        "--to",
        TARGET,
        "--to",
        OTHER,
        "--summary",
        "s",
        "hello",
        "--json",
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["queued"] is True
    assert "holdReason" not in payload
    assert [row["messageId"] for row in payload["deliveries"]] == [
        "m-held",
        "m-sent",
    ]
    assert [row.get("holdReason") for row in payload["deliveries"]] == [
        ipc_errors.LINK_MINTING,
        None,
    ]
    assert [method for method, _ in calls] == ["message.send"]


def test_queued_human_render_names_the_reason_and_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": TARGET,
                "messageId": "m-1",
                "accepted": True,
                "queued": True,
                "holdReason": ipc_errors.LINK_SELF_FULL,
                "detail": "self window full",
            }
        ],
    }
    _record(monkeypatch, answer=verdict)

    result = _invoke(*_send(json_output=False))

    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "held" in out
    assert TARGET in out
    assert ipc_errors.LINK_SELF_FULL in out
    assert GUARD in out
    assert "TTL" in out
    assert GUARD in out.rstrip().splitlines()[-1]


def test_a_fully_delivered_send_has_no_retry_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {"target": TARGET, "messageId": "m-1", "accepted": True, "queued": False}
        ],
    }
    _record(monkeypatch, answer=verdict)

    result = _invoke(*_send(json_output=False))

    assert result.exit_code == 0, result.stdout
    assert "sent" in result.stdout
    assert GUARD not in result.stdout


def test_an_expired_queued_send_is_not_advertised_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": TARGET,
                "messageId": "m-1",
                "accepted": True,
                "queued": True,
                "expired": True,
                "holdReason": ipc_errors.LINK_WINDOW_FULL,
            }
        ],
    }
    _record(monkeypatch, answer=verdict)

    result = _invoke(*_send(json_output=False))

    assert result.exit_code == 0, result.stdout
    assert GUARD not in result.stdout
    assert "expired" in result.stdout
    assert "sent" not in result.stdout


def test_a_refused_send_stays_ok_false_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch, answer={"ok": False, "code": ipc_errors.LINK_UNKNOWN})

    result = _invoke(*_send())

    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == ipc_errors.LINK_UNKNOWN
    assert "queued" not in payload
    assert [method for method, _ in calls] == ["message.send"]


def test_open_requests_from_a_delivery_is_one_stderr_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": TARGET,
                "messageId": "m-2",
                "accepted": True,
                "queued": False,
                "openRequestsFrom": ["m1", "m2"],
            }
        ],
    }
    _record(monkeypatch, answer=verdict)

    result = _invoke(*_send(json_output=False))

    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1
    assert TARGET in lines[0]
    assert "m1" in lines[0] and "m2" in lines[0]
    assert "hyprial message reply" in lines[0]


def test_open_requests_hint_is_silent_under_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {
                "target": TARGET,
                "messageId": "m-2",
                "accepted": True,
                "queued": False,
                "openRequestsFrom": ["m1"],
            }
        ],
    }
    _record(monkeypatch, answer=verdict)

    result = _invoke(*_send())

    assert result.exit_code == 0, result.stdout
    assert result.stderr == ""
    assert json.loads(result.stdout)["deliveries"][0]["openRequestsFrom"] == ["m1"]


@pytest.mark.parametrize("field", [None, []])
def test_no_open_requests_field_means_no_stderr_line(
    monkeypatch: pytest.MonkeyPatch, field: Any
) -> None:
    delivery: dict[str, Any] = {
        "target": TARGET,
        "messageId": "m-2",
        "accepted": True,
        "queued": False,
    }
    if field is not None:
        delivery["openRequestsFrom"] = field
    else:
        delivery["openRequestsFrom"] = None
    _record(monkeypatch, answer={"ok": True, "deliveries": [delivery]})

    result = _invoke(*_send(json_output=False))

    assert result.exit_code == 0, result.stdout
    assert result.stderr == ""


def test_top_level_open_requests_uses_the_single_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = {
        "ok": True,
        "deliveries": [
            {"target": TARGET, "messageId": "m-2", "accepted": True, "queued": False}
        ],
        "openRequestsFrom": ["m9"],
    }
    _record(monkeypatch, answer=verdict)

    result = _invoke(*_send(json_output=False))

    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1
    assert TARGET in lines[0] and "m9" in lines[0]
