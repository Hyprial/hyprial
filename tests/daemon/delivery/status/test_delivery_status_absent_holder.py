"""Delivery status never turns an absent recipient into a certain expiry."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    DeliveryStatus,
    HolderReplyCount,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    TerminalState,
    encode_status_frame,
    query_delivery_status,
)
from hyprial.daemon.impl.transport import KeySpace, TransportSample
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SENDER = "agent:alice:node-alice:sender"
RECIPIENT = "agent:bob:node-bob:worker"
TTL_MS = DEFAULT_HOLD_TTL_MS
_OPEN_SERVICES: list[InboxService] = []
_OPEN_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_resources() -> None:
    yield
    while _OPEN_APPLICATIONS:
        _OPEN_APPLICATIONS.pop()._agent_session_domains.close()
    while _OPEN_SERVICES:
        _OPEN_SERVICES.pop().close()


def _service(path: Path) -> InboxService:
    service = InboxService(
        path,
        MemoryDeliveryTransport(),
        node_id="node-alice",
    )
    _OPEN_SERVICES.append(service)
    return service


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _ScriptedStatusTransport:
    """The production TransportSession.get seam, with one scripted pull."""

    def __init__(
        self,
        *samples: TransportSample,
        errors: tuple[str, ...] = (),
    ) -> None:
        self.samples = list(samples)
        self.errors = errors
        self.queries: list[tuple[str, float, bool]] = []

    def get(
        self,
        key_expr: str,
        *,
        timeout: float = 3.0,
        errors: list[str] | None = None,
        all_replies: bool = False,
    ) -> list[TransportSample]:
        self.queries.append((key_expr, timeout, all_replies))
        if errors is not None:
            errors.extend(self.errors)
        return list(self.samples)

    def close(self) -> None:
        pass


def _message(message_id: str, recipient: str = RECIPIENT) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender=SENDER,
        recipient=recipient,
        payload=b"opaque-frame",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


def _status(
    message_id: str,
    *,
    state: TerminalState,
    holder: str,
    recipient: str = RECIPIENT,
    reason: str | None = None,
) -> DeliveryStatus:
    return DeliveryStatus(
        message_id=message_id,
        sender=SENDER,
        recipient=recipient,
        state=state,
        holder=holder,
        reason=reason
        or (
            "RECIPIENT_COMMIT"
            if state is TerminalState.FETCHED
            else "DELIVERY_RETRY_EXHAUSTED"
        ),
        recorded_at_ms=2,
        conversation_id="conversation-1",
    )


def _sample(holder: str, *records: DeliveryStatus) -> TransportSample:
    return TransportSample(
        key=KeySpace().message_status_root(SENDER),
        payload=encode_status_frame(records, holder=holder),
    )


def _application(
    tmp_path: Path, service: InboxService, transport: _ScriptedStatusTransport
) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node-alice",
    **_deep_module_daemon_dependencies())
    app._inbox = service
    app._transport = transport  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    _OPEN_APPLICATIONS.append(app)
    return app


def _settled_log_text(path: Path) -> str:
    app = next(app for app in _OPEN_APPLICATIONS if app._logger.path == path)
    status = app._logger.writer_status()
    assert status is not None
    assert app._logger.wait_for_writes(
        target=status.accepted, rejected_at_capture=status.rejected, timeout=2.0
    ), (
        app._logger.writer_status()
    )
    return path.read_text()


def _expire(service: InboxService, message_id: str, recipient: str = RECIPIENT) -> None:
    service.submit(_message(message_id, recipient), now_ms=0)
    service.retry_due(now_ms=TTL_MS + 1)
    [record] = service.delivery_status_records(SENDER, message_id=message_id)
    assert record.state is TerminalState.EXPIRED


def _query(
    tmp_path: Path,
    transport: _ScriptedStatusTransport,
    *,
    message_id: str = "msg-1",
    recipient: str = RECIPIENT,
) -> dict[str, object]:
    service = _service(tmp_path / "inbox.sqlite3")
    _expire(service, message_id, recipient)
    app = _application(tmp_path, service, transport)
    return app.handle(
        "message.status", {"from": SENDER, "messageId": message_id}
    )


def test_recipient_reply_with_commit_still_reports_fetched(tmp_path: Path) -> None:
    fetched = _status("msg-1", state=TerminalState.FETCHED, holder="node-bob")
    result = _query(
        tmp_path, _ScriptedStatusTransport(_sample("node-bob", fetched))
    )

    assert result["state"] == "fetched"
    assert result["records"][0]["reason"] == "RECIPIENT_COMMIT"
    assert "observedState" not in result["records"][0]


def test_recipient_timeout_preserves_expired_and_names_missing_holder(
    tmp_path: Path,
) -> None:
    result = _query(tmp_path, _ScriptedStatusTransport(errors=("Timeout",)))

    record = result["records"][0]
    assert result["state"] == "expired"
    assert record["state"] == "expired"
    assert record["reason"] == "TTL_EXPIRED"
    assert "observedState" not in record
    assert "observedReason" not in record
    assert result["diagnostics"]["missingRecipientHolders"] == ["node-bob"]

    log_text = _settled_log_text(tmp_path / "state" / "logs" / "daemon.jsonl")
    assert "opaque-frame" not in log_text
    events = [json.loads(line) for line in log_text.splitlines()]
    [reply_error] = [
        event for event in events if event["event"] == "message.status.reply_error"
    ]
    assert reply_error["holder"] == "node-bob"
    assert reply_error["key"].endswith("/msg-1")
    assert reply_error["messageId"] == "msg-1"
def test_recipient_empty_reply_preserves_certain_expired(tmp_path: Path) -> None:
    result = _query(tmp_path, _ScriptedStatusTransport(_sample("node-bob")))

    record = result["records"][0]
    assert result["state"] == "expired"
    assert record["state"] == "expired"
    assert record["reason"] == "TTL_EXPIRED"
    assert "observedState" not in record
    assert result["diagnostics"]["meshResponders"] == ["node-bob"]
    assert result["diagnostics"]["meshHolders"] == []


def test_recipient_undecodable_reply_preserves_expired_reason(
    tmp_path: Path,
) -> None:
    malformed_record = _status(
        "msg-1", state=TerminalState.FETCHED, holder="node-bob"
    ).to_json()
    malformed_record["state"] = "future-state"
    payload = json.dumps(
        {
            "schema": "hyprial-delivery-status/v1",
            "holder": "node-bob",
            "records": [malformed_record],
        }
    ).encode()
    sample = TransportSample(
        key=KeySpace().message_status(SENDER, "msg-1"), payload=payload
    )
    result = _query(tmp_path, _ScriptedStatusTransport(sample))

    record = result["records"][0]
    assert result["state"] == "expired"
    assert record["state"] == "expired"
    assert record["reason"] == "TTL_EXPIRED"
    assert "observedState" not in record
    assert "observedReason" not in record
    assert result["diagnostics"]["undecodableHolders"] == ["node-bob"]

    events = [
        json.loads(line)
        for line in _settled_log_text(
            tmp_path / "state" / "logs" / "daemon.jsonl"
        ).splitlines()
    ]
    [undecodable] = [
        event
        for event in events
        if event["event"] == "message.status.undecodable_reply"
    ]
    assert undecodable["holder"] == "node-bob"
    assert undecodable["messageId"] == "msg-1"


def test_status_is_decided_per_recipient_not_per_response(tmp_path: Path) -> None:
    service = _service(tmp_path / "inbox.sqlite3")
    _expire(service, "msg-bob", "agent:bob:node-bob:worker")
    _expire(service, "msg-carol", "agent:carol:node-carol:worker")
    app = _application(
        tmp_path, service, _ScriptedStatusTransport(_sample("node-bob"))
    )

    result = app.handle("message.status", {"from": SENDER})
    by_id = {record["messageId"]: record for record in result["records"]}

    assert by_id["msg-bob"]["state"] == "expired"
    assert by_id["msg-carol"]["state"] == "expired"
    assert by_id["msg-carol"]["reason"] == "TTL_EXPIRED"


def test_other_holder_reply_does_not_stand_in_for_recipient(tmp_path: Path) -> None:
    result = _query(tmp_path, _ScriptedStatusTransport(_sample("node-other")))

    assert result["state"] == "expired"
    assert result["records"][0]["reason"] == "TTL_EXPIRED"


def test_holder_casing_mismatch_still_counts_as_answered(tmp_path: Path) -> None:
    # The recipient URI machine segment and the answering holder name the
    # same node with different casing; that must not silently read as
    # "holder absent" and demote a certain expiry to unconfirmed.
    result = _query(
        tmp_path,
        _ScriptedStatusTransport(_sample("node-bob")),
        recipient="agent:bob:NODE-BOB:worker",
    )

    record = result["records"][0]
    assert result["state"] == "expired"
    assert record["state"] == "expired"
    assert "observedState" not in record
    assert result["diagnostics"]["missingRecipientHolders"] == []


def test_holder_casing_normalization_does_not_leak_into_output(
    tmp_path: Path,
) -> None:
    # Normalization is for comparison only: a missing holder is reported
    # with the casing the recipient URI used, and a responding holder with
    # the casing it announced.
    missing = _query(
        tmp_path / "missing",
        _ScriptedStatusTransport(_sample("node-other")),
        recipient="agent:bob:NODE-BOB:worker",
    )
    assert missing["diagnostics"]["missingRecipientHolders"] == ["NODE-BOB"]

    answered = _query(
        tmp_path / "answered",
        _ScriptedStatusTransport(_sample("Node-Bob")),
        recipient="agent:bob:node-bob:worker",
    )
    assert answered["state"] == "expired"
    assert answered["diagnostics"]["meshResponders"] == ["Node-Bob"]


def test_mesh_holders_names_record_holders_and_responders_are_separate(
    tmp_path: Path,
) -> None:
    # E2E-001 phase6c shape: two holders answer with records, a third node
    # answers with none.  meshHolders is the published "which holders the
    # merged set came from" (01-two-machine-roundtrip.md); a record-less
    # responder must not widen it, and must still be visible on its own.
    fetched = _status("msg-1", state=TerminalState.FETCHED, holder="node-bob")
    relayed = _status("msg-1", state=TerminalState.FETCHED, holder="node-carol")
    result = _query(
        tmp_path,
        _ScriptedStatusTransport(
            _sample("node-bob", fetched),
            _sample("node-carol", relayed),
            _sample("node-dave"),
        ),
    )

    diagnostics = result["diagnostics"]
    assert diagnostics["meshHolders"] == ["node-bob", "node-carol"]
    assert diagnostics["meshResponders"] == ["node-bob", "node-carol", "node-dave"]
    assert "meshRecordHolders" not in diagnostics


def test_local_recipient_does_not_require_a_mesh_reply(tmp_path: Path) -> None:
    local_recipient = "agent:alice:node-alice:worker"
    result = _query(
        tmp_path,
        _ScriptedStatusTransport(),
        recipient=local_recipient,
    )

    assert result["state"] == "expired"
    assert result["records"][0]["state"] == "expired"


def test_local_recipient_survives_an_undecodable_frame_naming_this_node(
    tmp_path: Path,
) -> None:
    sample = TransportSample(
        key=KeySpace().message_status(SENDER, "msg-1"),
        payload=json.dumps(
            {
                "schema": "hyprial-delivery-status/v1",
                "holder": "node-alice",
                "records": [{"not": "a record"}],
            }
        ).encode(),
    )
    result = _query(
        tmp_path,
        _ScriptedStatusTransport(sample),
        recipient="agent:alice:node-alice:worker",
    )

    # The local store already confirms this recipient; the malformed mesh
    # frame must not demote a locally-confirmed expiry to unconfirmed.
    assert result["state"] == "expired"
    assert result["records"][0]["state"] == "expired"


def test_message_status_help_names_only_terminal_outcomes() -> None:
    result = CliRunner().invoke(_cli_root_app, ["message", "status", "--help"])

    assert result.exit_code == 0
    assert "unconfirmed" not in result.output
    assert "fetched" in result.output
    assert "expired" in result.output


def test_unresolvable_recipient_preserves_expired_reason(tmp_path: Path) -> None:
    service = _service(tmp_path / "inbox.sqlite3")
    service.delivery_status.record(
        message_id="msg-1",
        sender=SENDER,
        recipient="legacy-bare-recipient",
        state=TerminalState.EXPIRED,
        holder="node-alice",
        reason="DELIVERY_EXPIRED",
        now_ms=1,
    )
    app = _application(tmp_path, service, _ScriptedStatusTransport())

    result = app.handle("message.status", {"from": SENDER, "messageId": "msg-1"})

    assert result["state"] == "expired"
    assert result["records"][0]["reason"] == "DELIVERY_EXPIRED"


def test_duplicate_holder_answers_surface_in_diagnostics_and_log(
    tmp_path: Path,
) -> None:
    """Two replies under one holder name are the duplicate-identity signature.

    ``meshHolders`` is name-keyed and collapses both answers to one entry;
    ``duplicateHolderRecords`` keeps the pull's own count so a duplicated
    node identity is visible from the status-query vantage too.
    """

    fetched = _status("msg-1", state=TerminalState.FETCHED, holder="node-bob")
    expired = _status("msg-1", state=TerminalState.EXPIRED, holder="node-bob")
    result = _query(
        tmp_path / "dup",
        _ScriptedStatusTransport(_sample("node-bob", fetched), _sample("node-bob", expired)),
    )

    assert result["diagnostics"]["meshHolders"] == ["node-bob"]
    assert result["diagnostics"]["duplicateHolderRecords"] == [
        {"holder": "node-bob", "replies": 2, "records": 2}
    ]
    # The merge still resolves the disagreement -- that is not the point; the
    # point is that the diagnostics now say the name answered twice.
    assert result["state"] == "fetched"

    log_text = _settled_log_text(tmp_path / "dup" / "state" / "logs" / "daemon.jsonl")
    events = [json.loads(line) for line in log_text.splitlines()]
    [duplicate] = [
        event for event in events if event["event"] == "message.status.duplicate_holder"
    ]
    assert duplicate["holders"] == [
        {"holder": "node-bob", "replies": 2, "records": 2}
    ]
    assert duplicate["messageId"] == "msg-1"


def test_single_reply_per_holder_reports_no_duplicates(tmp_path: Path) -> None:
    fetched = _status("msg-1", state=TerminalState.FETCHED, holder="node-bob")
    result = _query(
        tmp_path / "single",
        _ScriptedStatusTransport(_sample("node-bob", fetched)),
    )

    assert result["diagnostics"]["duplicateHolderRecords"] == []
    log_text = _settled_log_text(tmp_path / "single" / "state" / "logs" / "daemon.jsonl")
    assert "message.status.duplicate_holder" not in log_text


def test_pull_counts_legacy_frames_and_undecodable_envelopes_per_name() -> None:
    """Reply counting must survive frames without envelope metadata.

    Old frames carry no holder name; attribution falls back to each record's
    holder, one reply per sample that mentions the name.  An undecodable
    reply whose envelope survived still proves the name answered.
    """

    expired = _status("msg-1", state=TerminalState.EXPIRED, holder="node-old")
    legacy_first = TransportSample(
        key=KeySpace().message_status_root(SENDER),
        payload=encode_status_frame((expired,)),
    )
    fetched = _status("msg-1", state=TerminalState.FETCHED, holder="node-old")
    legacy_second = TransportSample(
        key=KeySpace().message_status_root(SENDER),
        payload=encode_status_frame((fetched,)),
    )
    undecodable_named = TransportSample(
        key=KeySpace().message_status_root(SENDER),
        payload=json.dumps(
            {
                "schema": "hyprial-delivery-status/v1",
                "holder": "node-old",
                "records": [{"not": "a record"}],
            }
        ).encode(),
    )

    report = query_delivery_status(
        _ScriptedStatusTransport(legacy_first, legacy_second, undecodable_named),
        SENDER,
    )

    assert report.responded_holders == ("node-old",)
    assert report.duplicate_holders == (
        HolderReplyCount(holder="node-old", replies=3, records=2),
    )
    # The undecodable reply still names its holder in the report.
    assert report.undecodable_holders == ("node-old",)
