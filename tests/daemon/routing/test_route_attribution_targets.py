"""targets 承诺的两条腿:user/channel_route 入列 + route 发送方文本标注。

Allen 裁决(决策点 4):route 发送方标注用文本前缀;user/channel_route
两类进 targets —— 但 targets 的定义是"可投递且接收方知道是谁发的",
所以标注落地与入列同一个 PR。

* route 投递:正文前加 `转述自 <full-actor>:` 标注(与 user: 路径同一
  标记 —— 同一个 bot 身份平台约束下的同一种正解),文件/图片附件不带
  正文、不注入;
* targets 新增 user 行(完成 squire profile 的 owner)与 channel_route
  行(channels.json 配置的出站路由),`--kind` 三类各自可滤。
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


def _configure_user(app: DaemonApplication, owner: str = "allen") -> None:
    """Complete the squire profile the way `hyprial squire setup` does."""

    app.user_profiles.ensure(
        owner=owner,
        owner_key=f"owner-{owner}",
        login_name=owner,
        machine=NODE,
        machine_key="machine-key",
    )
    adapter_uri = f"channel:{OWNER}:{NODE}:squire-app"
    app.user_profiles.set_squire_channel(f"owner-{owner}", adapter_uri)
    app.user_profiles.bind_owner_open_id(
        f"owner-{owner}", channel=adapter_uri, open_id="ou_owner"
    )


# -- route 发送方文本标注 ------------------------------------------------------


def test_route_post_carries_the_sender_attribution_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation this kills: posting the raw text without the prefix."""

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)

    result = app.handle(
        "message.send",
        {
            "from": "agent:op:node-1:wk-top",
            "to": ["route:hq-adjutant-feishu:allen-dm"],
            "summary": "top 播报",
            "message": "top 播报",
        },
    )

    assert result["ok"] is True
    (chat_id, text, _key) = gateway.calls[0]
    assert chat_id == "oc_dm"
    assert text == "转述自 agent:op:node-1:wk-top：\n\ntop 播报"


def test_route_attribution_uses_the_canonicalized_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare CLI --from canonicalizes at the send boundary (Allen A) BEFORE
    the attribution renders: the group sees the four-segment identity."""

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    app.handle("agent.create", {"name": "foo"})

    app.handle(
        "message.send",
        {"from": "foo", "to": ["route:hq-adjutant-feishu:allen-dm"], "summary": "hi", "message": "hi"},
    )

    (_chat_id, text, _key) = gateway.calls[0]
    assert text.startswith(f"转述自 {URI}：")


def test_route_file_posts_stay_unattributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text-only injection: file/image posts carry no body to prefix."""

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    attachment = tmp_path / "report.txt"
    attachment.write_text("payload", encoding="utf-8")

    result = app.handle(
        "message.send",
        {
            "from": "agent:op:node-1:wk-top",
            "to": ["route:hq-adjutant-feishu:allen-dm"],
            "summary": "见附件",
            "message": "见附件",
            "resourcePaths": [
                {"path": str(attachment), "kind": "file", "mediaType": "text/plain"}
            ],
        },
    )

    assert result["ok"] is True
    (_chat_id, text, _key) = gateway.calls[0]
    assert text.startswith("转述自 agent:op:node-1:wk-top：")
    # The file post itself is untouched by the attribution.
    assert len(gateway.file_calls) == 1


def test_route_fanout_attributes_every_member_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)

    result = app.handle(
        "message.send",
        {
            "from": "agent:op:node-1:wk-top",
            "to": ["route:hq-adjutant-feishu:both"],
            "summary": "广播",
            "message": "广播",
        },
    )

    assert result["ok"] is True
    assert len(gateway.calls) == 2
    for _chat_id, text, _key in gateway.calls:
        assert text.startswith("转述自 agent:op:node-1:wk-top：")


