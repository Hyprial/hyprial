from __future__ import annotations

from pathlib import Path
import pytest
from typer.testing import CliRunner
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.adapters.user_delivery.receiver import NO_USER_AGENT_TARGET_MESSAGE, ReceiverUserDelivery, UserDeliveryLedger, UserDeliveryResult

from ..support import (
    FakeUserDelivery,
    _application,
    _close_test_applications as _close_test_applications,
    _request,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_cli_send_passes_user_target_to_daemon_as_a_first_class_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"ok": True, "deliveries": [{"target": "user:bob"}]}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "message",
            "send",
            "--from",
            "agent:alice:sender-mac:worker",
            "--to",
            "user:bob",
            "--summary",
            "hello",
            "hello",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "message.send",
            {
                "from": "agent:alice:sender-mac:worker",
                "to": ["user:bob"],
                "message": "hello",
                "summary": "hello",
            },
        )
    ]


def test_receiver_without_an_owner_push_returns_user_agent_cell_guidance(
    tmp_path: Path,
) -> None:
    receiver = ReceiverUserDelivery(
        node_id="receiver-mac",
        owns=lambda owner: owner == "bob",
        ledger=UserDeliveryLedger(tmp_path / "receiver-home" / "state" / "user-deliveries.json"),
    )

    result = receiver.handle(_request())

    assert result.accepted is False
    assert result.code == "TARGET_UNRESOLVED"
    assert result.message == NO_USER_AGENT_TARGET_MESSAGE
    assert "hyprial agent cell show hyprial-user" in result.message
    assert "user proxy" not in result.message


def test_receiver_unknown_user_returns_coordinator_guidance(
    tmp_path: Path,
) -> None:
    receiver = ReceiverUserDelivery(
        node_id="receiver-mac",
        owns=lambda owner: owner == "bob",
        ledger=UserDeliveryLedger(
            tmp_path / "receiver-home" / "state" / "user-deliveries.json"
        ),
    )

    result = receiver.handle(_request(owner="carol"))

    assert result.accepted is False
    assert result.code == "TARGET_UNRESOLVED"
    assert result.message == NO_USER_AGENT_TARGET_MESSAGE
    # Nothing was sent, so the refusal cannot lock the idempotency key.  A
    # retry re-evaluates the now-current receiver configuration.
    retried = receiver.handle(_request(owner="carol"))
    assert retried.accepted is False
    assert retried.duplicate is False
    assert receiver.ledger.get("operation-user-1:carol") is None
    assert retried.code == "TARGET_UNRESOLVED"


def test_daemon_surfaces_the_other_owners_receiver_error(tmp_path: Path) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(
            message_id="msg-user-1",
            accepted=False,
            # per Allen 2026-11-07 (R2): message.send no longer declares the
            # pre-S10 TARGET_SQUIRE_* codes; an S10 receiver answers
            # TARGET_UNRESOLVED with the same guidance.
            code="TARGET_UNRESOLVED",
            message="目标用户尚未配置侍从；请改用协调者转达",
        )
    )
    app, _inbox = _application(tmp_path, delivery)

    with pytest.raises(
        DaemonRequestError, match="目标用户尚未配置侍从；请改用协调者转达"
    ) as failure:
        app.handle(
            "message.send",
            {
                "from": app.agents.get("worker").uri,
                "to": ["user:bob"],
                "summary": "hello",
                "message": "hello",
                "idempotencyKey": "operation-1",
            },
        )

    assert failure.value.code == "TARGET_UNRESOLVED"
