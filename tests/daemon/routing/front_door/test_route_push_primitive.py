"""``push_to_adapter_route``: the delivery primitive keyed by (adapter, route).

Decision notes (I4b): these cases were the ``route:`` front door's pinned
behaviour; the front door is gone and the primitive is the only delivery
body, so what it posts, under which keys, and how each failure is classified
is pinned here directly.  The ``PushFailure`` reasons are the ones the cell
push folds into ``pushCode``.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError, LarkSdkError
from hyprial.daemon.impl.network.route_delivery import RouteResource
from hyprial.daemon.impl.network.route_push import (
    NOT_SENT,
    UNKNOWN,
    PushFailure,
    push_to_adapter_route,
)
from hyprial.kernel import PersistentConfigError
from tests.daemon.routing.front_door.support import ADAPTER, Gateway, make_app
from tests.support.runtime._owned_applications import owned_applications as owned_applications

SENDER = "agent:alice:fd-mac:worker"


def _push(app, route, **extra):
    return push_to_adapter_route(
        app, adapter=extra.pop("adapter", ADAPTER), route=route, text="hello",
        sender=SENDER, idempotency_key="op-1", **extra,
    )


def _failure(app, route, **extra) -> PushFailure:
    with pytest.raises(PushFailure) as raised:
        _push(app, route, **extra)
    return raised.value


def test_a_direct_route_posts_the_attributed_text_under_the_caller_key(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    pushed = _push(app, "dm")

    assert (pushed.adapter, pushed.route, pushed.fanout) == (ADAPTER, "dm", False)
    assert [(post.route, post.chat_id, post.key, post.native_message_id) for post in pushed.posts] == [
        ("dm", "oc_dm", "op-1", "om-1"),
    ]
    assert gateway.calls == [("oc_dm", f"转述自 {SENDER}：\n\nhello", "op-1")]


def test_attribute_false_posts_the_text_unchanged(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    _push(app, "dm", attribute=False)

    assert gateway.calls == [("oc_dm", "hello", "op-1")]


def test_fanout_posts_each_chat_under_its_member_key(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    pushed = _push(app, "both")

    assert pushed.fanout is True
    assert [post.route for post in pushed.posts] == ["dm", "group"]
    assert [call[0] for call in gateway.calls] == ["oc_dm", "oc_group"]
    assert [call[2] for call in gateway.calls] == ["op-1:dm", "op-1:group"]
    for _chat, text, _key in gateway.calls:
        assert text.startswith(f"转述自 {SENDER}：")


def test_a_replay_posts_under_the_same_keys(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    _push(app, "dm")
    _push(app, "dm")

    assert gateway.calls[0][2] == gateway.calls[1][2] == "op-1"


def test_an_absent_route_uses_the_gateway_default(tmp_path, monkeypatch) -> None:
    from tests.support.identity.cell_push import edit_gateway

    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)
    edit_gateway(app, defaultRoute="group")

    pushed = _push(app, None)

    assert pushed.route == "group"
    assert [call[0] for call in gateway.calls] == ["oc_group"]


def test_stopping_at_the_first_failure_leaves_the_next_chat_unposted(tmp_path, monkeypatch) -> None:
    gateway = Gateway({"oc_dm": LarkApiError("send message", 230002, "not in chat")})
    app = make_app(tmp_path, monkeypatch, gateway)

    failure = _failure(app, "both")

    assert (failure.state, failure.reason, failure.route, failure.chat_id) == (
        NOT_SENT, "provider-refused", "dm", "oc_dm",
    )
    assert failure.posts == ()
    assert isinstance(failure.__cause__, LarkApiError)
    assert [call[0] for call in gateway.calls] == ["oc_dm"]


def test_not_stopping_attempts_every_chat_and_reports_the_posted_ones(tmp_path, monkeypatch) -> None:
    gateway = Gateway({"oc_dm": LarkApiError("send message", 230002, "not in chat")})
    app = make_app(tmp_path, monkeypatch, gateway)

    failure = _failure(app, "both", stop_at_first_failure=False)

    assert [call[0] for call in gateway.calls] == ["oc_dm", "oc_group"]
    assert [post.chat_id for post in failure.posts] == ["oc_group"]
    assert failure.reason == "provider-refused"


@pytest.mark.parametrize(
    ("adapter", "route", "reason"),
    [
        ("nobody", "dm", "adapter-unconfigured"),
        (ADAPTER, "missing", "route-unconfigured"),
        (ADAPTER, "nested", "route-fanout-invalid"),
        (ADAPTER, None, "no-default-route"),
    ],
)
def test_configuration_gaps_are_not_sent_and_post_nothing(
    tmp_path, monkeypatch, adapter, route, reason
) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    failure = _failure(app, route, adapter=adapter)

    assert (failure.state, failure.reason) == (NOT_SENT, reason)
    assert gateway.posted == 0


@pytest.mark.parametrize(
    ("error", "state", "reason"),
    [
        (LarkApiError("send message", 999999, "boom"), NOT_SENT, "provider-refused"),
        (LarkApiError("send message", 230002, "x"), NOT_SENT, "provider-refused"),
        (RuntimeError("socket closed"), UNKNOWN, "outcome-unknown"),
        (LarkSdkError("send message returned no message id"), UNKNOWN, "outcome-unknown"),
    ],
    ids=["lark-catch-all", "not-in-chat", "transport", "no-message-id"],
)
def test_post_failures_are_classified_and_chain_the_cause(
    tmp_path, monkeypatch, error, state, reason
) -> None:
    app = make_app(tmp_path, monkeypatch, Gateway({"oc_dm": error}))

    failure = _failure(app, "dm")

    assert (failure.state, failure.reason, failure.route, failure.chat_id) == (
        state, reason, "dm", "oc_dm",
    )
    assert failure.__cause__ is error


def test_a_missing_secret_file_is_adapter_unavailable(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway, secret=None)

    failure = _failure(app, "dm")

    assert (failure.state, failure.reason) == (NOT_SENT, "adapter-unavailable")
    assert isinstance(failure.__cause__, PersistentConfigError)
    assert gateway.posted == 0


def test_a_secret_without_app_secret_is_adapter_unavailable(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway, secret={"other": "x"})

    failure = _failure(app, "dm")

    assert (failure.state, failure.reason) == (NOT_SENT, "adapter-unavailable")
    assert gateway.posted == 0


def test_resources_follow_the_text_under_resource_keys(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)
    resources = (
        RouteResource("proof.txt", "file", "text/plain", b"proof"),
        RouteResource("pixel.png", "image", "image/png", b"png"),
    )

    pushed = _push(app, "dm", resources=resources)

    assert gateway.file_calls == [("oc_dm", "proof.txt", b"proof", "text/plain", "op-1:resource:0")]
    assert gateway.image_calls == [("oc_dm", "pixel.png", b"png", "image/png", "op-1:resource:1")]
    [post] = pushed.posts
    assert [row["kind"] for row in post.resource_deliveries] == ["file", "image"]


def test_fanout_resources_use_each_members_key(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    _push(app, "both", resources=(RouteResource("proof.txt", "file", "text/plain", b"proof"),))

    assert [(call[0], call[4]) for call in gateway.file_calls] == [
        ("oc_dm", "op-1:dm:resource:0"), ("oc_group", "op-1:group:resource:0"),
    ]
    assert [call[0] for call in gateway.calls] == ["oc_dm", "oc_group"]
