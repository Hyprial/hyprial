"""F1c (Allen 2026-11-23, Q0-b): the ``channel:`` adapter-address spelling is
retired.  Every adapter writes ``adapter:``; a ``channel:`` address is no longer
read as ``adapter:`` at any of the former dual-read points, and falls through
to the same path as any unknown scheme.  S3c deleted the last reader
(``parse_channel_uri``).
"""

from __future__ import annotations

import pytest

from hyprial.daemon import audience_for_sender
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_alarm_destination,
    lark_reply_adapter,
)
from hyprial.daemon.impl.api import visible_message_origin
from hyprial.daemon.impl.conversations.ids import endpoint_key
from hyprial.daemon.impl.conversations.keys import direct_endpoint
from hyprial.daemon.impl.conversations.record.contracts import pair_endpoint
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle
from hyprial.daemon.impl.ipc.wire import _lifecycle
import hyprial.kernel as kernel
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    parse_adapter_short_uri,
    parse_adapter_uri,
    short_actor_name,
)


def test_the_kernel_no_longer_exports_a_channel_prefix() -> None:
    assert ADAPTER_URI_PREFIX == "adapter:"
    assert not hasattr(kernel, "CHANNEL_URI_PREFIX")


def test_parse_adapter_uri_reads_only_the_adapter_spelling() -> None:
    assert parse_adapter_uri("adapter:allen:mac:lark-main") == (
        "allen",
        "mac",
        "lark-main",
    )
    assert parse_adapter_uri("channel:allen:mac:lark-main") is None
    assert parse_adapter_uri("adapter:allen:mac") is None
    assert parse_adapter_short_uri("adapter:lark:main") == ("lark", "main")
    assert parse_adapter_short_uri("channel:lark:main") is None


def test_short_actor_name_leaves_a_channel_address_verbatim() -> None:
    assert short_actor_name("adapter:allen:mac:lark-main") == "lark-main"
    assert short_actor_name("channel:allen:mac:lark-main") == (
        "channel:allen:mac:lark-main"
    )


def test_lark_reply_bridge_reads_only_the_adapter_spelling() -> None:
    assert lark_reply_adapter("adapter:lark:squire") == "squire"
    assert lark_reply_adapter("channel:lark:squire") is None
    assert lark_reply_adapter("adapter:lark:") is None
    assert lark_reply_adapter("adapter:lark:squire:extra") is None
    assert lark_reply_adapter("agent:allen:mac:squire") is None


def test_lark_alarm_destination_accepts_only_the_reply_bridge_shape() -> None:
    assert lark_alarm_destination("adapter:lark:squire") == "squire"
    assert lark_alarm_destination("channel:lark:squire") is None
    assert lark_alarm_destination("route:hyprial-introducer-feishu:customer-dm") is None


def test_lark_alarm_destination_rejects_shapes_without_a_chat_handle() -> None:
    assert lark_alarm_destination("user:bob.smith@example.com") is None
    assert lark_alarm_destination("agent:allen:mac:squire") is None
    assert lark_alarm_destination("squire") is None
    assert lark_alarm_destination("route:") is None
    assert lark_alarm_destination("route:only-adapter") is None
    assert lark_alarm_destination("route:a:b:c") is None


def test_audience_for_sender_treats_a_channel_sender_as_an_agent() -> None:
    assert audience_for_sender("adapter:lark:squire") == "human"
    assert audience_for_sender("channel:lark:squire") == "agent"
    assert audience_for_sender("agent:allen:mac:pi-worker") == "agent"


def test_wire_lifecycle_default_is_durable_only_for_adapter_recipients() -> None:
    assert (
        _lifecycle(None, "adapter:allen:mac:lark-main")
        == DeliveryLifecycle.DURABLE_SERVICE
    )
    assert (
        _lifecycle(None, "channel:allen:mac:lark-main")
        == DeliveryLifecycle.ONLINE_ONLY
    )


def test_visible_origin_no_longer_labels_a_channel_sender_as_adapter() -> None:
    assert visible_message_origin({}, "adapter:lark:main") == {"via": "adapter"}
    assert visible_message_origin({}, "channel:lark:main") == {"via": "daemon"}


@pytest.mark.parametrize(
    "address",
    ["channel:allen:mac:lark-main", "channel:lark:main"],
)
def test_conversation_keys_do_not_rewrite_a_channel_address(address: str) -> None:
    assert endpoint_key(address) == address
    assert direct_endpoint(address) is None
    assert pair_endpoint(address) is None


def test_conversation_keys_still_read_the_adapter_spelling() -> None:
    assert endpoint_key("adapter:allen:mac:lark-main") == "adapter:allen:mac:lark-main"
    assert direct_endpoint("adapter:allen:mac:lark-main") is not None
    assert pair_endpoint("adapter:lark:main") == "adapter:lark:main"
