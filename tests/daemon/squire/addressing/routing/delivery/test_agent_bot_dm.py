"""A local pinned agent uses its own Lark bot for ``user:`` delivery."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.composition.ports import LarkPortClient

from hyprial.daemon.impl.adapters.lark import LarkApiError
from hyprial.daemon.impl.squire import UserDeliveryResult
from hyprial.identity import UserStore

from ..support import (
    FakeUserDelivery,
    _application,
    _close_test_applications as _close_test_applications,
)


ADAPTER = "worker-bot"
SENDER = "agent:alice:sender-mac:worker"
SESSION = "agent-bot-session"


class _AdapterRuntime:
    gateway_names = (ADAPTER,)

    def __init__(self, *, online: bool = True) -> None:
        self.online = online

    def read_adapters(self) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(**row) for row in self.status())

    def read_adapter(self, name: str) -> SimpleNamespace | None:
        rows = [row for row in self.status(name) if row["name"] == name]
        return SimpleNamespace(**rows[0]) if rows else None

    def status(self, _name: str | None = None) -> tuple[dict[str, object], ...]:
        return (
            {
                "name": ADAPTER,
                "configured": True,
                "online": self.online,
                "status": "online" if self.online else "stopped",
            },
        )


class _Gateway:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str]] = []
        self.posts: dict[str, str] = {}

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        self.calls.append((open_id, text, idempotency_key))
        if isinstance(self.error, LarkApiError):
            raise self.error
        self.posts.setdefault(idempotency_key, f"om-{len(self.posts) + 1}")
        if self.error is not None:
            raise self.error
        return self.posts[idempotency_key]

    def send_owner_dm_settled(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        return self.send_owner_dm(open_id, text, idempotency_key=idempotency_key)


def _delivery() -> FakeUserDelivery:
    return FakeUserDelivery(
        UserDeliveryResult(
            message_id="fallback-message",
            accepted=True,
            native_message_id="om-fallback",
        )
    )


def _register_session(app: Any, cwd: Path) -> None:
    workspace = cwd.parent / f"{cwd.name}-workspace"
    workspace.mkdir(exist_ok=True)
    app.handle(
        "session.register",
        {
            "actor": "worker",
            "sessionRef": SESSION,
            "cwd": str(workspace),
            "command": ["test-carrier"],
            "source": "test-channel",
            "runtime": "test_interactive",
        },
    )


def _seed_account(
    app: Any, *, adapter: str, open_id: str, owner: str = "alice"
) -> None:
    users = UserStore(
        app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home
    )
    try:
        if users.get_user(owner) is None:
            users.add_user(kind="member", owner=owner, confirmed_by="operator")
        users.bind_account(
            owner,
            adapter=f"lark:{adapter}",
            open_id=open_id,
            union_id="on_bob",
            confirmed_by="operator",
        )
    finally:
        users.close()


def _eligible_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gateway: _Gateway,
    online: bool = True,
    seed_open_id: bool = True,
) -> tuple[Any, FakeUserDelivery, list[tuple[str, dict[str, object]]]]:
    fallback = _delivery()
    app, _inbox = _application(tmp_path, fallback)
    _register_session(app, tmp_path)
    app.agents.pin(ADAPTER, "worker")
    app._adapters = _AdapterRuntime(online=online)  # type: ignore[assignment]
    app._lark_client = LarkPortClient(app._adapters, None)  # type: ignore[arg-type]
    configuration = SimpleNamespace(
        channels=SimpleNamespace(
            gateways=(SimpleNamespace(name=ADAPTER),),
        )
    )
    monkeypatch.setattr(app, "load_persistent_configuration", lambda: configuration)
    monkeypatch.setattr(app, "_route_lark_gateway", lambda _config: gateway)
    if seed_open_id:
        _seed_account(app, adapter=ADAPTER, open_id="ou_worker_bot")
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    return app, fallback, events


def _send(app: Any, *, operation: str = "operation-1") -> dict[str, object]:
    return app.handle(
        "message.send",
        {
            "from": SENDER,
            "sessionRef": SESSION,
            "to": ["user:alice"],
            "summary": "agent report",
            "message": "agent report",
            "idempotencyKey": operation,
        },
    )


def test_pinned_local_agent_dms_with_its_own_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, fallback, events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    result = _send(app)

    assert result["ok"] is True
    assert result["deliveries"] == [
        {
            "target": "user:alice",
            "messageId": result["messageId"],
            "accepted": True,
            "queued": False,
            "receiverAdapter": True,
            "nativeMessageId": "om-1",
            "via": "agent-bot",
            "adapter": ADAPTER,
        }
    ]
    assert gateway.calls == [
        (
            "ou_worker_bot",
            "agent report",
            "operation-1:user:alice:agent-bot:worker-bot",
        )
    ]
    assert "转述自" not in gateway.calls[0][1]
    assert fallback.requests == []
    assert [fields["outcome"] for event, fields in events if event == "user-delivery.agent-bot"] == [
        "delivered"
    ]


@pytest.mark.parametrize(
    "failure",
    ["no-open-id", "not-running", "lark-refused"],
)
def test_definitely_not_sent_agent_bot_attempt_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    error = (
        LarkApiError(
            "send owner DM",
            99991679,
            "missing scope",
            missing_scopes=("im:message",),
        )
        if failure == "lark-refused"
        else None
    )
    gateway = _Gateway(error)
    app, fallback, events = _eligible_app(
        tmp_path,
        monkeypatch,
        gateway=gateway,
        online=failure != "not-running",
        seed_open_id=failure != "no-open-id",
    )

    result = _send(app)

    assert result["ok"] is True
    assert fallback.requests[0].sender == SENDER
    assert fallback.requests[0].idempotency_key == "operation-1:user:alice"
    assert result["deliveries"][0]["nativeMessageId"] == "om-fallback"
    assert [fields["outcome"] for event, fields in events if event == "user-delivery.agent-bot"] == [
        "fallback"
    ]


def test_timeout_is_outcome_unknown_and_retry_keeps_one_platform_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(TimeoutError("request may have reached Lark"))
    app, fallback, events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)

    first = _send(app, operation="retry-key")
    second = _send(app, operation="retry-key")

    for result in (first, second):
        assert result["ok"] is False
        assert result["deliveries"][0]["accepted"] is False
        assert result["deliveries"][0]["outcomeKnown"] is False
        assert result["deliveries"][0]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert fallback.requests == []
    assert {call[2] for call in gateway.calls} == {
        "retry-key:user:alice:agent-bot:worker-bot"
    }
    assert len(gateway.posts) == 1
    assert [fields["outcome"] for event, fields in events if event == "user-delivery.agent-bot"] == [
        "uncertain",
        "uncertain",
    ]


def test_owner_open_id_is_selected_for_the_pinned_app_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway()
    app, _fallback, _events = _eligible_app(tmp_path, monkeypatch, gateway=gateway)
    _seed_account(app, adapter="other-bot", open_id="ou_other_bot")

    _send(app)

    assert gateway.calls[0][0] == "ou_worker_bot"


@pytest.mark.parametrize(
    "sender_kind",
    ["system-service", "operator", "dispatch", "remote-agent", "agent-without-pin"],
)
def test_ineligible_sender_uses_existing_user_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender_kind: str,
) -> None:
    fallback = _delivery()
    app, _inbox = _application(tmp_path, fallback)
    gateway = _Gateway()
    app._adapters = _AdapterRuntime()  # type: ignore[assignment]
    app._lark_client = LarkPortClient(app._adapters, None)  # type: ignore[arg-type]
    monkeypatch.setattr(app, "_route_lark_gateway", lambda _config: gateway)
    params: dict[str, object] = {
        "to": ["user:bob"],
        "summary": "agent report",
        "message": "agent report",
        "idempotencyKey": "operation-1",
    }
    direct = False
    if sender_kind == "operator":
        params["from"] = "user:alice"
    elif sender_kind == "dispatch":
        params["from"] = app._dispatch_service_actor
    elif sender_kind == "agent-without-pin":
        params["from"] = SENDER
    else:
        direct = True
        sender = (
            "inbox-watchdog"
            if sender_kind == "system-service"
            else "agent:carol:remote-mac:worker"
        )
        params["from"] = sender
        monkeypatch.setattr(
            app,
            "_authenticated_message_caller",
            lambda *_args, **_kwargs: SimpleNamespace(
                sender=sender,
                subject=sender,
                via="daemon",
                on_behalf_of=None,
            ),
        )

    result = (
        app._ipc_message_send("message.send", params, "daemon")
        if direct
        else app.handle("message.send", params)
    )

    assert result["ok"] is True
    assert len(fallback.requests) == 1
    assert gateway.calls == []
    assert "via" not in result["deliveries"][0]