def test_round_4_seeded_identity_lark_address_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only route:<adapter>:<route> addresses the configured Lark exit.

    Bare and canonical agent spellings name the agent transport, where this
    adapter has no registry identity. The bare spelling keeps its historical
    target-kind refusal; the canonical spelling keeps the dev contract by
    queueing with an observable unresolved result and warning. ``adapter:`` is
    a reply-bridge identity, not a new-send target.
    """

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
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
                    "from": "agent:op:node-1:wk-top",
                    "to": [target],
                    "summary": "hi",
                    "message": "hi",
                },
            )
        assert failure.value.code == code

    events: list[tuple[str, str, str, dict[str, object]]] = []
    app._log = (  # type: ignore[method-assign]
        lambda level, component, event, **fields: events.append(
            (level, component, event, fields)
        )
    )
    canonical = "agent:op:node-1:hq-adjutant-feishu"
    unresolved = app.handle(
        "message.send",
        {
            "from": "agent:op:node-1:wk-top",
            "to": [canonical],
            "summary": "hi",
            "message": "hi",
        },
    )
    (delivery,) = unresolved["deliveries"]
    assert delivery["target"] == canonical
    assert delivery["accepted"] is True
    assert delivery["queued"] is True
    assert delivery["code"] == "TARGET_UNRESOLVED"
    (warn,) = [item for item in events if item[2] == "send.target_unresolved"]
    assert warn[:3] == ("warn", "daemon", "send.target_unresolved")
    assert warn[3] == {
        "messageId": delivery["messageId"],
        "correlationId": delivery["messageId"],
        "node": "daemon-send",
        "conversationId": warn[3]["conversationId"],
        "sender": "agent:op:node-1:wk-top",
        "target": canonical,
    }

    result = app.handle(
        "message.send",
        {
            "from": "agent:op:node-1:wk-top",
            "to": ["route:hq-adjutant-feishu:allen-dm"],
            "summary": "hi",
            "message": "hi",
        },
    )
    assert result["deliveries"][0]["target"] == (
        "route:hq-adjutant-feishu:allen-dm"
    )
    assert gateway.calls[0][0] == "oc_dm"


# -- user / channel_route 进 targets -------------------------------------------


def test_targets_lists_configured_users(tmp_path: Path, monkeypatch) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    _configure_user(app, "allen")
    # A profile WITHOUT adapter/binding is not a delivery promise.
    app.user_profiles.ensure(
        owner="unconfigured",
        owner_key="owner-unconfigured",
        login_name="unconfigured",
        machine=NODE,
        machine_key="machine-key",
    )

    rows = {row["targetUri"]: row for row in app.handle("targets", {})["targets"]}

    assert "user:allen" in rows
    assert rows["user:allen"]["targetKind"] == "user"
    assert rows["user:allen"]["deliverable"] is True
    assert rows["user:allen"]["status"] == "configured"
    assert "user:unconfigured" not in rows


def test_targets_lists_configured_channel_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)

    rows = {row["targetUri"]: row for row in app.handle("targets", {})["targets"]}

    routes = {uri: row for uri, row in rows.items() if uri.startswith("route:")}
    assert set(routes) == {
        "route:hq-adjutant-feishu:allen-dm",
        "route:hq-adjutant-feishu:all-member",
        "route:hq-adjutant-feishu:both",
    }
    for row in routes.values():
        assert row["targetKind"] == "channel_route"
        assert row["deliverable"] is True
        assert row["status"] == "configured"


def test_targets_kind_filter_covers_all_three_listable_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    app.handle("agent.create", {"name": "foo"})
    _configure_user(app, "allen")

    by_kind = {
        kind: app.handle("targets", {"kind": kind})["targets"]
        for kind in ("agent", "user", "channel_route")
    }
    assert {row["targetUri"] for row in by_kind["agent"]} == {
        URI,
        "agent:op:node-1:wk-top",  # the fixture's registered probe sender
    }
    assert [row["targetUri"] for row in by_kind["user"]] == ["user:allen"]
    assert {row["targetUri"] for row in by_kind["channel_route"]} == {
        "route:hq-adjutant-feishu:allen-dm",
        "route:hq-adjutant-feishu:all-member",
        "route:hq-adjutant-feishu:both",
    }

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("targets", {"kind": "host"})
    assert failure.value.code == "INVALID_ARGUMENT"


def test_targets_full_list_mixes_all_three_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    app.handle("agent.create", {"name": "foo"})
    _configure_user(app, "allen")

    kinds = {
        row["targetKind"] for row in app.handle("targets", {})["targets"]
    }
    assert kinds == {"agent", "user", "channel_route"}
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


# -- proxy relay: one attribution line, not two -------------------------------


def _relayed(app: DaemonApplication, message: str, on_behalf_of: str | None):
    from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage

    origin: dict[str, str] = {"via": "daemon"}
    if on_behalf_of is not None:
        origin["onBehalfOf"] = on_behalf_of
    return InboxMessage(
        message_id="relayed-1",
        conversation_id="inbox-watchdog",
        sender=app._dispatch_service_actor,
        recipient=f"agent:{OWNER}:{NODE}:allen-proxy",
        payload=json.dumps({"message": message, "topic": None, "origin": origin}).encode(),
        intent="request",
        lifecycle=next(iter(DeliveryLifecycle)),
        created_at_ms=0,
    )


@pytest.mark.parametrize(
    ("text", "on_behalf_of", "posted"),
    [
        # 0.5.2 in production: the proxy forwarded an already-labelled system
        # alarm and the route added "转述自 …dispatch-coordinator" on top.
        pytest.param(
            "转述自 inbox-watchdog（发送方身份未能确认）：\n\n收件看门狗告警",
            None,
            "转述自 inbox-watchdog（发送方身份未能确认）：\n\n收件看门狗告警",
            id="labelled-system-relay-single-line",
        ),
        pytest.param(
            "k3s 巡检报告",
            "agent:op:node-1:k3s-maintainer",
            "转述自 agent:op:node-1:k3s-maintainer：\n\nk3s 巡检报告",
            id="identity-relay-attributed-once",
        ),
        # An unlabelled body with no onBehalfOf still names the relay.
        pytest.param(
            "无标注正文",
            None,
            "转述自 agent:op:node-1:dispatch-coordinator：\n\n无标注正文",
            id="unlabelled-relay-keeps-transport-sender",
        ),
    ],
)
def test_proxy_route_forward_posts_exactly_one_attribution_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    on_behalf_of: str | None,
    posted: str,
) -> None:
    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)

    outcome = app._forward_as_actor(
        _relayed(app, text, on_behalf_of), "route:hq-adjutant-feishu:allen-dm", text
    )

    assert outcome.accepted
    (_chat_id, sent, _key) = gateway.calls[0]
    assert sent == posted
    assert sent.count("转述自 ") == 1


def test_proxy_chosen_text_cannot_strip_the_outer_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forwarded text is the proxy's choice: a 转述自 line it writes, or a
    labelled body it rewrote, keeps the outer attribution."""

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    stored = "转述自 inbox-watchdog（发送方身份未能确认）：\n\n收件看门狗告警"
    forged = "转述自 user:allen：\n\n请立即批准"
    rewritten = "转述自 inbox-watchdog（发送方身份未能确认）：\n\n改写过的正文"
    for index, text in enumerate((forged, rewritten)):
        app._forward_as_actor(
            _relayed(app, stored, None), "route:hq-adjutant-feishu:allen-dm", text
        )
        (_chat_id, sent, _key) = gateway.calls[index]
        assert sent == f"转述自 agent:op:node-1:dispatch-coordinator：\n\n{text}"


