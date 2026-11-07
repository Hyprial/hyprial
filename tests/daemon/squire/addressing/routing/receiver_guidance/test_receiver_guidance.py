from __future__ import annotations

from pathlib import Path
import pytest
from typer.testing import CliRunner
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.identity.impl.users.profile import UserProfileStore
from hyprial.daemon.impl.squire.addressing import UNAVAILABLE_ADAPTER_MESSAGE, UNCONFIGURED_SQUIRE_MESSAGE, ReceiverUserDelivery, UserAdapterRegistry, UserDeliveryLedger, UserDeliveryResult

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


def test_receiver_without_squire_returns_explicit_coordinator_guidance(
    tmp_path: Path,
) -> None:
    profiles = UserProfileStore(tmp_path / "receiver-home" / "state" / "users.json")
    profiles.ensure(
        owner="bob",
        owner_key="owner-bob",
        login_name="bob",
        machine="receiver-mac",
        machine_key="receiver-machine-key",
    )
    receiver = ReceiverUserDelivery(
        node_id="receiver-mac",
        profiles=profiles,
        adapters=UserAdapterRegistry(),
        ledger=UserDeliveryLedger(tmp_path / "receiver-home" / "state" / "user-deliveries.json"),
    )

    result = receiver.handle(_request())

    assert result.accepted is False
    assert result.code == "TARGET_SQUIRE_UNCONFIGURED"
    assert result.message == UNCONFIGURED_SQUIRE_MESSAGE
    assert "hyprial squire setup" in result.message


def test_receiver_unknown_user_returns_coordinator_guidance(
    tmp_path: Path,
) -> None:
    profiles = UserProfileStore(tmp_path / "receiver-home" / "state" / "users.json")
    # no profile exists for "carol" anywhere
    receiver = ReceiverUserDelivery(
        node_id="receiver-mac",
        profiles=profiles,
        adapters=UserAdapterRegistry(),
        ledger=UserDeliveryLedger(
            tmp_path / "receiver-home" / "state" / "user-deliveries.json"
        ),
    )

    result = receiver.handle(_request(owner="carol"))

    assert result.accepted is False
    assert result.code == "TARGET_SQUIRE_UNCONFIGURED"
    assert result.message == UNCONFIGURED_SQUIRE_MESSAGE
    # Nothing was sent, so the refusal cannot lock the idempotency key.  A
    # retry re-evaluates the now-current receiver configuration.
    retried = receiver.handle(_request(owner="carol"))
    assert retried.accepted is False
    assert retried.duplicate is False
    assert receiver.ledger.get("operation-user-1:carol") is None
    assert retried.code == "TARGET_SQUIRE_UNCONFIGURED"


def test_receiver_configured_but_unavailable_adapter_returns_explicit_guidance(
    tmp_path: Path,
) -> None:
    profiles = UserProfileStore(tmp_path / "receiver-home" / "state" / "users.json")
    profiles.ensure(
        owner="bob",
        owner_key="owner-bob",
        login_name="bob",
        machine="receiver-mac",
        machine_key="receiver-machine-key",
    )
    adapter_uri = "channel:bob:receiver-mac:squire-app"
    profiles.set_squire_channel("owner-bob", adapter_uri)
    profiles.bind_owner_open_id(
        "owner-bob", channel=adapter_uri, open_id="ou_receiver_owner"
    )
    receiver = ReceiverUserDelivery(
        node_id="receiver-mac",
        profiles=profiles,
        adapters=UserAdapterRegistry(),  # squire adapter is not online
        ledger=UserDeliveryLedger(
            tmp_path / "receiver-home" / "state" / "user-deliveries.json"
        ),
    )

    result = receiver.handle(_request())

    assert result.accepted is False
    assert result.code == "TARGET_SQUIRE_ADAPTER_UNAVAILABLE"
    assert result.message == UNAVAILABLE_ADAPTER_MESSAGE
    assert "channels.json" in result.message


def test_daemon_surfaces_receiver_squire_error(tmp_path: Path) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(
            message_id="msg-user-1",
            accepted=False,
            code="TARGET_SQUIRE_UNCONFIGURED",
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
                "from": "agent:alice:sender-mac:worker",
                "to": ["user:bob"],
                "summary": "hello",
                "message": "hello",
                "idempotencyKey": "operation-1",
            },
        )

    assert failure.value.code == "TARGET_SQUIRE_UNCONFIGURED"
