"""E2E-004 scenarios / probes; explicit callbacks keep entrypoint seams live."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
    HarnessDelivery,
    HarnessReceipt,
    LarkAdapter,
    LarkSdkGateway,
    LarkStateStore,
    RequestCorrelation,
    normalize_sdk_event,
)
from hyprial.kernel.impl.log import Logger
from feishu.evidence import ContractFailure, native_id_label, text_digest


@dataclass(frozen=True)
class ScenariosProbesPorts:
    """Only this module's observed collaborators and configuration."""

    ACTOR: ActorTarget
    APP_ID: str
    APP_NAME: str
    ChildRoutes: Callable[..., Any]
    dump_dead_letters: Callable[..., Any]
    get_message: Callable[..., Any]
    issue_delivery: Callable[..., Any]
    wait_for_evidence: Callable[..., Any]


def nontext_roundtrip(
    *,
    label: str,
    message_id: str,
    expected_message_type: str,
    expected_text: str | None,
    minimum_text_length: int,
    chat_id: str,
    chat_type: str,
    nonce: str,
    command_dir: Path,
    evidence_path: Path,
    process: subprocess.Popen[str] | None,
    _ports: ScenariosProbesPorts,
) -> dict[str, Any]:
    """Drive one non-text message all the way to an agent reply.

    Asserts the four things the 2026-08-13 P0 would have failed: the event is
    normalized under its real type, the agent receives an intelligible body
    (never empty, never a "cannot read" placeholder), the adapter answers
    natively, and nothing lands in the adapter-private dead-letter store.
    """

    before = {
        letter.get("message_id")
        for letter in _ports.dump_dead_letters(command_dir, evidence_path, process)
    }
    sdk = _ports.wait_for_evidence(
        evidence_path,
        "sdk.event",
        lambda row: row.get("message_id") == message_id,
        process=process,
    )
    if (
        sdk.get("chat_label") != native_id_label(chat_id)
        or sdk.get("chat_type") != chat_type
        or sdk.get("message_type") != expected_message_type
    ):
        raise ContractFailure(
            f"{label} message was not normalized as {expected_message_type}: "
            f"observed type={sdk.get('message_type')!r} "
            f"chat_type={sdk.get('chat_type')!r}"
        )
    # An empty or "unreadable" body is the silent-drop shape: it survives every
    # "did not raise" style check while the agent learns nothing.
    if int(sdk.get("text_length") or 0) < minimum_text_length:
        raise ContractFailure(
            f"{label} normalized body is too short to be intelligible: "
            f"length={sdk.get('text_length')} (expected >= {minimum_text_length})"
        )
    for unreadable in (
        f"[Unreadable Lark {expected_message_type} message]",
        "[Unsupported Lark message type]",
    ):
        if sdk.get("text_digest") == text_digest(unreadable):
            raise ContractFailure(
                f"{label} normalized to the {unreadable!r} placeholder"
            )
    if expected_text is not None and sdk.get("text_digest") != text_digest(
        expected_text
    ):
        raise ContractFailure(
            f"{label} flattened body did not match the contract "
            f"(expected digest {text_digest(expected_text)}, "
            f"observed {sdk.get('text_digest')} length {sdk.get('text_length')})"
        )
    inbound = _ports.wait_for_evidence(
        evidence_path,
        "adapter.inbound",
        lambda row: row.get("message_id") == message_id,
        process=process,
    )
    if inbound.get("status") != "forwarded" or inbound.get("new_dead_letter"):
        raise ContractFailure(
            f"{label} was not forwarded to an agent: status="
            f"{inbound.get('status')!r} dead_lettered={inbound.get('new_dead_letter')!r}"
        )
    harness = _ports.wait_for_evidence(
        evidence_path,
        "harness.request.accepted",
        lambda row: row.get("native_message_id") == message_id,
        process=process,
    )
    if harness.get("target_actor_id") != _ports.ACTOR.actor_id:
        raise ContractFailure(f"{label} did not reach the expected actor")
    # The agent-visible text is what Harness received, not merely what the
    # normalizer produced; assert on that copy too.
    if expected_text is not None and harness.get("text_digest") != text_digest(
        expected_text
    ):
        raise ContractFailure(
            f"{label} agent-visible text differs from the normalized body"
        )
    reply_text = f"CONTRACT_LARK_{label.upper()}_REPLY_{nonce}"
    command_id, delivery_id = _ports.issue_delivery(
        command_dir,
        str(harness["harness_message_id"]),
        reply_text,
        f"{label}_{nonce}",
    )
    delivery = _ports.wait_for_evidence(
        evidence_path,
        "adapter.delivery",
        lambda row: row.get("command_id") == command_id,
        process=process,
    )
    if delivery.get("status") != "accepted" or not isinstance(
        delivery.get("native_message_id"), str
    ):
        raise ContractFailure(f"{label} reply was not accepted: {delivery}")
    native_reply_id = str(delivery["native_message_id"])
    reply_message = _ports.get_message(native_reply_id)
    content = reply_message.get("content")
    if (
        reply_message.get("reply_to") != message_id
        or not isinstance(content, str)
        or f"Reply-To: {message_id}" not in content
        or reply_text not in content
    ):
        raise ContractFailure(f"{label} reply was not a native, headed reply")
    _ports.wait_for_evidence(
        evidence_path,
        "harness.delivery.accepted",
        lambda row: row.get("delivery_id") == delivery_id,
        process=process,
    )
    after = _ports.dump_dead_letters(command_dir, evidence_path, process)
    leaked = [
        letter
        for letter in after
        if letter.get("message_id") == message_id
        and letter.get("message_id") not in before
    ]
    if leaked:
        raise ContractFailure(
            f"{label} was silently dead-lettered: reason={leaked[0].get('reason')!r}"
        )
    return {
        "native": message_id,
        "harness": harness["harness_message_id"],
        "reply_native": native_reply_id,
        "message_type": sdk.get("message_type"),
        "text_length": sdk.get("text_length"),
        "dead_letters": len(after),
        "reply_text": reply_text,
    }


