"""targets 承诺的腿:只列 agent/user,不列 route;route: 发送被拒(I4b)。

route:<adapter>:<route> 不再是地址族:targets 不列 route 行,`--kind channel_route`
是 INVALID_ARGUMENT,向 route: 发送是 UNSUPPORTED_TARGET。route 投递的发送方标注由
推送原语承担(见 tests/daemon/routing/front_door)。
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.configuration.identity import write_settings_identity
from hyprial.identity import DeviceRecord, write_device_record
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

NODE = "node-1"
OWNER = "op"
URI = f"agent:{OWNER}:{NODE}:foo"


class FakeChatGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.file_calls: list[tuple[str, str, bytes, str, str]] = []

    def send_chat(self, chat_id: str, text: str, *, idempotency_key: str) -> str:
        self.calls.append((chat_id, text, idempotency_key))
        return f"om-route-{len(self.calls)}"

    def send_chat_file(
        self,
        chat_id: str,
        name: str,
        data: bytes,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.file_calls.append((chat_id, name, data, media_type, idempotency_key))
        return {"kind": "file", "nativeMessageId": "om-route-file-1"}

    def send_chat_image(
        self,
        chat_id: str,
        name: str,
        data: bytes,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        return {"kind": "image", "nativeMessageId": "om-route-image-1"}


class _StubPresence:
    def actor_online(self, actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "hq-adjutant-feishu",
                        "appId": "cli_test_app",
                        "credentialRef": "lark-test",
                        "routes": [
                            {
                                "name": "allen-dm",
                                "nativeId": "oc_dm",
                                "type": "direct",
                            },
                            {
                                "name": "all-member",
                                "nativeId": "oc_group",
                                "type": "direct",
                            },
                            {
                                "name": "both",
                                "type": "fanout",
                                "members": ["allen-dm", "all-member"],
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (home / "secrets" / "lark-test.json").write_text(
        json.dumps({"appSecret": "isolated-secret"}), encoding="utf-8"
    )
    return home


def _application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway: FakeChatGateway,
) -> DaemonApplication:
    home = _home(tmp_path)
    state = home / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]
    from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport

    app._inbox = InboxService(state / "inbox.sqlite3", MemoryDeliveryTransport())
    # resolve-or-reject (Allen b): the probe sender must be registered.
    app.handle("agent.create", {"name": "wk-top"})
    monkeypatch.setattr(
        LarkSdkGateway, "from_credentials", lambda app_id, secret: gateway
    )
    return app


def _official(app: DaemonApplication, name: str) -> str:
    """The id-form address a registered agent is reached and attributed by."""

    agent = app.agents.require(name)
    assert agent.uri == f"agent:{OWNER}:{NODE}:{agent.agent_id}"
    return agent.uri


# -- route 发送方文本标注 ------------------------------------------------------










def test_round_4_seeded_identity_lark_address_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No send address reaches the configured Lark exit.

    Bare and canonical agent spellings name the agent transport, where this
    adapter has no registry identity. The bare spelling keeps its historical
    target-kind refusal; the canonical spelling names a local agent address
    nothing on this node claims, which id-form addressing refuses at the send
    boundary as a fatal ``TARGET_UNRESOLVED`` (UUID addressing §2.3, HQ Q2)
    instead of queueing a row nobody drains. ``adapter:`` is a reply-bridge
    identity, not a new-send target.
    """

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    probe = app.agents.get("wk-top")
    assert probe is not None
    sender = probe.uri
    write_settings_identity(
        OWNER,
        mode="casdoor",
        issuer="https://auth.hyprial.test",
        user_id="user-a",
        username=OWNER,
        hyprial_home=app.hyprial_home,
    )
    write_device_record(
        app.hyprial_home,
        DeviceRecord(
            device_id=NODE,
            owner=OWNER,
            server_public="nodekey:server",
            client_public="nodekey:client",
            device_uid="11111111111141118111111111111111",
            name=NODE,
        ),
    )

    cases = (
        ("hq-adjutant-feishu", "TARGET_IS_NODE"),
        ("adapter:lark:hq-adjutant-feishu", "UNSUPPORTED_TARGET"),
    )
    for target, code in cases:
        with pytest.raises(DaemonRequestError) as failure:
            app.handle(
                "message.send",
                {
                    "from": sender,
                    "to": [target],
                    "summary": "hi",
                    "message": "hi",
                },
            )
        assert failure.value.code == code

    canonical = "agent:op:node-1:hq-adjutant-feishu"
    with pytest.raises(DaemonRequestError) as unresolved:
        app.handle(
            "message.send",
            {
                "from": sender,
                "to": [canonical],
                "summary": "hi",
                "message": "hi",
            },
        )
    assert unresolved.value.code == "TARGET_UNRESOLVED"
    assert unresolved.value.data == {"target": canonical}
    assert app._inbox.outbox_items() == ()
    assert gateway.calls == []

    with pytest.raises(DaemonRequestError) as retired:
        app.handle(
            "message.send",
            {
                "from": sender,
                "to": ["route:hq-adjutant-feishu:allen-dm"],
                "summary": "hi",
                "message": "hi",
            },
        )
    assert retired.value.code == "UNSUPPORTED_TARGET"
    assert retired.value.data == {"target": "route:hq-adjutant-feishu:allen-dm"}
    assert gateway.calls == []






def test_targets_never_list_route_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _application(tmp_path, monkeypatch, FakeChatGateway())
    app.handle("agent.create", {"name": "foo"})

    rows = app.handle("targets", {})["targets"]

    assert not [row for row in rows if row["targetUri"].startswith("route:")]
    assert "channel_route" not in {row["targetKind"] for row in rows}


def test_targets_kind_filter_covers_the_listable_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    wk_top = _official(app, "wk-top")
    app.handle("agent.create", {"name": "foo"})

    by_kind = {
        kind: app.handle("targets", {"kind": kind})["targets"]
        for kind in ("agent", "user")
    }
    assert {row["targetUri"] for row in by_kind["agent"]} == {
        _official(app, "foo"),
        wk_top,
    }
    assert by_kind["user"] == []

    for refused in ("channel_route", "host"):
        with pytest.raises(DaemonRequestError) as failure:
            app.handle("targets", {"kind": refused})
        assert failure.value.code == "INVALID_ARGUMENT"


def test_targets_full_list_holds_only_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    app.handle("agent.create", {"name": "foo"})

    kinds = {
        row["targetKind"] for row in app.handle("targets", {})["targets"]
    }
    assert kinds == {"agent"}
    # Every listed row keeps the deliverable promise.
    assert all(
        row["deliverable"] is True
        for row in app.handle("targets", {})["targets"]
    )


def test_targets_cli_accepts_the_new_kinds(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        _cli_support, "_daemon_request", lambda *_a, **_k: {"targets": []}
    )
    runner = CliRunner()

    # R2/B9: the CLI filter vocabulary is agent/user only (channel_route rows
    # still appear unfiltered; the filter moved to `adapter route list`).
    for kind in ("agent", "user"):
        assert runner.invoke(_cli_root_app, ["message", "targets", "--kind", kind]).exit_code == 0
    for kind in ("channel_route", "host"):
        assert runner.invoke(_cli_root_app, ["message", "targets", "--kind", kind]).exit_code == 1
