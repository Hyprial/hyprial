"""Review-round regressions for sender-local agent-bot delivery."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.composition.ports import LarkPortClient

from hyprial.daemon.impl.adapters.lark import LarkApiError
from hyprial.daemon.impl.application import DaemonApplication

from ..support import (
    FakeInbox,
    _TEST_APPLICATIONS,
    _application,
    _close_test_applications as _close_test_applications,
    _deep_module_daemon_dependencies,
)
from .test_agent_bot_dm import (
    ADAPTER,
    _AdapterRuntime,
    _delivery,
    _eligible_app,
    _Gateway,
    _register_session,
    _send,
)


def _agent_bot_events(events: list[tuple[str, dict[str, object]]]) -> list[dict[str, object]]:
    return [fields for event, fields in events if event == "user-delivery.agent-bot"]


def test_uncertain_replay_with_adapter_offline_never_crosses_to_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(TimeoutError("request may have reached Lark"))
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    first = _send(app, operation="uncertain-offline")
    app._adapters.online = False
    replay = _send(app, operation="uncertain-offline")

    assert first["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(gateway.calls) == 1
    assert fallback.requests == []


def test_accepted_replay_returns_recorded_duplicate_without_gateway_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    first = _send(app, operation="accepted-replay")
    replay = _send(app, operation="accepted-replay")

    assert replay["deliveries"][0] == {
        **first["deliveries"][0],
        "duplicate": True,
    }
    assert len(gateway.calls) == 1
    assert fallback.requests == []


def test_definite_refusal_replay_stays_on_fallback_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(
        LarkApiError(
            "send owner DM",
            230013,
            "Bot has NO availability to this user",
        )
    )
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    first = _send(app, operation="definite-replay")
    gateway.error = None
    replay = _send(app, operation="definite-replay")

    assert first["ok"] is True
    assert replay["ok"] is True
    assert len(gateway.calls) == 1
    assert len(fallback.requests) == 2


def test_inflight_crash_replay_is_uncertain_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(SystemExit("simulated crash before settlement"))
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    with pytest.raises(SystemExit, match="simulated crash"):
        _send(app, operation="crash-inflight")
    gateway.error = None
    app._adapters.online = False

    replay = _send(app, operation="crash-inflight")

    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(gateway.calls) == 1
    assert fallback.requests == []


@pytest.mark.parametrize(
    ("code", "message", "missing_scopes"),
    [
        (230001, "invalid receive_id", ()),
        (230013, "Bot has NO availability to this user", ()),
        (99991400, "too many requests", ()),
        (230027, "Lack of necessary permissions", ("im:message",)),
    ],
)
def test_explicit_lark_pre_send_refusals_allow_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    message: str,
    missing_scopes: tuple[str, ...],
) -> None:
    gateway = _Gateway(
        LarkApiError(
            "send owner DM",
            code,
            message,
            missing_scopes=missing_scopes,
        )
    )
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    result = _send(app, operation=f"pre-send-{code}")

    assert result["ok"] is True
    assert len(fallback.requests) == 1


@pytest.mark.parametrize(
    "error",
    [
        LarkApiError("send owner DM", 230012, "content rejected"),
        LarkApiError("send owner DM", 230027, "ambiguous permission error"),
    ],
)
def test_other_coded_lark_errors_are_uncertain_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: LarkApiError,
) -> None:
    gateway = _Gateway(error)
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    result = _send(app, operation=f"coded-unknown-{error.code}")

    assert result["ok"] is False
    assert result["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert fallback.requests == []


def test_runtime_error_text_cannot_claim_pre_send_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(RuntimeError("gateway closed before send (untrusted text)"))
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    result = _send(app, operation="runtime-text")

    assert result["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert fallback.requests == []


class _MultipleAdapterRuntime:
    gateway_names = (ADAPTER, "other-lark")

    def read_adapters(self) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(**row) for row in self.status())

    def read_adapter(self, name: str) -> SimpleNamespace | None:
        rows = [row for row in self.status(name) if row["name"] == name]
        return SimpleNamespace(**rows[0]) if rows else None

    def status(self, _name: str | None = None) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "name": name,
                "configured": True,
                "online": True,
                "status": "online",
            }
            for name in self.gateway_names
        )


def test_exactly_one_lark_pin_is_selected_among_non_lark_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    agent = app.agents.require("worker")
    monkeypatch.setattr(
        app.agents,
        "get",
        lambda _actor: replace(
            agent, pinned_adapters=("aaa-work", ADAPTER)
        ),
    )

    result = _send(app, operation="mixed-pins")

    assert result["deliveries"][0]["via"] == "agent-bot"
    assert len(gateway.calls) == 1
    assert fallback.requests == []


def test_multiple_lark_pins_are_ineligible_with_named_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    agent = app.agents.require("worker")
    monkeypatch.setattr(
        app.agents,
        "get",
        lambda _actor: replace(
            agent, pinned_adapters=(ADAPTER, "other-lark")
        ),
    )
    app._adapters = _MultipleAdapterRuntime()  # type: ignore[assignment]
    app._lark_client = LarkPortClient(app._adapters, None)  # type: ignore[arg-type]

    result = _send(app, operation="multiple-lark-pins")

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert gateway.calls == []
    assert _agent_bot_events(events)[0]["reason"] == "multiple-adapter-pins"


def test_no_lark_pin_uses_named_fallback_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = _delivery()
    app, _inbox = _application(tmp_path, fallback)
    _register_session(app, tmp_path)
    agent = app.agents.require("worker")
    monkeypatch.setattr(
        app.agents,
        "get",
        lambda _actor: replace(agent, pinned_adapters=("work",)),
    )
    app._adapters = _AdapterRuntime()  # type: ignore[assignment]
    app._lark_client = LarkPortClient(app._adapters, None)  # type: ignore[arg-type]
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )

    result = _send(app, operation="no-lark-pin")

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert _agent_bot_events(events)[0]["reason"] == "sender-has-no-lark-pin"


def test_watchdog_style_send_emits_no_agent_bot_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = _delivery()
    app, _inbox = _application(tmp_path, fallback)
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(
        app,
        "_authenticated_message_caller",
        lambda *_args, **_kwargs: SimpleNamespace(
            sender="inbox-watchdog",
            subject="inbox-watchdog",
            via="daemon",
            on_behalf_of=None,
        ),
    )

    result = app._ipc_message_send(
        "message.send",
        {
            "from": "inbox-watchdog",
            "to": ["user:bob"],
            "summary": "watchdog alert",
            "message": "watchdog alert",
            "idempotencyKey": "watchdog-alert",
        },
        "daemon",
    )

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert _agent_bot_events(events) == []


def test_inflight_record_survives_a_new_application_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(SystemExit("simulated daemon exit"))
    first, first_fallback, _events = _eligible_app(
        tmp_path, monkeypatch, gateway=gateway
    )
    with pytest.raises(SystemExit, match="simulated daemon exit"):
        _send(first, operation="restart-inflight")
    first._adapters = None
    first._close()
    first._close_home_lease()
    _TEST_APPLICATIONS.remove(first)

    second_fallback = _delivery()
    second = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="sender-mac",
        owner="alice",
        **_deep_module_daemon_dependencies(),
    )
    _TEST_APPLICATIONS.append(second)
    second._inbox = FakeInbox()
    second._harnesses = object()  # type: ignore[assignment]
    second._presence = object()  # type: ignore[assignment]
    second._user_delivery = second_fallback
    second._adapters = _AdapterRuntime(online=False)  # type: ignore[assignment]
    _register_session(second, tmp_path)

    replay = _send(second, operation="restart-inflight")

    assert replay["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert first_fallback.requests == []
    assert second_fallback.requests == []


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param('{"schemaVersion": 9, "attempts": {}}', id="unknown-schema"),
        pytest.param('{"schemaVersion": 1, "attempts": {"k": 7}}', id="bad-row"),
    ],
)
def test_unreadable_attempt_ledger_is_a_typed_refusal_without_any_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: str
) -> None:
    """Prior attempts are unknown, so neither the bot nor the proxy may send."""
    from hyprial.kernel import DaemonRequestError

    gateway = _Gateway()
    app, fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    ledger_path = app._agent_bot_attempts.path
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(DaemonRequestError) as raised:
        _send(app, operation="corrupt-ledger")

    assert raised.value.code == "USER_DELIVERY_UNAVAILABLE"
    assert gateway.calls == []
    assert fallback.requests == []