def assert_provider_failure(
    root: Path, chat_id: str, *, _ports: ScenariosProbesPorts
) -> dict[str, Any]:
    class FailingLark:
        def __init__(self) -> None:
            self.reactions = {"om_forced_failure"}
            self.clear_calls = 0

        def add_reaction(self, _message_id: str, _emoji_type: str) -> None:
            raise AssertionError("negative delivery must not add a reaction")

        def clear_reaction(self, message_id: str, _emoji_type: str) -> None:
            self.clear_calls += 1
            self.reactions.discard(message_id)

        def reply(self, _message_id: str, _text: str) -> str:
            raise RuntimeError("forced provider failure")

        def get_message(self, _message_id: str):  # type: ignore[no-untyped-def]
            raise AssertionError("negative delivery must not fetch a quote")

    class Harness:
        def __init__(self) -> None:
            self.accepted: list[tuple[str, str]] = []
            self.rejected: list[tuple[str, str, bool]] = []

        def send_request(self, _request):  # type: ignore[no-untyped-def]
            raise AssertionError("negative delivery must not send inbound")

        def accept_delivery(self, delivery_id: str, message_id: str) -> None:
            self.accepted.append((delivery_id, message_id))

        def reject_delivery(
            self, delivery_id: str, error: str, *, deterministic: bool
        ) -> None:
            self.rejected.append((delivery_id, error, deterministic))

    state = LarkStateStore(root / "negative-state.json")
    state.record_inbound(
        RequestCorrelation(
            harness_message_id="harness_forced_failure",
            message_id="om_forced_failure",
            chat_id=chat_id,
            conversation_id=f"lark:{chat_id}:{chat_id}",
        ),
        event_id="event_forced_failure",
    )
    lark_port = FailingLark()
    harness = Harness()
    adapter = LarkAdapter(
        state=state,
        lark=lark_port,
        harness=harness,
        routes=_ports.ChildRoutes(chat_id),
        channel_actor_id=f"adapter:e2e:lark:{_ports.APP_NAME}",
    )
    outcome = adapter.handle_delivery(
        HarnessDelivery(
            delivery_id="delivery_forced_failure",
            message_id="message_forced_failure",
            reply_to="harness_forced_failure",
            from_actor=_ports.ACTOR,
            text="forced failure payload",
        )
    )
    return {
        "status": outcome.status,
        "accepted": harness.accepted,
        "rejected": harness.rejected,
        "reaction_retained": "om_forced_failure" in lark_port.reactions,
        "clear_calls": lark_port.clear_calls,
    }


