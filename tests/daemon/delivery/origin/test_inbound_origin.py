"""A message's origin survives from the adapter to the reader.

The Lark adapter parses ``chat_type`` off the event and hands it to the daemon
inside ``providerMetadata``.  Only ``senderId`` was ever read from that block,
so the value arrived and was dropped before storage -- a reader could not tell
a group from a direct message, and Feishu uses the same ``oc_`` prefix for
both, so nothing else in the row distinguished them either.

Absence is load-bearing here.  "This provider reports no origin" and "a direct
message" are different answers and only one of them is safe to act on, so these
tests pin absence as carefully as presence.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication, _message_origin
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.runtime.cell_delivery import AdmittingCellDelivery

READER = {
    "provider": "pi",
    "name": "reader",
    "headless": True,
    "args": [],
    "ownership": "managed",
}


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _LoopbackTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        return self.service.receive(message).acknowledged


def _app(tmp_path: Path) -> DaemonApplication:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [READER],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    transport = _LoopbackTransport()
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", transport)
    transport.service = app._inbox
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()
    app._cell_delivery = AdmittingCellDelivery()
    if app.agents.get("driver") is None:
        app.handle("agent.create", {"name": "driver"})
    app.handle(
        "session.register",
        {
            "actor": "driver",
            "sessionRef": "session-driver",
            "cwd": str(workspace),
            "command": ["test"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )
    app.handle(
        "session.register",
        {
            "actor": "reader",
            "sessionRef": "session-reader",
            "cwd": str(workspace),
            "command": ["test"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )
    return app


def _round_trip(
    app: DaemonApplication, **metadata: object
) -> tuple[dict[str, object], dict[str, object]]:
    params: dict[str, object] = {
        "from": "driver",
        "sessionRef": "session-driver",
        "to": ["reader"],
        "summary": "hi",
        "message": "hi",
    }
    if metadata:
        params["providerMetadata"] = {"provider": "lark", **metadata}
    app.handle("message.send", params)
    pending = app.handle(
        "message.pending.list",
        {"from": "reader", "sessionRef": "session-reader"},
    )
    (row,) = pending["messages"]
    # The row carries summary/bodyBytes only; the text is read on demand.
    body = app.handle(
        "message.read_body",
        {
            "from": "reader",
            "sessionRef": "session-reader",
            "messageId": row["messageId"],
        },
    )
    app._inbox.close()
    app.agents.close()
    return row, body


# -- the reader's view ---------------------------------------------------------


def test_a_group_message_reaches_the_reader_as_a_group(tmp_path: Path) -> None:
    row, _body = _round_trip(_app(tmp_path), chatKind="group", chatId="oc_group")
    assert row["origin"] == {
        "chatKind": "group",
        "provider": "lark",
        "via": "session",
    }


def test_a_direct_message_reaches_the_reader_as_a_direct_message(
    tmp_path: Path,
) -> None:
    row, _body = _round_trip(_app(tmp_path), chatKind="dm", chatId="oc_dm")
    assert row["origin"] == {
        "chatKind": "dm",
        "provider": "lark",
        "via": "session",
    }


def test_a_session_without_adapter_metadata_still_reports_provenance(
    tmp_path: Path,
) -> None:
    """Absent, not empty and not "dm": an unknown origin is not a direct one."""

    row, _body = _round_trip(_app(tmp_path))
    assert row["origin"] == {"via": "session"}


def test_an_origin_alone_does_not_disturb_the_rest_of_the_row(
    tmp_path: Path,
) -> None:
    """The fields a reader already depends on keep their values."""

    app = _app(tmp_path)
    row, body = _round_trip(app, chatKind="group")
    assert body["message"] == "hi"
    assert row["summary"] == "hi"
    assert row["bodyBytes"] == len("hi".encode())
    # `from` is the canonicalized sender, not the bare name that was passed in.
    assert row["from"] == app.agents.get("driver").uri
    assert row["conversationId"]
    assert row["intent"] == "request"
    assert row["messageId"] == row["deliveryId"]


def test_a_row_written_before_this_change_is_still_readable(tmp_path: Path) -> None:
    """Old payloads carry no ``origin`` and must not be rejected."""

    app = _app(tmp_path)
    app._inbox.receive(
        InboxMessage(
            message_id="m-old",
            conversation_id="c-old",
            sender="agent:op:node-1:driver",
            recipient=app.agents.require("reader").uri,
            payload=b'{"message":"legacy","topic":null}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    try:
        pending = app.handle(
            "message.pending.list",
            {"from": "reader", "sessionRef": "session-reader"},
        )
        (row,) = pending["messages"]
        assert "message" not in row
        assert row["summary"] is None
        assert row["bodyBytes"] == len("legacy".encode())
        legacy_body = app.handle(
            "message.read_body",
            {
                "from": "reader",
                "sessionRef": "session-reader",
                "messageId": "m-old",
            },
        )
        assert legacy_body["message"] == "legacy"
        assert legacy_body["summary"] is None
        assert row["origin"] == {"via": "daemon"}
    finally:
        app._inbox.close()
        app.agents.close()


# -- the block itself ----------------------------------------------------------


def test_message_origin_ignores_a_metadata_block_without_a_chat_type() -> None:
    assert _message_origin({"senderId": "ou_x"}) is None
    assert _message_origin({"chatKind": ""}) is None
    assert _message_origin({"chatKind": 7}) is None
    assert _message_origin("not-a-dict") is None
    assert _message_origin(None) is None


def test_message_origin_omits_a_provider_it_was_not_given() -> None:
    """The block carries only what the sender actually said."""

    assert _message_origin({"chatKind": "group"}) == {"chatKind": "group"}
    assert _message_origin({"provider": "", "chatKind": "group"}) == {
        "chatKind": "group"
    }


# --------------------------------------------------------------------------
# platformCreatedAt: the platform's own clock, projected into the envelope.
#
# The adapter already reports it (``provider_metadata["createTime"]``, a
# millisecond epoch).  Until this projection carried it, a message that the
# platform generated seven hours earlier arrived looking exactly like a fresh
# one: no layer downstream could tell the difference.  Three states are pinned,
# and the third is separate from the second on purpose.
# --------------------------------------------------------------------------

PLATFORM_MS = "1793081760000"
PLATFORM_ISO = "2026-10-27T06:16:00.000Z"


def test_platform_created_at_is_projected_as_iso_utc() -> None:
    origin = _message_origin({"chatKind": "dm", "createTime": PLATFORM_MS})
    assert origin is not None
    assert origin["platformCreatedAt"] == PLATFORM_ISO, (
        "the platform time must be normalised to ISO-8601 in UTC, not passed "
        "through as a bare millisecond count"
    )


def test_missing_platform_created_at_is_omitted_not_zeroed() -> None:
    origin = _message_origin({"chatKind": "dm"})
    assert origin is not None
    assert "platformCreatedAt" not in origin, (
        "'the platform said nothing' must stay distinguishable from any epoch"
    )


def test_unparseable_platform_created_at_is_omitted_like_missing() -> None:
    origin = _message_origin({"chatKind": "dm", "createTime": "not-a-number"})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_zero_platform_created_at_is_omitted() -> None:
    origin = _message_origin({"chatKind": "dm", "createTime": "0"})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_a_superscript_unicode_digit_is_omitted_not_raised() -> None:
    """``str.isdigit`` accepts '²' but ``int`` cannot read it.

    The parser must reject the value itself instead of letting the
    ``int()`` call raise through the dispatch loop.
    """

    origin = _message_origin({"chatKind": "dm", "createTime": "²"})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_non_ascii_digits_are_rejected_not_coerced() -> None:
    """Only ASCII digits parse: '١٢٣' is read by ``int`` as 123 and must not be."""

    origin = _message_origin({"chatKind": "dm", "createTime": "١٢٣"})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_a_five_thousand_digit_create_time_is_omitted_not_raised() -> None:
    """A hostile length must omit the key, not hit the int↔str digit limit."""

    origin = _message_origin({"chatKind": "dm", "createTime": "9" * 5000})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_a_negative_create_time_is_omitted() -> None:
    origin = _message_origin({"chatKind": "dm", "createTime": "-1793081760000"})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_an_empty_create_time_is_omitted() -> None:
    origin = _message_origin({"chatKind": "dm", "createTime": ""})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_a_whitespace_only_create_time_is_omitted() -> None:
    origin = _message_origin({"chatKind": "dm", "createTime": "   "})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_an_out_of_range_epoch_is_omitted_not_raised() -> None:
    """A bounded but datetime-unrepresentable epoch stays in the third state."""

    origin = _message_origin({"chatKind": "dm", "createTime": "9999999999999999"})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_a_huge_integer_create_time_is_omitted_not_raised() -> None:
    origin = _message_origin({"chatKind": "dm", "createTime": 10**5000})
    assert origin is not None
    assert "platformCreatedAt" not in origin


def test_platform_created_at_reaches_the_agent_visible_surface() -> None:
    """The end-to-end claim: a reader that only sees the stored body gets it."""

    from hyprial.daemon.impl.api import visible_message_origin

    stored = _message_origin({"chatKind": "dm", "createTime": PLATFORM_MS})
    visible = visible_message_origin({"origin": stored}, "adapter:lark:squire")
    assert visible["platformCreatedAt"] == PLATFORM_ISO


def test_unparsable_platform_created_at_is_reported_per_message() -> None:
    """The third state must be observable somewhere, or it is only two states.

    The envelope deliberately omits the key for both "absent" and "unparsable" —
    a reader must never see a fabricated timestamp. The difference is reported
    through the projection's observer, once per message; the *log line* is
    throttled by the caller (see the counter test below), so "once" here means
    one observation, not one log line.
    """

    seen: list[object] = []
    origin = _message_origin(
        {"chatKind": "dm", "createTime": "not-a-number"},
        on_unparsable=seen.append,
    )
    assert origin is not None and "platformCreatedAt" not in origin
    assert seen == ["not-a-number"], seen


def test_absent_platform_created_at_is_not_reported() -> None:
    """Absence is not an anomaly: nothing may be reported for it."""

    seen: list[object] = []
    _message_origin({"chatKind": "dm"}, on_unparsable=seen.append)
    assert seen == []


def test_parseable_platform_created_at_is_not_reported() -> None:
    seen: list[object] = []
    _message_origin({"chatKind": "dm", "createTime": PLATFORM_MS}, on_unparsable=seen.append)
    assert seen == []


def test_unparsable_counter_is_monotonic_and_the_log_is_throttled() -> None:
    """Quantify the third state, and keep the log from drowning in it."""

    from hyprial.daemon.impl.application.messaging import visibility as vis

    vis._reset_unparsable_report_state()
    emitted: list[dict[str, object]] = []

    def log(level: str, component: str, event: str, **fields: object) -> None:
        emitted.append({"level": level, "event": event, **fields})

    window = vis.UNPARSABLE_REPORT_WINDOW_MS
    # three anomalies inside one window: counted three times, logged once
    assert vis.report_unparsable_platform_created_at("x1", log=log, now_ms=1_000) is True
    assert vis.report_unparsable_platform_created_at("x2", log=log, now_ms=1_000 + window // 2) is False
    assert vis.report_unparsable_platform_created_at("x3", log=log, now_ms=1_000 + window - 1) is False
    assert vis.platform_created_at_unparsable_total_per_process() == 3
    assert len(emitted) == 1, emitted
    assert emitted[0]["total"] == 1

    # the next window emits again and reports the running total
    assert vis.report_unparsable_platform_created_at("x4", log=log, now_ms=1_000 + window) is True
    assert vis.platform_created_at_unparsable_total_per_process() == 4
    assert [row["total"] for row in emitted] == [1, 4]

    vis._reset_unparsable_report_state()
    assert vis.platform_created_at_unparsable_total_per_process() == 0


def test_unparsable_counter_is_per_process_not_per_install() -> None:
    """A restart legitimately restarts the count; the name and the log say so."""

    from hyprial.daemon.impl.application.messaging import visibility as vis

    vis._reset_unparsable_report_state()
    emitted: list[dict[str, object]] = []

    def log(level: str, component: str, event: str, **fields: object) -> None:
        emitted.append({"level": level, "event": event, **fields})

    vis.report_unparsable_platform_created_at("a", log=log, now_ms=1)
    vis.report_unparsable_platform_created_at("b", log=log, now_ms=2)
    assert vis.platform_created_at_unparsable_total_per_process() == 2

    # Simulated restart: the count returns to zero rather than continuing, which
    # is exactly why the function name says "per process".
    vis._reset_unparsable_report_state()
    assert vis.platform_created_at_unparsable_total_per_process() == 0

    # The log line carries the epoch the count started from, so two totals from
    # different processes are never compared as one span.
    vis.report_unparsable_platform_created_at("c", log=log, now_ms=3)
    assert emitted[-1]["total"] == 1
    assert emitted[-1]["since_ms"] == vis._PROCESS_STARTED_MS


def test_the_production_unparsable_report_never_raises_on_hostile_values() -> None:
    """The observer wired in the dispatch path must survive hostile values.

    ``send.py`` wires ``on_unparsable`` straight into
    ``report_unparsable_platform_created_at``, whose log preview truncated
    with ``str()`` -- and Python 3.12 raises ValueError once an int→str
    conversion exceeds the digit limit (default 4300), so a huge
    ``createTime`` raised through the very path that exists to contain
    anomalies.  Every unparsable value must omit the key, be counted, and
    log a bounded preview -- never raise.
    """

    from hyprial.daemon.impl.application.messaging import visibility as vis

    emitted: list[dict[str, object]] = []

    def log(level: str, component: str, event: str, **fields: object) -> None:
        emitted.append({"level": level, "event": event, **fields})

    hostiles = [
        10**5000,
        "9" * 5000,
        "²",
        "١٢٣",
        "not-a-number",
        "-1793081760000",
        "",
        "   ",
        "0",
        "9999999999999999",
        True,
        1.5,
        ["nope"],
    ]
    for raw in hostiles:
        vis._reset_unparsable_report_state()
        seen: list[object] = []

        def on_unparsable(value: object) -> None:
            seen.append(value)
            vis.report_unparsable_platform_created_at(value, log=log, now_ms=1)

        origin = _message_origin(
            {"chatKind": "dm", "createTime": raw},
            on_unparsable=on_unparsable,
        )
        assert origin is not None
        assert "platformCreatedAt" not in origin, raw
        assert seen == [raw], raw
        (record,) = emitted
        assert record["event"] == "origin.platform_created_at.unparsable"
        preview = record["raw"]
        assert isinstance(preview, str)
        assert 0 < len(preview) <= 32, preview
        emitted.clear()

    vis._reset_unparsable_report_state()
    vis.report_unparsable_platform_created_at(10**5000, log=log, now_ms=1)
    (record,) = emitted
    assert record["raw"] == f"<int {(10**5000).bit_length()} bits>", record
    vis._reset_unparsable_report_state()
