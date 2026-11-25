"""Shared wiring for the route push primitive and the retired ``route:`` front door tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import AckResult, InboxMessage, SubmissionResult
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

ADAPTER = "front-door"
ROUTES: list[dict[str, object]] = [
    {"name": "dm", "type": "direct", "nativeId": "oc_dm"},
    {"name": "group", "type": "direct", "nativeId": "oc_group"},
    {"name": "both", "type": "fanout", "members": ["dm", "group"]},
    {"name": "nested", "type": "fanout", "members": ["dm", "both"]},
]


class Gateway:
    """Records each post; *failures* maps a chat id to the exception it raises."""

    def __init__(self, failures: dict[str, BaseException] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.file_calls: list[tuple[str, str, bytes, str, str]] = []
        self.image_calls: list[tuple[str, str, bytes, str, str]] = []
        self.failures = failures or {}

    def send_chat(self, chat_id: str, text: str, *, idempotency_key: str) -> str:
        self.calls.append((chat_id, text, idempotency_key))
        failure = self.failures.get(chat_id)
        if failure is not None:
            raise failure
        return f"om-{len(self.calls)}"

    def send_chat_file(
        self, chat_id: str, name: str, data: bytes, *, media_type: str, idempotency_key: str
    ) -> dict[str, object]:
        self.file_calls.append((chat_id, name, data, media_type, idempotency_key))
        return {"kind": "file", "fileName": name, "nativeMessageId": f"om-file-{len(self.file_calls)}"}

    def send_chat_image(
        self, chat_id: str, name: str, data: bytes, *, media_type: str, idempotency_key: str
    ) -> dict[str, object]:
        self.image_calls.append((chat_id, name, data, media_type, idempotency_key))
        return {"kind": "image", "fileName": name, "nativeMessageId": f"om-image-{len(self.image_calls)}"}

    @property
    def posted(self) -> int:
        return len(self.calls) + len(self.file_calls) + len(self.image_calls)


class Inbox:
    def __init__(self) -> None:
        self.submitted: list[InboxMessage] = []

    def pending_messages(self, _actor: str) -> tuple[InboxMessage, ...]:
        return ()

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        self.submitted.append(message)
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, _actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)


def home(tmp_path: Path, *, secret: Any = {"appSecret": "isolated"}) -> Path:
    root = tmp_path / "home"
    (root / "secrets").mkdir(parents=True)
    (root / "channels.json").write_text(json.dumps({
        "version": 1,
        "gateways": [{
            "provider": "lark", "name": ADAPTER, "appId": "cli_front_door",
            "credentialRef": f"lark-{ADAPTER}", "routes": ROUTES,
        }],
    }), encoding="utf-8")
    if secret is not None:
        (root / "secrets" / f"lark-{ADAPTER}.json").write_text(
            json.dumps(secret), encoding="utf-8"
        )
    return root


def make_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway: Gateway, **options: Any
) -> DaemonApplication:
    root = home(tmp_path, **options)
    state = root / "state"
    app = DaemonApplication(
        state_dir=state, socket_path=state / "daemon.sock", node_id="fd-mac",
        hyprial_home=root, owner="alice", **daemon_dependencies(),
    )
    app._inbox = Inbox()
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    monkeypatch.setattr(LarkSdkGateway, "from_credentials", lambda app_id, secret, **_: gateway)
    app.handle("agent.create", {"name": "worker"})
    return app