def sdk_event(
    *,
    message_type: str | None,
    content: str | None,
    message_id: str,
    chat_id: str,
    event_id: str,
) -> SimpleNamespace:
    """The official SDK's P2ImMessageReceiveV1 shape, as the stream yields it."""

    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                chat_id=chat_id,
                chat_type="p2p",
                message_type=message_type,
                content=content,
                mentions=(),
                root_id=None,
                thread_id=None,
                parent_id=None,
                create_time="1786682000000",
            ),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_contract_probe"),
                sender_type="user",
            ),
        ),
    )


class _RecordingHarness:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def send_request(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return HarnessReceipt(message_id=f"harness_probe_{len(self.requests)}")

    def accept_delivery(self, delivery_id: str, message_id: str) -> None:
        raise AssertionError("inbound-only probe must not accept a delivery")

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        raise AssertionError("inbound-only probe must not reject a delivery")


class _SilentLark:
    """A Lark port that records reactions and never talks to the platform."""

    def __init__(self) -> None:
        self.replies: list[str] = []

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        return None

    def clear_reaction(self, message_id: str, emoji_type: str) -> None:
        return None

    def reply(self, message_id: str, text: str, **_: Any) -> str:
        self.replies.append(text)
        return f"om_probe_reply_{len(self.replies)}"

    def get_message(self, message_id: str):  # type: ignore[no-untyped-def]
        return None


def _probe_adapter(
    state_path: Path, chat_id: str, *, _ports: ScenariosProbesPorts
) -> tuple[Any, Any, Any]:
    state = LarkStateStore(state_path)
    harness = _RecordingHarness()
    lark_port = _SilentLark()
    adapter = LarkAdapter(
        state=state,
        lark=lark_port,
        harness=harness,
        routes=_ports.ChildRoutes(chat_id),
        channel_actor_id=f"adapter:e2e:lark:{_ports.APP_NAME}",
        logger=Logger.adapter(state_path.parent, name=_ports.APP_NAME),
    )
    return adapter, harness, lark_port


def assert_merge_forward_not_dropped(
    root: Path, chat_id: str, *, _ports: ScenariosProbesPorts
) -> dict[str, Any]:
    """A forwarded-conversation message must reach the agent as a stand-in.

    Half of the 2026-08-13 P0 was a `merge_forward` message that never arrived.
    It cannot be produced live: the platform refuses `merge_forward` for a user
    token (99991668), lark-cli restricts the command to `bot`, and the CLI's own
    bot is not a member of the contract chats (99992402).  So this asserts the
    same normalize-and-forward path the live types use, through the real
    `normalize_sdk_event` entry point, one seam below the websocket.
    """

    adapter, harness, _ = _probe_adapter(
        root / "merge-forward-state.json", chat_id, _ports=_ports
    )
    message = normalize_sdk_event(
        sdk_event(
            message_type="merge_forward",
            content=json.dumps(
                {"title": "Chat history", "message_id_list": ["om_probe_forwarded"]}
            ),
            message_id="om_probe_merge_forward",
            chat_id=chat_id,
            event_id="event_probe_merge_forward",
        )
    )
    outcome = adapter.handle_inbound(message)
    return {
        "status": outcome.status,
        "message_type": message.message_type,
        "content_status": message.content_status,
        "text": message.text,
        "forwarded_text": harness.requests[0].text if harness.requests else None,
        "harness_requests": len(harness.requests),
        "dead_letters": len(adapter.dead_letters()),
    }


def assert_unsupported_type_is_loud(
    root: Path, chat_id: str, *, _ports: ScenariosProbesPorts
) -> dict[str, Any]:
    """An unhandled message must fail loudly, never disappear.

    Three ways a message can fail to be understood; each must end in a
    recoverable dead-letter record and never in silence.
    """

    results: dict[str, Any] = {}

    adapter, harness, lark_port = _probe_adapter(
        root / "unknown-type-state.json", chat_id, _ports=_ports
    )
    unknown = normalize_sdk_event(
        sdk_event(
            message_type="quantum_teleport",
            content=json.dumps({"payload": "CONTRACT_UNSUPPORTED_BODY"}),
            message_id="om_probe_unknown_type",
            chat_id=chat_id,
            event_id="event_probe_unknown_type",
        )
    )
    outcome = adapter.handle_inbound(unknown)
    letters = {letter.message_id: letter for letter in adapter.dead_letters()}
    results["unknown_type"] = {
        "status": outcome.status,
        "new_dead_letter": outcome.new_dead_letter,
        "harness_requests": len(harness.requests),
        "recorded": "om_probe_unknown_type" in letters,
        "reason": getattr(letters.get("om_probe_unknown_type"), "reason", None),
        "normalized_type": unknown.message_type,
        "reply_count": len(lark_port.replies),
        "reply_text": lark_port.replies[0] if lark_port.replies else None,
    }

    adapter, harness, lark_port = _probe_adapter(
        root / "invalid-content-state.json", chat_id, _ports=_ports
    )
    invalid = normalize_sdk_event(
        sdk_event(
            message_type="post",
            content="{not valid json",
            message_id="om_probe_invalid_content",
            chat_id=chat_id,
            event_id="event_probe_invalid_content",
        )
    )
    outcome = adapter.handle_inbound(invalid)
    letters = {letter.message_id: letter for letter in adapter.dead_letters()}
    results["invalid_content"] = {
        "status": outcome.status,
        "new_dead_letter": outcome.new_dead_letter,
        "harness_requests": len(harness.requests),
        "recorded": "om_probe_invalid_content" in letters,
        "reason": getattr(letters.get("om_probe_invalid_content"), "reason", None),
        "reply_count": len(lark_port.replies),
        "reply_text": lark_port.replies[0] if lark_port.replies else None,
    }

    # The P0's actual shape: normalization itself raises.  The raw event must
    # still be preserved rather than dying inside the SDK callback thread.
    adapter, harness, lark_port = _probe_adapter(
        root / "normalize-error-state.json", chat_id, _ports=_ports
    )
    outcome = adapter.handle_sdk_event(
        sdk_event(
            message_type=None,
            content=json.dumps({"text": "CONTRACT_UNPARSEABLE_BODY"}),
            message_id="om_probe_normalize_error",
            chat_id=chat_id,
            event_id="event_probe_normalize_error",
        )
    )
    letters = {letter.message_id: letter for letter in adapter.dead_letters()}
    results["normalize_error"] = {
        "status": outcome.status,
        "harness_requests": len(harness.requests),
        "recorded": "om_probe_normalize_error" in letters,
        "reason": getattr(letters.get("om_probe_normalize_error"), "reason", None),
        "reply_count": len(lark_port.replies),
        "reply_text": lark_port.replies[0] if lark_port.replies else None,
    }
    return results


def assert_live_unsupported_alarm(
    root: Path,
    *,
    app_secret: str,
    chat_id: str,
    original_message_id: str,
    _ports: ScenariosProbesPorts,
) -> dict[str, Any]:
    """Cross the real App reply seam for an adapter-rejected event shape.

    Lark rejects unknown ``msg_type`` values before they can enter a chat, so
    the unsupported event shape is injected at the adapter boundary while its
    ``message_id`` names a real user message in the dedicated test App DM.  The
    resulting failure response must still cross the real App API and appear as
    a native reply to that original message.
    """

    class LiveAlarmLark:
        def __init__(self) -> None:
            self.gateway = LarkSdkGateway.from_credentials(_ports.APP_ID, app_secret)
            self.reply_ids: list[str] = []

        def reply(self, message_id: str, text: str, *, idempotency_key: str) -> str:
            reply_id = self.gateway.reply(
                message_id, text, idempotency_key=idempotency_key
            )
            self.reply_ids.append(reply_id)
            return reply_id

    lark_port = LiveAlarmLark()
    harness = _RecordingHarness()
    adapter = LarkAdapter(
        state=LarkStateStore(root / "live-unsupported-state.sqlite3"),
        lark=lark_port,  # type: ignore[arg-type]
        harness=harness,
        routes=_ports.ChildRoutes(chat_id),
        channel_actor_id=f"adapter:e2e:lark:{_ports.APP_NAME}",
        logger=Logger.adapter(root / "live-unsupported-logs", name=_ports.APP_NAME),
    )
    outcome = adapter.handle_sdk_event(
        sdk_event(
            message_type="quantum_teleport",
            content=json.dumps({"opaque": True}),
            message_id=original_message_id,
            chat_id=chat_id,
            event_id=f"event-live-unsupported-{original_message_id}",
        )
    )
    if len(lark_port.reply_ids) != 1:
        raise ContractFailure(
            f"unsupported event produced {len(lark_port.reply_ids)} native replies"
        )
    reply_id = lark_port.reply_ids[0]
    reply = _ports.get_message(reply_id)
    return {
        "status": outcome.status,
        "harness_requests": len(harness.requests),
        "reply_id": reply_id,
        "reply_to": reply.get("reply_to"),
        "content": reply.get("content"),
    }
