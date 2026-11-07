"""Dual-read: ``channel:lark:``/``channel:<owner>:<machine>:<name>`` (legacy)
and ``adapter:lark:``/``adapter:<owner>:<machine>:<name>`` (current) parse to
the identical result at every one of the five real parse points (PAC
b8d2b52b6358 / NAMB efeeaf0a01ba naming migration). ``mapping.py:73``/``:374``
are excluded by Allen's ruling (IRC-codec, no production caller, disposed of
alongside IRC) and are not covered here.

Positive mutation check performed by hand while writing this file (not left
in the suite): commenting out the ``adapter:``/``ADAPTER_URI_PREFIX`` branch
in each of the five touch points turns every ``adapter:``-form assertion
below red while every ``channel:``-form assertion stays green — proving
these tests actually exercise the new branch, not just re-confirm the old
one.
"""

from __future__ import annotations

from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_alarm_destination,
    lark_reply_adapter,
)
from hyprial.daemon import audience_for_sender
from hyprial.daemon.impl.ipc.wire import _lifecycle
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    CHANNEL_URI_PREFIX,
    parse_channel_uri,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle


def test_channel_uri_prefixes_are_the_legacy_and_current_spellings() -> None:
    assert CHANNEL_URI_PREFIX == "channel:"
    assert ADAPTER_URI_PREFIX == "adapter:"


def test_parse_channel_uri_accepts_both_spellings_identically() -> None:
    legacy = parse_channel_uri("channel:allen:mac:lark-main")
    current = parse_channel_uri("adapter:allen:mac:lark-main")

    assert legacy == ("allen", "mac", "lark-main")
    assert current == legacy


def test_parse_channel_uri_rejects_neither_spelling() -> None:
    assert parse_channel_uri("route:allen:mac:lark-main") is None
    assert parse_channel_uri("channel:allen:mac") is None
    assert parse_channel_uri("adapter:allen:mac") is None


def test_lark_reply_adapter_accepts_both_spellings_identically() -> None:
    legacy = lark_reply_adapter("channel:lark:squire")
    current = lark_reply_adapter("adapter:lark:squire")

    assert legacy == "squire"
    assert current == legacy


def test_lark_reply_adapter_rejects_malformed_forms_of_either_spelling() -> None:
    assert lark_reply_adapter("channel:lark:") is None
    assert lark_reply_adapter("adapter:lark:") is None
    assert lark_reply_adapter("channel:lark:squire:extra") is None
    assert lark_reply_adapter("adapter:lark:squire:extra") is None
    assert lark_reply_adapter("agent:allen:mac:squire") is None


def test_lark_alarm_destination_accepts_reply_bridge_and_route_shapes() -> None:
    """The in-DM alarm leg (Allen 2026-10-13): both reply-bridge spellings
    and a channel route name the adapter whose chat must hear the notice."""

    assert lark_alarm_destination("channel:lark:squire") == "squire"
    assert lark_alarm_destination("adapter:lark:squire") == "squire"
    assert (
        lark_alarm_destination("route:hyprial-introducer-feishu:customer-dm")
        == "hyprial-introducer-feishu"
    )


def test_lark_alarm_destination_rejects_shapes_without_a_chat_handle() -> None:
    """No adapter is guessed: the daemon holds the notice for retry instead."""

    assert lark_alarm_destination("user:bob.smith@example.com") is None
    assert lark_alarm_destination("agent:allen:mac:squire") is None
    assert lark_alarm_destination("squire") is None
    assert lark_alarm_destination("route:") is None
    assert lark_alarm_destination("route:only-adapter") is None
    assert lark_alarm_destination("route:a:b:c") is None


def test_audience_for_sender_recognizes_both_spellings_as_human() -> None:
    assert audience_for_sender("channel:lark:squire") == "human"
    assert audience_for_sender("adapter:lark:squire") == "human"
    assert audience_for_sender("agent:allen:mac:pi-worker") == "agent"


def test_contracts_wire_lifecycle_default_recognizes_both_spellings() -> None:
    legacy = _lifecycle(None, "channel:allen:mac:lark-main")
    current = _lifecycle(None, "adapter:allen:mac:lark-main")

    assert legacy == DeliveryLifecycle.DURABLE_SERVICE
    assert current == legacy
    assert _lifecycle(None, "agent:allen:mac:squire") == DeliveryLifecycle.ONLINE_ONLY