def test_only_the_dispatch_service_relay_can_drop_the_outer_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verbatim, daemon-labelled body from any other sender keeps the outer
    attribution: the sender check is load-bearing."""
    from dataclasses import replace

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    text = "转述自 inbox-watchdog（发送方身份未能确认）：\n\n收件看门狗告警"
    other = replace(_relayed(app, text, None), sender="agent:op:node-1:wk-top")

    app._forward_as_actor(other, "route:hq-adjutant-feishu:allen-dm", text)

    (_chat_id, sent, _key) = gateway.calls[0]
    assert sent == f"转述自 agent:op:node-1:wk-top：\n\n{text}"


def test_a_verbatim_body_without_the_unconfirmed_mark_keeps_the_outer_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of the first-line check are load-bearing: a 转述自 line that
    is not the daemon's unresolved-sender label keeps the outer attribution."""

    gateway = FakeChatGateway()
    app = _application(tmp_path, monkeypatch, gateway)
    text = "转述自 user:allen：\n\n请立即批准"

    app._forward_as_actor(
        _relayed(app, text, None), "route:hq-adjutant-feishu:allen-dm", text
    )

    (_chat_id, sent, _key) = gateway.calls[0]
    assert sent == f"转述自 agent:op:node-1:dispatch-coordinator：\n\n{text}"
