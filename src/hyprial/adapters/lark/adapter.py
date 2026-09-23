"""Lark event conversion, routing, acknowledgement, and receipt correlation."""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hyprial.alarm import Alarm, AlarmEmitter
from hyprial.adapters.lark.reply_bridge import lark_reply_adapter
from hyprial.contracts import ipc_errors
from hyprial.contracts.ipc_errors import DaemonRequestError
from hyprial.log import Logger
from hyprial.home import configured_hyprial_home

from .api import (
    MAX_LARK_TEXT_CONTENT_BYTES,
    MAX_RUNTIME_TARGET_ITEM_BYTES,
    AgentDirectoryEntry,
    MERGE_FORWARD_LABEL,
    ActorTarget,
    DeliveryOutcome,
    HarnessDelivery,
    HarnessPort,
    HarnessRequest,
    InboundOutcome,
    LarkApiPort,
    LarkInboundMessage,
    ReconcileReport,
    RouteDirectory,
    canonical_lark_message_type,
    encode_lark_text_content,
    is_safe_runtime_target_field,
    normalize_lark_message_content,
)
from .health import DEFAULT_RECONCILE_LOOKBACK_SECONDS
from .sdk import LarkApiError
from .inbound_runtime import LarkInboundRuntime
from .reaction_effects import ReactionEffectAdmission, ReactionEffectsRuntime
from .scopes import parse_permission_violation
from .sdk import LARK_HISTORY_CODES_PERMANENT_CHAT_GONE
from .state import (
    DeadLetter,
    LarkStateStore,
    PendingCommandCapacityError,
    PendingCommandResponse,
    ReplyRoute,
    RequestCorrelation,
)

ACK_EMOJI = "OnIt"
# Feishu returns either code when an app without broad group-history access
# attempts to list a chat's messages. Keep this allowlist specific to that one
# endpoint: any other Lark API failure must remain reconciliation-fatal.
_HISTORY_SCOPE_FORBIDDEN_CODES = frozenset({230002, 230027})
DEFAULT_DEAD_LETTER_ALERT_THRESHOLD = 10
DEAD_LETTER_ALERT_WINDOW_SECONDS = 60 * 60
# PR #332 F3: in-place bounded retry for daemon-bound inbound steps during
# the daemon's restore window.  The budget must cover a derived restore
# round (F1: (ceil(targets/width)+1) x per-start timeout) for typical
# fleets -- 120s covers ~8 targets at the default admission width and
# start timeout -- while staying short enough that a wedged restore does
# not pin the ordered inbound lane for long.
DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS = 120.0
DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS = 0.5
DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS = 5.0
# Platform codes meaning "this chat is not readable, and no retry changes
# that".  They complete the criterion a954c161 introduced -- failures split by
# *whether retrying could ever clear them* -- which until now recognised only
# one way of being permanent, a missing scope.  A chat the bot was removed
# from is equally beyond retry, and landed in the retryable bucket by default.
#
# Documented by the platform (open.feishu.cn, chat and chat-member endpoints):
#   232006  the chat_id is invalid
#   232009  the chat has been dissolved
#   232011  the operator (this bot) is not in the chat
#
# ⚠️ Sourced for the chat endpoints; this sweep calls the message-list
# endpoint, and its own error table was not found in those docs.  That is why
# an unrecognised code still fails closed below rather than being assumed
# permanent: adding a code here may be a no-op, but it can never turn a
# transient failure into a silent pass.
#
# "Permanent" here means "no retry clears it", not "never clears" -- someone
# re-adding the bot fixes 232011, exactly as granting a scope fixes the
# missing-scope case that already sits in this bucket.
_UNREADABLE_CHAT_CODES = frozenset({232006, 232009, 232011})

_FIXED_OFFSET = re.compile(r"UTC([+-])(\d{2}):(\d{2})(?::(\d{2}))?")

OperatorNotifier = Callable[[str, str], bool]


def _permanent_chat_failure_code(error: BaseException) -> int | None:
    """The platform code when a history refusal is permanent, else None.

    Classification is by SDK error code only -- never by error text, which
    is both unlocalized and credential-bearing.  Anything unrecognized
    (including a ``LarkApiError`` without a code) stays transient so the
    health layer keeps failing closed.
    """

    if (
        isinstance(error, LarkApiError)
        and error.code in LARK_HISTORY_CODES_PERMANENT_CHAT_GONE
    ):
        return error.code
    return None


def _reconcile_error_category(error: BaseException) -> str:
    """Name the failure class from *structured* fields only.

    ``history-permission-unavailable`` is the one incomplete outcome that must
    NOT restart the adapter: a mention-only Feishu app cannot replay messages it
    was never permitted to read, but its websocket subscription still receives
    new @-mentions (worker.py's probe predicate keys off this exact suffix).
    Everything else stays ``history-unavailable`` and keeps failing closed.
    """
    permission_denied = parse_permission_violation(error) is not None
    # Feishu's message-list endpoint can return a history-scope denial without a
    # structured ``permission_violations`` payload.
    history_scope_forbidden = (
        isinstance(error, LarkApiError)
        and error.operation == "list chat messages"
        and error.code in _HISTORY_SCOPE_FORBIDDEN_CODES
    )
    if permission_denied or history_scope_forbidden:
        return "history-permission-unavailable"
    return "history-unavailable"


class TransientHarnessRetryExhausted(RuntimeError):
    """A transient daemon refusal outlived the inbound retry budget.

    Carries the refusal's stable code (or, for a transport blip, the
    exception name) so the dead-letter trail records WHY the budget burned
    out -- distinct from a permanent ``forward-error`` rejection.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = detail


def _channel_actor_adapter_name(channel_actor_id: str) -> str:
    """The operator-facing adapter name from either channel URI shape.

    Three-segment ``channel:lark:<adapter>`` via the reply-bridge parser,
    four-segment ``channel:<owner>:<machine>:<adapter>`` via the one
    channel-URI deconstructor; anything else stays verbatim.
    """

    bridged = lark_reply_adapter(channel_actor_id)
    if bridged is not None:
        return bridged
    from hyprial.uri import parse_channel_uri

    parsed = parse_channel_uri(channel_actor_id)
    return parsed[2] if parsed is not None else channel_actor_id


def _fixed_offset_name(offset: timedelta) -> str:
    seconds = int(offset.total_seconds())
    if seconds == 0:
        return "UTC"
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    suffix = f":{seconds:02d}" if seconds else ""
    return f"UTC{sign}{hours:02d}:{minutes:02d}{suffix}"


def _timezone(value: str):  # type: ignore[no-untyped-def]
    if value == "UTC":
        return UTC
    fixed = _FIXED_OFFSET.fullmatch(value)
    if fixed is not None:
        hours, minutes, seconds = (int(part or 0) for part in fixed.groups()[1:])
        total = hours * 3600 + minutes * 60 + seconds
        if minutes > 59 or seconds > 59 or total >= 24 * 3600:
            raise ValueError("invalid fixed UTC offset")
        if fixed.group(1) == "-":
            total = -total
        return timezone(timedelta(seconds=total))
    if not is_safe_runtime_target_field(value) or len(value.encode("utf-8")) > 255:
        raise ValueError("invalid timezone name")
    try:
        return ZoneInfo(value)
    except (
        ZoneInfoNotFoundError,
        ValueError,
        OverflowError,
        UnicodeError,
        OSError,
    ) as error:
        raise ValueError("invalid timezone name") from error


def _local_timezone() -> str:
    configured = os.environ.get("TZ", "").removeprefix(":")
    if configured:
        try:
            zone = _timezone(configured)
        except ValueError:
            pass
        else:
            if isinstance(zone, ZoneInfo):
                return configured
            offset = zone.utcoffset(None)
            if offset is not None:
                return _fixed_offset_name(offset)
    zone = datetime.now().astimezone().tzinfo
    key = getattr(zone, "key", None)
    if isinstance(key, str) and key:
        return key
    offset = zone.utcoffset(None) if zone is not None else None
    return _fixed_offset_name(offset or timedelta(0))


def _time_in(zone: str) -> str:
    return datetime.now(_timezone(zone)).strftime(
        f"%Y-%m-%d %H:%M:%S %z [{zone}]"
    )


def _bounded_listing(
    header: str,
    values: tuple[tuple[str, str | None], ...],
    *,
    total: int,
    empty_message: str = "(none online)",
) -> str:
    """Render untrusted target records under Lark's UTF-8 reply limit."""

    safe = tuple(
        sorted(
            (
                (value, annotation)
                for value, annotation in values
                if is_safe_runtime_target_field(value)
                and (
                    annotation is None
                    or is_safe_runtime_target_field(annotation)
                )
                and len(
                    (
                        f"- {value}"
                        + (f" [{annotation}]" if annotation is not None else "")
                    ).encode("utf-8")
                )
                <= MAX_RUNTIME_TARGET_ITEM_BYTES
            ),
            key=lambda item: (item[0], item[1] or ""),
        )
    )
    lines: list[str] = [header]
    shown = 0
    for value, annotation in safe:
        line = f"- {value}" + (f" [{annotation}]" if annotation is not None else "")
        footer = f"shown={shown + 1} total={total}"
        candidate = "\n".join((*lines, line, footer))
        if (
            len(encode_lark_text_content(candidate).encode("utf-8"))
            >= MAX_LARK_TEXT_CONTENT_BYTES
        ):
            break
        lines.append(line)
        shown += 1
    if shown == 0 and total == 0:
        lines.append(empty_message)
    lines.append(f"shown={shown} total={total}")
    result = "\n".join(lines)
    assert (
        len(encode_lark_text_content(result).encode("utf-8"))
        < MAX_LARK_TEXT_CONTENT_BYTES
    )
    return result


def _fallback_agent_directory(
    routes: RouteDirectory,
) -> tuple[AgentDirectoryEntry, ...]:
    """Keep older test/dummy route implementations usable during the seam rollout."""

    return tuple(
        AgentDirectoryEntry(
            name=target.target_uri,
            status="running" if target.status == "online" else "down",
        )
        for target in routes.list_runtime_targets()
    )


def _agent_directory_listing(
    entries: tuple[AgentDirectoryEntry, ...],
) -> str:
    rows: list[tuple[str, str | None]] = []
    for entry in entries:
        if not is_safe_runtime_target_field(entry.name):
            continue
        if entry.status in {"running", "online"}:
            status = "running"
        elif entry.status in {"down", "offline"}:
            status = "down"
        else:
            continue
        adapters = ",".join(entry.pinned_adapters) or "—"
        preferred = entry.preferred_harness or "—"
        if not is_safe_runtime_target_field(adapters) or not is_safe_runtime_target_field(
            preferred
        ):
            continue
        rows.append(
            (
                f"{entry.name} | {status} | {adapters} | {preferred}",
                None,
            )
        )
    return _bounded_listing(
        "Agent entities (name | status | bound adapter | preferred harness):",
        tuple(rows),
        total=len(entries),
        empty_message="No agent entities are registered or present.",
    )


def _org_context_summary(path: Path) -> str:
    """Read the accepted org-context file without importing the org module.

    The file is a Markdown document whose machine-readable prefix contains a
    small YAML ``meta`` mapping.  This intentionally parses only scalar meta
    fields; the org worker owns validation and signature semantics.
    """

    absent = "org-context absent — 尚未采信任何组织上下文"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return absent

    meta_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "meta:"),
        None,
    )
    if meta_index is None:
        return absent
    values: dict[str, str] = {}
    end = len(lines)
    for index in range(meta_index + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            match = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
            if match is None:
                return absent
            value = re.sub(r"\s+#.*$", "", match.group(2)).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value or not is_safe_runtime_target_field(value):
                return absent
            values[match.group(1)] = value
            continue
        end = index
        break
    publisher = values.get("publisher") or values.get("authority")
    version = values.get("version")
    issued_at = values.get("issued_at")
    if not version or not issued_at or not publisher:
        return absent

    preview: list[str] = []
    for line in lines[end:]:
        stripped = line.strip()
        if not stripped or stripped == "---" or stripped == "```":
            continue
        if not is_safe_runtime_target_field(stripped):
            return absent
        preview.append(stripped)
        if len(preview) == 3:
            break
    result = [
        "org-context:",
        f"version={version} issued_at={issued_at} publisher={publisher}",
    ]
    if preview:
        result.append("preview:")
        result.extend(f"- {line}" for line in preview)
    return "\n".join(result)


def _command_response_kind(text: str) -> str:
    folded = text.strip().casefold()
    if re.fullmatch(r"/hyprial(?:\s+help)?\s*", folded):
        return "help"
    if re.fullmatch(r"/hyprial\s+agents\s*", folded):
        return "agents"
    if re.fullmatch(r"/hyprial\s+org\s*", folded):
        return "org"
    if re.match(r"^/hyprial\s+agents\s+(?:pin|unpin)(?:\s|$)", folded):
        return "pin-unsupported"
    if re.match(r"^/hyprial\s+set\s+timezone", folded):
        return "timezone"
    if re.match(r"^/hyprial\s+route(?:\s|$)", folded):
        return "route-unsupported"
    return "help-unknown"


def _command_help() -> str:
    return "\n".join(
        (
            "Harness Bridge Lark commands:",
            "/hyprial agents",
            "/hyprial org",
            "/hyprial ask <runtime-target> <message>",
            "/hyprial set timezone=<IANA-zone>",
            "/hyprial set timezone",
            "/hyprial set timezone=local",
            "The agents view lists Agent entities and current daemon liveness.",
        )
    )


#: How far back a post-reconnect sweep pulls chat history.  Bounded: the
#: websocket auto-reconnects within seconds-to-minutes, so a short window
#: covers realistic outages without re-scanning deep history.
def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Lark event {name} must be a non-empty string")
    return value


def _create_time_ms(value: str | None) -> int:
    """Best-effort parse of a millisecond create_time; unparseable sorts old."""

    try:
        return int(value) if value else 0
    except ValueError:
        return 0


def normalize_sdk_event(value: Any) -> LarkInboundMessage:
    """Normalize the official SDK's ``P2ImMessageReceiveV1`` model."""

    header = value.header
    event = value.event
    message = event.message
    sender = event.sender
    sender_id = sender.sender_id
    message_id = _required(message.message_id, "message.message_id")
    message_type = _required(message.message_type, "message.message_type")
    normalized = normalize_lark_message_content(
        message_type,
        _required(message.content, "message.content"),
        # The carrying message's own id, so image/file stand-ins print the
        # ``ref:<message_id>/<key>`` handle for ``hyprial adapter media get``.
        message_id=message_id,
    )
    text = normalized.text
    mention_names: list[str] = []
    mention_open_ids: list[str] = []
    for mention in message.mentions or ():
        name = getattr(mention, "name", None)
        key = getattr(mention, "key", None)
        if isinstance(name, str) and name:
            mention_names.append(name)
        if isinstance(key, str) and key:
            text = text.replace(key, "")
        # ``MentionEvent.id`` is a UserId object; its open_id is the identity
        # key bot-mention routing compares against the app's own bot open_id.
        identity = getattr(mention, "id", None)
        open_id = getattr(identity, "open_id", None)
        if isinstance(open_id, str) and open_id:
            mention_open_ids.append(open_id)
    sender_union_id = getattr(sender_id, "union_id", None)
    return LarkInboundMessage(
        event_id=_required(header.event_id, "header.event_id"),
        message_id=message_id,
        chat_id=_required(message.chat_id, "message.chat_id"),
        chat_type=_required(message.chat_type, "message.chat_type"),
        text=text.strip(),
        sender_id=_required(sender_id.open_id, "sender.sender_id.open_id"),
        sender_type=_required(sender.sender_type, "sender.sender_type"),
        root_id=message.root_id,
        thread_id=getattr(message, "thread_id", None),
        reply_to=message.parent_id,
        create_time=message.create_time,
        message_type=normalized.message_type,
        content_status=normalized.status,
        mentions=tuple(mention_names),
        mention_open_ids=tuple(mention_open_ids),
        sender_union_id=(
            sender_union_id
            if isinstance(sender_union_id, str) and sender_union_id
            else None
        ),
    )


class LarkAdapter:
    def __init__(
        self,
        *,
        state: LarkStateStore,
        lark: LarkApiPort,
        reaction_lark: LarkApiPort | None = None,
        reaction_effects: ReactionEffectsRuntime | None = None,
        harness: HarnessPort,
        routes: RouteDirectory,
        channel_actor_id: str,
        logger: Logger | None = None,
        notify_operator: OperatorNotifier | None = None,
        dead_letter_alert_threshold: int = DEFAULT_DEAD_LETTER_ALERT_THRESHOLD,
        transient_retry_budget: float = DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS,
        transient_retry_backoff_initial: float = (
            DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS
        ),
        transient_retry_backoff_max: float = (
            DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS
        ),
        utcnow: Callable[[], datetime] | None = None,
        org_context_path: Path | None = None,
    ) -> None:
        if dead_letter_alert_threshold < 1:
            raise ValueError("dead-letter alert threshold must be positive")
        if transient_retry_budget < 0.0:
            raise ValueError("transient retry budget must not be negative")
        if transient_retry_backoff_initial <= 0.0:
            raise ValueError("transient retry backoff start must be positive")
        self._state = state
        self._lark = lark
        self._reaction_lark = reaction_lark or lark
        self._reaction_lock = threading.Lock()
        self._reaction_effects = reaction_effects
        self._harness = harness
        self._routes = routes
        self._channel_actor_id = channel_actor_id
        # The operator-facing adapter name, for guidance that names the exact
        # CLI command instead of a dead-end "pin an agent" instruction.
        self._adapter_name = _channel_actor_adapter_name(channel_actor_id)
        self._logger = logger.bind(component="adapter") if logger else None
        self._notify_operator = notify_operator
        self._dead_letter_alert_threshold = dead_letter_alert_threshold
        self._transient_retry_budget = transient_retry_budget
        self._transient_retry_backoff_initial = transient_retry_backoff_initial
        self._transient_retry_backoff_max = max(
            transient_retry_backoff_max, transient_retry_backoff_initial
        )
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._org_context_path = org_context_path or (
            configured_hyprial_home()[0] / "org-context.md"
        )
        self._alarm = (
            AlarmEmitter(
                self._logger,
                deliver_human=self._deliver_native_alarm,
                claim=self._state.claim_alarm,
                clock_ms=lambda: int(self._utcnow().timestamp() * 1000),
            )
            if self._logger is not None
            else None
        )
        self._inbound = LarkInboundRuntime(
            self._adapter_name,
            processor=self._process_inbound,
        )

    def _process_inbound(
        self,
        message: LarkInboundMessage,
        suppress_guidance: bool,
    ) -> InboundOutcome:
        return self._handle_inbound(
            message,
            suppress_guidance=suppress_guidance,
        )

    def close(self, timeout: float = 5.0) -> None:
        """Boundedly drain actor admission and its ordered effect lane."""

        self._inbound.close(timeout)
        if self._reaction_effects is not None:
            self._reaction_effects.close(timeout)
        self._state.close()

    def __del__(self) -> None:
        # Tests and embedders historically treated LarkAdapter as a plain
        # value.  Keep that seam leak-free while explicit worker shutdown uses
        # close() for a real bounded drain.
        try:
            self.close(0.0)
        except Exception:
            pass

    def handle_sdk_event(self, event: Any) -> InboundOutcome:
        try:
            message = normalize_sdk_event(event)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - normalization boundary
            # A message we cannot parse (non-text content, missing fields)
            # would otherwise vanish without a trace; keep what we can.
            self._dead_letter_unparsed(event, error)
            return InboundOutcome(status="normalize-error", ack_error=str(error))
        return self.handle_inbound(message)

    def _expanded_merge_forward(
        self, message: LarkInboundMessage
    ) -> LarkInboundMessage:
        """Replace a forward's placeholder with the text its children carry.

        The websocket event for a merge-forward carries only the platform's
        ``Merged and Forwarded Message`` placeholder -- the forwarded
        conversation exists solely in ``im.v1.message.get``.  The bounded
        fetch executes on the inbound actor's ordered effect lane rather than
        an SDK websocket thread.

        This never raises and never dead-letters: an unreachable platform
        degrades to a loud label, because a forward the agent can see the
        shape of beats one it never receives.

        Duplicates are pre-checked (reads take their own state locks) so a
        redelivered event or a reconcile sweep full of already-seen forwards
        does not refetch each one; :meth:`_handle_inbound` remains the
        authoritative duplicate gate.
        """

        if self._state.seen(message.event_id) or self._state.seen_message(
            message.message_id
        ):
            return message
        try:
            quote = self._lark.get_message(message.message_id)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            del error
            quote = None
        if quote is None or not quote.text.strip():
            return replace(
                message,
                text=f"[{MERGE_FORWARD_LABEL} (contents unavailable)]",
            )
        return replace(message, text=quote.text.strip())

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        """Send through this receiving user's own app-scoped adapter."""

        return self._lark.send_owner_dm(
            open_id, text, idempotency_key=idempotency_key
        )

    def handle_inbound(
        self, message: LarkInboundMessage, *, suppress_guidance: bool = False
    ) -> InboundOutcome:
        canonical_type = canonical_lark_message_type(message.message_type)
        if canonical_type != message.message_type:
            # Callers of this public seam may bypass the SDK normalizer. Do
            # not permit an attacker-controlled type/body to reach outbound
            # metadata or the durable dead-letter store through that path.
            message = replace(
                message,
                message_type="unknown",
                content_status="unsupported",
                text="[Unsupported Lark message type]",
            )
        # Official SDK callbacks may run concurrently.  The inbound actor owns
        # admission and in-flight event/message deduplication; blocking REST,
        # SQLite and daemon IPC execute on its bounded effect pool.
        return self._inbound.process(
            message,
            suppress_guidance=suppress_guidance,
        )

    def _with_transient_retry(self, operation: Callable[[], Any]) -> Any:
        """Run one daemon-bound inbound step, retrying transient refusals.

        PR #332 F3: a DAEMON_RESTORING envelope is the daemon's restore
        gate refusing the call at dispatch -- BEFORE any side effect -- so
        retrying it in place is safe even for the ``message.send``
        mutation; a transport blip while the daemon generation restarts
        (OSError/TimeoutError) is equally side-effect-free.  Anything else
        is permanent for this one websocket event and propagates
        immediately to the dead-letter path.  The budget and backoff are
        bounded so a wedged restore cannot pin the ordered inbound lane
        forever; an exhausted transient falls as
        TransientHarnessRetryExhausted, which the failure path records
        under its own dead-letter reason.
        """
        backoff = self._transient_retry_backoff_initial
        deadline = time.monotonic() + self._transient_retry_budget
        while True:
            try:
                return operation()
            except (NameError, ImportError):
                raise
            # PR #332 F4②: the restore-gate verdict is caught by its registered
            # class instead of a code comparison.  The other transient codes
            # are transport-minted and already surface as OSError/TimeoutError
            # here, so the retry set is unchanged.
            except ipc_errors.DaemonRestoringError as error:
                detail = error.code
            except DaemonRequestError:
                raise
            except (OSError, TimeoutError) as error:
                detail = type(error).__name__
            if time.monotonic() >= deadline:
                raise TransientHarnessRetryExhausted(detail) from None
            time.sleep(backoff)
            backoff = min(backoff * 2.0, self._transient_retry_backoff_max)

    def _notify_sender_of_dead_letter(
        self,
        message: LarkInboundMessage,
        *,
        reason: str,
        code: str | None,
    ) -> None:
        """Best-effort native "not delivered" reply to the sender (F3).

        The dead-letter store is the custody boundary -- the body survives
        for replay and audit; this reply is the sender's only signal that
        their message did not arrive.  It must never break the dead-letter
        path itself, and the per-message idempotency key keeps a replay of
        the same message from re-notifying.
        """
        label = code if code else reason
        text = (
            "Delivery failed\n"
            f"Code: {label}\n"
            "Your message was not delivered. It is preserved in the "
            "dead-letter store for replay."
        )
        try:
            self._lark.reply(
                message.message_id,
                text,
                idempotency_key=f"{message.message_id}:dead-letter:{reason}",
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - notification is best effort
            pass

    def _handle_inbound(
        self, message: LarkInboundMessage, *, suppress_guidance: bool = False
    ) -> InboundOutcome:
        # Idempotency is keyed on BOTH the event id (platform redelivery of
        # the same event) and the native message id (the same message
        # arriving via live event, manual recovery, or reconcile replay).
        if self._state.seen(message.event_id) or self._state.seen_message(
            message.message_id
        ):
            return InboundOutcome(status="duplicate")
        # Retirement is lifted by evidence from OUTSIDE the sweep, never by
        # the sweep that imposed it.  The invariant covering every upstream
        # of this function (live SDK events, reconcile replays, and
        # ``recover_message`` re-drives): any path that reaches here holds a
        # ``LarkInboundMessage`` the platform served for this chat, and the
        # platform only serves a chat's messages to a member -- so a served
        # message IS membership evidence.  ``recover_message`` is covered by
        # its own early return: when the platform will not serve the body
        # (bot not in the chat), ``get_inbound_message`` answers None and it
        # returns ``not-found`` BEFORE reaching here.  That early return
        # rests on the platform refusing non-member reads of
        # ``im.v1.message.get``; if a tenant-level scope ever lets a
        # non-member read through, the worst case is a self-correcting
        # oscillation (rejoin scan set -> next sweep eats 230002 -> retired
        # again), never data loss.
        # The read probe keeps the common case (nothing ever retired, or the
        # row already gone) free of a write transaction per inbound message.
        if self._state.retired_chat_code(message.chat_id) is not None:
            self._unretire_chat(message.chat_id)
        if message.message_type == "merge_forward":
            message = self._expanded_merge_forward(message)
        try:
            # Remember the chat type observed on live traffic; the REST
            # message models carry no chat_type, so recovery paths rely on
            # this record to route recovered messages from the same chat.
            if message.chat_type != "unknown":
                self._state.record_chat_type(message.chat_id, message.chat_type)
            self._observe_sender_identity(message)
            if message.content_status != "supported":
                reason = (
                    "unsupported-message-type"
                    if message.content_status == "unsupported"
                    else "invalid-message-content"
                )
                created = self._record_dead_letter(message, reason=reason)
                return InboundOutcome(status=reason, new_dead_letter=created)
            return self._forward_inbound(message, suppress_guidance=suppress_guidance)
        except PendingCommandCapacityError as error:
            # Capacity is custody, not a cache. Refuse the new command before
            # any Lark platform send and expose only bounded numeric state.
            return InboundOutcome(
                status="command-capacity-exhausted",
                pending_capacity=error.capacity,
            )
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - failure-path preservation
            # The forwarding path failed (IPC, state, platform).  The event
            # is gone from the websocket stream for good, so preserve the
            # body before reporting the failure.  A transient refusal that
            # outlived the retry budget is recorded under its own reason so
            # replay tooling can tell "the daemon never finished restore"
            # from a permanent rejection.  Either way the sender is told
            # their message was not delivered (best effort).
            if isinstance(error, TransientHarnessRetryExhausted):
                reason = "forward-retry-exhausted"
                detail = error.code
            else:
                reason = "forward-error"
                detail = (
                    error.code
                    if isinstance(error, DaemonRequestError)
                    else type(error).__name__
                )
            created = self._record_dead_letter(message, reason=reason, detail=detail)
            if created and isinstance(
                error, (DaemonRequestError, TransientHarnessRetryExhausted)
            ):
                # The "not delivered" reply is scoped to daemon-forward
                # failures (F3's wire refusals).  State-store failures keep
                # the custody fence pinned by the local-command tests: when
                # pending state cannot be written, no platform send happens
                # at all -- an unpersisted reply must never be sent.
                self._notify_sender_of_dead_letter(
                    message, reason=reason, code=detail if detail != reason else None
                )
            return InboundOutcome(
                status="error",
                ack_error=type(error).__name__,
                new_dead_letter=created,
            )

    def _forward_inbound(
        self, message: LarkInboundMessage, *, suppress_guidance: bool
    ) -> InboundOutcome:
        pending = self._state.pending_command_response(message.message_id)
        if pending is not None:
            return self._send_pending_command(message, pending)
        slash = re.fullmatch(
            r"/hyprial[ \t]+ask[ \t]+(\S+)[ \t]+(.+)",
            message.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if slash is not None:
            token = slash.group(1)
            if not is_safe_runtime_target_field(token):
                reply = (
                    "Delivery failed\nCode: INVALID_TARGET\n"
                    "Runtime target must be valid printable UTF-8 under 4096 bytes."
                )
                return self._consume_command(
                    message,
                    reply,
                    status="command-error",
                    response_kind="ask-invalid",
                )
            matches = self._with_transient_retry(
                lambda: self._routes.resolve_runtime_target(token)
            )
            if len(matches) != 1:
                if not matches:
                    reply = (
                        "Delivery failed\nCode: TARGET_NOT_FOUND\n"
                        f"Runtime target: {token}\n"
                        "Run /hyprial agents and use an exact listed target URI."
                    )
                    response_kind = "ask-not-found"
                else:
                    reply = _bounded_listing(
                        "Delivery failed\nCode: AMBIGUOUS_TARGET\n"
                        f"Alias: {token}\nCandidates:",
                        tuple((item.actor_id, None) for item in matches),
                        total=len(matches),
                    )
                    response_kind = "ask-ambiguous"
                return self._consume_command(
                    message,
                    reply,
                    status="command-error",
                    response_kind=response_kind,
                )
            target = matches[0]
            if not all(
                is_safe_runtime_target_field(value)
                for value in (
                    target.actor_id,
                    target.actor_key,
                    target.display_name,
                )
            ):
                return self._consume_command(
                    message,
                    "Delivery failed\nCode: INVALID_TARGET_RESPONSE",
                    status="command-error",
                    response_kind="ask-invalid-response",
                )
            route_status = "slash-ask"
            # The adapter owns command parsing. Only the requested body may
            # cross the Harness boundary; command syntax is never prompt text.
            message = replace(message, text=slash.group(2))
        else:
            command = self._handle_command(message)
            if command is not None:
                return self._consume_command(
                    message,
                    command,
                    response_kind=_command_response_kind(message.text),
                )
            target, route_status = self._route(message)
        if target is None:
            # "ignored" is a group message that never addressed us — not a
            # loss.  Anything a user plausibly expected us to handle keeps
            # its body in the dead-letter trail.
            created = False
            if route_status != "ignored":
                created = self._record_dead_letter(message, reason=route_status)
            guidance = (
                None
                if suppress_guidance or self._alarm is not None
                else self._route_guidance(message, route_status)
            )
            if guidance is not None:
                try:
                    self._lark.reply(
                        message.message_id,
                        guidance,
                        idempotency_key=(
                            f"{message.message_id}:route-guidance:{route_status}"
                        ),
                    )
                    self._state.record_seen(message.event_id)
                except (NameError, ImportError):
                    raise
                except Exception as error:  # noqa: BLE001 - platform/state boundary
                    return InboundOutcome(status="route-error", ack_error=str(error))
            return InboundOutcome(
                status=route_status,
                new_dead_letter=created,
            )

        text = self._fallback_quote_context(message, route_status)
        request = HarnessRequest(
            message_id=message.message_id,
            from_actor_id=self._channel_actor_id,
            to=target,
            text=text,
            conversation_id=message.conversation_id,
            provider_metadata={
                "provider": "lark",
                "eventId": message.event_id,
                "messageId": message.message_id,
                "chatId": message.chat_id,
                "chatType": message.chat_type,
                "rootId": message.root_id,
                "threadId": message.thread_id,
                "replyTo": message.reply_to,
                "senderId": message.sender_id,
                "senderType": message.sender_type,
                "createTime": message.create_time,
                "messageType": message.message_type,
            },
        )
        receipt = self._with_transient_retry(
            lambda: self._harness.send_request(request)
        )
        if self._logger is not None:
            try:
                self._logger.info(
                    "adapter.inbound",
                    messageId=receipt.message_id,
                    correlationId=receipt.message_id,
                    node="adapter-inbound",
                    conversationId=message.conversation_id,
                    actorId=self._channel_actor_id,
                    sender=self._channel_actor_id,
                    target=target.actor_id,
                    platform="lark",
                    nativeMessageId=message.message_id,
                    nativeEventId=message.event_id,
                )
            except (NameError, ImportError):
                raise
            except OSError:
                # The daemon receipt is custody. Losing local visibility must
                # not cause the adapter to resubmit an already accepted request.
                pass
        self._state.record_inbound(
            RequestCorrelation(
                harness_message_id=receipt.message_id,
                message_id=message.message_id,
                chat_id=message.chat_id,
                conversation_id=message.conversation_id,
            ),
            event_id=message.event_id,
        )
        ack_error = None
        if self._reaction_effects is not None:
            admission = self._reaction_effects.ack(message.message_id)
            if admission is not ReactionEffectAdmission.ACCEPTED:
                ack_error = f"reaction-effect-{admission.value}"
        else:
            try:
                with self._reaction_lock:
                    self._reaction_lark.add_reaction(message.message_id, ACK_EMOJI)
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - platform boundary
                # ACK visibility cannot undo accepted Harness custody.
                ack_error = str(error)
        return InboundOutcome(
            status="forwarded",
            harness_message_id=receipt.message_id,
            target_actor_id=target.actor_id,
            ack_error=ack_error,
        )

    def _consume_command(
        self,
        message: LarkInboundMessage,
        reply: str,
        *,
        response_kind: str,
        status: str = "command",
    ) -> InboundOutcome:
        pending = self._state.record_pending_command_response(
            PendingCommandResponse(
                message_id=message.message_id,
                event_id=message.event_id,
                kind=response_kind,
                text=reply,
                idempotency_key=(
                    f"{message.message_id}:command:{response_kind}"
                ),
            )
        )
        return self._send_pending_command(message, pending, status=status)

    def _send_pending_command(
        self,
        message: LarkInboundMessage,
        pending: PendingCommandResponse,
        *,
        status: str | None = None,
    ) -> InboundOutcome:
        self._lark.reply(
            pending.message_id,
            pending.text,
            idempotency_key=pending.idempotency_key,
        )
        self._state.complete_command_response(
            pending,
            delivered_event_id=message.event_id,
        )
        if status is None:
            status = "command-error" if pending.kind.startswith("ask-") else "command"
        return InboundOutcome(status=status)

    def _handle_command(self, message: LarkInboundMessage) -> str | None:
        text = message.text.strip()
        if not re.match(r"^/hyprial(?:\s|$)", text, re.IGNORECASE):
            return None
        if re.fullmatch(r"/hyprial(?:\s+help)?\s*", text, re.IGNORECASE):
            return _command_help()
        if re.fullmatch(r"/hyprial\s+agents\s*", text, re.IGNORECASE):
            list_directory = getattr(self._routes, "list_agent_directory", None)
            entries = (
                list_directory()
                if callable(list_directory)
                else _fallback_agent_directory(self._routes)
            )
            return _agent_directory_listing(entries)
        if re.fullmatch(r"/hyprial\s+org\s*", text, re.IGNORECASE):
            return _org_context_summary(self._org_context_path)
        if re.fullmatch(
            r"/hyprial\s+agents\s+(?:pin(?:\s+\S+)?|unpin)\s*",
            text,
            re.IGNORECASE,
        ):
            # Read-only visibility plus the exact operator command.  Writing
            # a pin from chat stays refused: a pin decides who receives this
            # adapter's DMs, and Lark carries no hyprial operator identity to
            # authorize that.
            pinned = self._routes.pinned_actor(message.conversation_id)
            status = (
                f"This adapter ({self._adapter_name}) is pinned to: "
                f"{pinned.actor_id}"
                if pinned is not None
                else f"This adapter ({self._adapter_name}) has no pinned agent."
            )
            return (
                f"{status}\n"
                "Changing a pin is not supported in Lark chat. An operator "
                "can change it on the daemon machine with: "
                f"hyprial adapter pin {self._adapter_name} <agent> "
                f"(or: hyprial adapter unpin {self._adapter_name})."
            )
        timezone = re.fullmatch(
            r"/hyprial\s+set\s+timezone=(\S+)\s*", text, re.IGNORECASE
        )
        if timezone is not None:
            if message.chat_type != "p2p":
                return "Timezone settings are available only in a direct message."
            zone = timezone.group(1)
            if zone.casefold() == "local":
                self._state.set_conversation_timezone(message.conversation_id, None)
                zone = _local_timezone()
                return (
                    f"Timezone restored to machine default: {zone}\n"
                    f"Current time: {_time_in(zone)}"
                )
            try:
                _timezone(zone)
            except ValueError:
                return f"Invalid timezone '{zone}'. Example: Asia/Taipei."
            self._state.set_conversation_timezone(message.conversation_id, zone)
            return (
                f"Timezone set for this DM: {zone}\nCurrent time: {_time_in(zone)}"
            )
        if re.fullmatch(r"/hyprial\s+set\s+timezone\s*", text, re.IGNORECASE):
            if message.chat_type != "p2p":
                return "Timezone settings are available only in a direct message."
            override = self._state.conversation_timezone(message.conversation_id)
            zone = override or _local_timezone()
            source = "DM override" if override else "machine default"
            try:
                current_time = _time_in(zone)
            except ValueError:
                return (
                    "Stored timezone is invalid. Set a valid IANA zone, "
                    "fixed UTC offset, or timezone=local."
                )
            return (
                f"Effective timezone: {zone} ({source})\n"
                f"Current time: {current_time}"
            )
        if re.match(r"^/hyprial\s+route(?:\s|$)", text, re.IGNORECASE):
            return (
                "Route commands are not supported by the Lark adapter in this build."
            )
        return _command_help()

    #: How an event's ``sender_type`` maps onto the identity taxonomy.  A
    #: message sent by an app arrives with the app's *bot* open_id, so it is
    #: recorded as the bot presence, not the app itself.
    _SENDER_IDENTITY_KINDS = {"user": "user", "app": "bot"}

    def _observe_sender_identity(self, message: LarkInboundMessage) -> None:
        """Passively record who was seen sending, without names.

        Identity collection is bookkeeping on the side of the inbound path:
        it must never fail a message, so every error is swallowed.  Events
        carry no sender display name; the name arrives later via
        ``identities sync`` or a member-change event and merges into the
        same row.
        """

        kind = self._SENDER_IDENTITY_KINDS.get(message.sender_type)
        if kind is None or not message.sender_id or message.sender_id == "unknown":
            return
        try:
            self._state.observe_identity(
                kind,
                message.sender_id,
                union_id=message.sender_union_id,
                source=f"event-sender:{message.chat_id}",
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - bookkeeping must never break inbound
            pass

    def handle_member_change_event(self, event: Any) -> None:
        """Record identities from a chat member added/deleted event.

        These events carry name + open_id pairs bound together by the
        platform, so they are the one passive source that can attach display
        names safely.  A deletion still refreshes the identity (the person
        exists; only the membership changed).  Fires only when the app's
        platform-side event subscription includes the im.chat.member.user.*
        events; registration alone does not subscribe.  Never raises.
        """

        try:
            body = getattr(event, "event", None)
            chat_id = getattr(body, "chat_id", None)
            users = getattr(body, "users", None) or ()
            source = (
                f"member-event:{chat_id}"
                if isinstance(chat_id, str) and chat_id
                else "member-event:unknown"
            )
            for user in users:
                identity = getattr(user, "user_id", None)
                open_id = getattr(identity, "open_id", None)
                if not isinstance(open_id, str) or not open_id:
                    continue
                name = getattr(user, "name", None)
                union_id = getattr(identity, "union_id", None)
                self._state.observe_identity(
                    "user",
                    open_id,
                    display_name=(
                        name if isinstance(name, str) and name else None
                    ),
                    union_id=(
                        union_id
                        if isinstance(union_id, str) and union_id
                        else None
                    ),
                    source=source,
                )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - bookkeeping must never break the stream
            pass

    def recover_message(self, message_id: str) -> InboundOutcome:
        """Re-drive one native message through the inbound pipeline.

        Uses ``im.v1.message.get`` to pull the original body from the server
        (the endpoint already used for quoted context), so a message lost on
        a dead websocket or a failed forward is recoverable by its native id
        — e.g. one surfaced in a dead letter.  Idempotent: a message that
        already reached Harness custody returns ``duplicate``.
        """

        if self._state.seen_message(message_id):
            # Already delivered; tidy any stale dead letter and stop.
            self._state.clear_dead_letter(message_id)
            return InboundOutcome(status="duplicate")
        message = self._lark.get_inbound_message(message_id)
        if message is None:
            return InboundOutcome(status="not-found")
        return self.handle_inbound(
            self._with_known_chat_type(message), suppress_guidance=True
        )

    def _unretire_chat(self, chat_id: str) -> None:
        """Lift a chat's retirement on outside-the-sweep membership evidence.

        Caller has already probed that a retirement row exists; the DELETE
        itself stays the conditional write.
        """

        # The DELETE is the side effect; keep it on its own line so no
        # later "tidy" of the condition can reorder it behind a short
        # circuit (a logger-less embedded/test adapter must still lift).
        lifted = self._state.unretire_chat(chat_id)
        if not lifted or self._logger is None:
            return
        try:
            self._logger.log(
                "info",
                "lark.reconcile.chat-unretired",
                adapter=self._state.adapter,
                chat_id=chat_id,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001,S110 - visibility is best effort
            pass

    def _retire_chat(self, chat_id: str, *, code: int) -> None:
        """Exclude a permanently-refused chat from all future sweeps.

        The chat's correlations and dead letters stay as audit records; the
        retirement event carries only the adapter, the chat and the platform
        error code -- never SDK error text (it can contain URLs/tokens).
        """

        newly_retired = self._state.retire_chat(
            chat_id, code=code, now=self._utcnow()
        )
        if not newly_retired or self._logger is None:
            return
        try:
            self._logger.log(
                "warn",
                "lark.reconcile.chat-retired",
                adapter=self._state.adapter,
                chat_id=chat_id,
                code=code,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001,S110 - visibility is best effort
            pass

    def reconcile_recent(
        self,
        *,
        lookback_seconds: int = DEFAULT_RECONCILE_LOOKBACK_SECONDS,
        now_ms: int | None = None,
    ) -> ReconcileReport:
        """Replay messages a websocket outage dropped, per chat, idempotently.

        Scans chats with prior inbound activity (correlations + dead letters)
        for messages inside the lookback window and feeds anything unseen
        through the normal inbound path.  Dedup by native message id makes
        repeat sweeps safe; guidance replies are suppressed so a still-
        unaddressed message is not re-answered on every reconnect.
        """

        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        window_start_ms = now_ms - lookback_seconds * 1000
        start_time = str(window_start_ms // 1000)
        errors: list[str] = []
        blocked: list[str] = []
        retired: list[str] = []
        scanned = forwarded = duplicates = dead_lettered = 0
        chats = self._state.recent_chats()
        self._log_sweep_started(len(chats))
        for chat_id in chats:
            try:
                batch = self._lark.list_chat_messages(
                    chat_id,
                    start_time=start_time,
                    chat_type=self._state.chat_type(chat_id),
                )
            except (NameError, ImportError):
                raise
            except LarkApiError as error:
                # The platform already told us *why*, in fields: a permission
                # rejection arrives with the scopes it wants. "Never copy SDK
                # error text" (below) forbids the free-text message; it never
                # asked us to discard the structured fields, and discarding
                # them is what left the caller unable to tell a failure that
                # will clear from one that never can.
                if error.missing_scopes:
                    # A missing scope is the ONE permanent-looking failure a
                    # human can clear -- by granting it.  So it is named and
                    # reported, but the chat stays in the scan set: retiring it
                    # would make the eventual grant invisible forever.
                    blocked.append(
                        f"{chat_id}: missing-scope "
                        + "+".join(sorted(error.missing_scopes))
                    )
                    self._log_blocked_chat(chat_id, error)
                    continue
                if error.code in _UNREADABLE_CHAT_CODES:
                    # Same bucket as a missing scope, for the same reason: the
                    # sweep will fail identically every time until a human acts,
                    # so failing the probe on it is a restart loop, not a gate.
                    # "Until a human acts" is also why it stays IN the scan set:
                    # the code names the permission a human can grant.
                    blocked.append(f"{chat_id}: unreadable {error.code}")
                    self._log_blocked_chat(chat_id, error)
                    continue
                if error.code in LARK_HISTORY_CODES_PERMANENT_CHAT_GONE:
                    # The one permanent class: nothing a human does brings this
                    # chat back (the bot is no longer a member), so it leaves the
                    # scan set as well as the fail-closed column.
                    blocked.append(f"{chat_id}: unreadable {error.code}")
                    self._log_blocked_chat(chat_id, error)
                    self._retire_chat(chat_id, code=error.code)
                    retired.append(chat_id)
                    continue
                # Still fails closed: an unrecognised code may well be
                # transient, and a sweep that might have missed messages must
                # not report health.  But say which code it was, or the only
                # way to learn that this one is permanent is to watch an
                # adapter quarantine itself and have nothing name the cause.
                self._log_unreadable_chat(chat_id, error.code)
                errors.append(f"{chat_id}: {_reconcile_error_category(error)}")
                continue
            except Exception as error:  # noqa: BLE001 - per-chat isolation
                # A scope/permission/rate failure for one chat must not
                # starve the others. Never copy SDK error text into state or
                # telemetry because it can contain URLs or access tokens.
                #
                # No retirement here, deliberately: a permanent-chat code is
                # only ever carried by a ``LarkApiError``, and every one of
                # those is caught by the handler above -- so a retirement path
                # in this branch would be unreachable code.  This branch is for
                # transport and shape failures, which are never permanent.
                self._log_unreadable_chat(chat_id, None)
                errors.append(f"{chat_id}: {_reconcile_error_category(error)}")
                continue
            if not getattr(batch, "complete", True):
                errors.append(f"{chat_id}: history-incomplete")
            for message in batch:
                if _create_time_ms(message.create_time) < window_start_ms:
                    continue
                scanned += 1
                outcome = self.handle_inbound(message, suppress_guidance=True)
                if outcome.status == "forwarded":
                    forwarded += 1
                elif outcome.status == "duplicate":
                    duplicates += 1
                elif outcome.new_dead_letter:
                    dead_lettered += 1
                elif outcome.status in {
                    "no-target",
                    "route-unavailable",
                    "error",
                    "unsupported-message-type",
                    "invalid-message-content",
                }:
                    # The audit record already existed before this sweep. It
                    # remains operator-visible, but is not a newly missed
                    # websocket message and must not churn the subscription.
                    pass
                elif outcome.status not in {"ignored"}:
                    errors.append(f"{message.message_id}: {outcome.status}")
        return ReconcileReport(
            chats_scanned=len(chats),
            messages_scanned=scanned,
            forwarded=forwarded,
            duplicates=duplicates,
            dead_lettered=dead_lettered,
            retryable_errors=tuple(errors),
            blocked_chats=tuple(blocked),
            retired_chats=tuple(retired),
        )

    def _log_sweep_started(self, chat_count: int) -> None:
        """Mark that the sweep reached the loop, and over how many chats.

        This is the phase half of the failure telemetry: the emit that names
        the exception cannot compute the phase where it fires (the exception
        has already left ``reconcile_recent``), so the phase is read off this
        marker's presence instead.  ``chatCount`` also answers the open
        question of how many chats this adapter's scan set actually holds.
        """

        if self._logger is None:
            return
        try:
            self._logger.info(
                "adapter.reconcile.sweep_started",
                chatCount=chat_count,
                node="adapter-reconcile",
            )
        except Exception:  # noqa: BLE001 - telemetry must not break a sweep
            pass

    def _log_blocked_chat(self, chat_id: str, error: LarkApiError) -> None:
        """Say which chat is unreadable and what would make it readable.

        Skipping silently would leave a permanently unreconciled chat looking
        exactly like a working one: the adapter reports healthy and nothing
        ever names the gap.  Only structured fields go out -- the scope names
        and the platform code -- never the SDK message, which can carry URLs
        or tokens.
        """

        if self._logger is None:
            return
        try:
            self._logger.warn(
                "adapter.reconcile.chat_blocked",
                chatId=chat_id,
                missingScopes=list(error.missing_scopes),
                platformCode=error.code,
                node="adapter-reconcile",
            )
        except Exception:  # noqa: BLE001 - telemetry must not break a sweep
            pass

    def _log_unreadable_chat(self, chat_id: str, code: int | None) -> None:
        """Name the code behind a failure this sweep could not classify.

        These still fail the probe, so a run of them ends in a supervised
        rebuild and then quarantine.  Without this line that outcome carries
        no cause at all: the adapter dies, and the one fact needed to decide
        whether the code belongs in :data:`_UNREADABLE_CHAT_CODES` is the one
        fact nothing recorded.  Only the platform code goes out, never the SDK
        message, which can carry URLs or tokens.
        """

        if self._logger is None:
            return
        try:
            self._logger.warn(
                "adapter.reconcile.chat_unreadable",
                chatId=chat_id,
                platformCode=code,
                node="adapter-reconcile",
            )
        except Exception:  # noqa: BLE001 - telemetry must not break a sweep
            pass

    def dead_letters(self) -> tuple[DeadLetter, ...]:
        """The preserved bodies of inbound messages that never delivered."""

        return self._state.dead_letters()

    def _with_known_chat_type(self, message: LarkInboundMessage) -> LarkInboundMessage:
        if message.chat_type != "unknown":
            return message
        known = self._state.chat_type(message.chat_id)
        return replace(message, chat_type=known) if known else message

    def _record_dead_letter(
        self,
        message: LarkInboundMessage,
        *,
        reason: str,
        detail: str | None = None,
    ) -> bool:
        try:
            now = self._utcnow().astimezone(UTC)
            return self._persist_dead_letter(
                DeadLetter(
                    message_id=message.message_id,
                    event_id=message.event_id,
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
                    conversation_id=message.conversation_id,
                    sender_id=message.sender_id,
                    sender_type=message.sender_type,
                    text=message.text,
                    reason=reason,
                    detail=detail,
                    created_at=now.isoformat(timespec="seconds"),
                    create_time=message.create_time,
                    reply_to=message.reply_to,
                    message_type=message.message_type,
                ),
                now=now,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - the audit trail must never break inbound
            return False

    def _dead_letter_unparsed(self, event: Any, error: Exception) -> None:
        """Preserve what we can of an event that failed normalization."""

        try:
            raw_event = getattr(event, "event", None)
            raw_message = getattr(raw_event, "message", None)
            raw_sender = getattr(raw_event, "sender", None)
            message_id = getattr(raw_message, "message_id", None)
            if not message_id:
                return  # nothing stable to key the record on
            header = getattr(event, "header", None)
            chat_id = getattr(raw_message, "chat_id", None) or ""
            safe_message_type = canonical_lark_message_type(
                getattr(raw_message, "message_type", None)
            )
            now = self._utcnow().astimezone(UTC)
            self._persist_dead_letter(
                DeadLetter(
                    message_id=message_id,
                    event_id=getattr(header, "event_id", None) or "",
                    chat_id=chat_id,
                    chat_type=getattr(raw_message, "chat_type", None) or "unknown",
                    conversation_id=(
                        f"lark:{chat_id}:{chat_id}" if chat_id else ""
                    ),
                    sender_id=(
                        getattr(
                            getattr(raw_sender, "sender_id", None), "open_id", None
                        )
                        or "unknown"
                    ),
                    sender_type=getattr(raw_sender, "sender_type", None) or "unknown",
                    text=f"[Unreadable Lark {safe_message_type} message]",
                    reason="normalize-error",
                    detail=type(error).__name__,
                    created_at=now.isoformat(timespec="seconds"),
                    create_time=getattr(raw_message, "create_time", None),
                    reply_to=getattr(raw_message, "parent_id", None),
                    message_type=safe_message_type,
                ),
                now=now,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - the audit trail must never break inbound
            pass

    def _persist_dead_letter(self, letter: DeadLetter, *, now: datetime) -> bool:
        """Persist once, then expose references without ever exposing the body."""

        created = self._state.record_dead_letter(letter)
        if not created:
            return False
        if self._logger is not None:
            try:
                self._logger.log(
                    "warn",
                    "dead-letter",
                    reason=letter.reason,
                    message_id=letter.message_id,
                    chat_id=letter.chat_id,
                    chat_type=letter.chat_type,
                    conversation_id=letter.conversation_id,
                )
            except (NameError, ImportError):
                raise
            except Exception:  # noqa: BLE001,S110 - visibility is best effort
                pass
        if self._alarm is not None:
            self._alarm.emit(
                Alarm(
                    correlation_id=letter.message_id,
                    message_id=letter.message_id,
                    conversation_id=letter.conversation_id,
                    sender=self._channel_actor_id,
                    recipient="lark-native-conversation",
                    reason=letter.reason,
                    audience="human",
                )
            )
        self._maybe_notify_dead_letter_backlog(now=now)
        return True

    def _deliver_native_alarm(self, alarm: Alarm, text: str) -> bool:
        self._lark.reply(
            alarm.message_id,
            text,
            idempotency_key=f"alarm:{alarm.message_id}:{alarm.reason}",
        )
        return True

    def _maybe_notify_dead_letter_backlog(self, *, now: datetime) -> None:
        notifier = self._notify_operator
        if notifier is None:
            return
        window_epoch = (
            int(now.timestamp()) // DEAD_LETTER_ALERT_WINDOW_SECONDS
        ) * DEAD_LETTER_ALERT_WINDOW_SECONDS
        window_start = datetime.fromtimestamp(window_epoch, UTC).isoformat(
            timespec="seconds"
        )
        try:
            count, reasons = self._state.dead_letter_summary(since=window_start)
            if count < self._dead_letter_alert_threshold:
                return
            claimed = self._state.claim_dead_letter_alert(
                window_start=window_start,
                claimed_at=now.isoformat(timespec="seconds"),
            )
            if not claimed:
                return
            reason_summary = ",".join(
                f"{reason}:{reason_count}"
                for reason, reason_count in sorted(reasons.items())
            )
            text = f"Dead-letter backlog: count={count}; reasons={reason_summary}"
            correlation_id = (
                f"dead-letter-backlog:{self._adapter_name}:{window_epoch}"
            )
            if self._alarm is None:
                sent = notifier(text, correlation_id)
            else:
                outcome = self._alarm.emit(
                    Alarm(
                        correlation_id=correlation_id,
                        message_id=correlation_id,
                        conversation_id=f"operator:{self._adapter_name}",
                        sender=self._channel_actor_id,
                        recipient="operator-route",
                        reason="dead-letter-backlog",
                        audience="operator",
                    ),
                    delivery=lambda _alarm, _rendered: notifier(
                        text, correlation_id
                    ),
                    terminal=False,
                    throttle=False,
                )
                sent = outcome.status == "delivered"
            if not sent:
                self._state.release_dead_letter_alert(window_start=window_start)
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - operator notification is best effort
            try:
                self._state.release_dead_letter_alert(window_start=window_start)
            except (NameError, ImportError):
                raise
            except Exception:  # noqa: BLE001,S110 - retain original boundary
                pass

    def _is_bot_mentioned(self, message: LarkInboundMessage) -> bool:
        """True when THIS app's bot is @-mentioned, judged by identity key.

        Compares the mention open_ids against the app's own bot open_id --
        never display names, which are mutable and collide (the TS-era
        route-name matching silently dropped a production group mention for
        exactly that reason).  When the platform cannot answer the bot's
        identity the message counts as not mentioned: group traffic keeps
        its "ignored" default instead of being misrouted on a guess.
        """

        if not message.mention_open_ids:
            return False
        try:
            bot = self._lark.bot_open_id()
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - platform boundary
            return False
        return bot is not None and bot in message.mention_open_ids

    def _route(self, message: LarkInboundMessage) -> tuple[ActorTarget | None, str]:
        if message.reply_to:
            reply_route = self._state.reply(message.reply_to)
            if reply_route is not None:
                actor = self._routes.actor_by_key(reply_route.actor_key)
                if actor is None:
                    return None, "route-unavailable"
                return actor, "native-reply"

        # An @ of this bot goes to the adapter's pinned agent, whatever the
        # chat: the adapter binds one agent, so addressing the bot IS
        # addressing that agent.  Mentions of anyone else fall through --
        # in a group that means "ignored", the decided default for group
        # traffic that never addressed us.
        if self._is_bot_mentioned(message):
            actor = self._routes.pinned_actor(message.conversation_id)
            if actor is not None:
                return actor, "bot-mention"
            return None, "no-target"

        if message.chat_type == "unknown":
            # Recovery paths could not establish the chat type (the REST
            # models carry none and this chat never produced a live event).
            # Do not guess "p2p" (that would misroute group traffic to a
            # pin), but do not silently drop it either: dead-letter it so
            # the body stays recoverable once the chat type is known.
            return None, "no-target"
        if message.chat_type == "p2p":
            actor = self._routes.pinned_actor(message.conversation_id)
            if actor is not None:
                return actor, "pin-fallback" if message.reply_to else "pin"
            return None, "no-target"
        return None, "ignored"

    def _route_guidance(
        self, message: LarkInboundMessage, route_status: str
    ) -> str | None:
        pin_command = f"hyprial adapter pin {self._adapter_name} <agent>"
        if route_status == "route-unavailable":
            return (
                "The agent associated with this reply is offline or unavailable. "
                "Your message was not rerouted."
            )
        if route_status != "no-target":
            return None
        if message.reply_to:
            return (
                "The quoted message does not resolve to an addressable Harness "
                "agent, and this adapter has no pinned agent. An operator can "
                f"bind one on the daemon machine with: {pin_command}"
            )
        return (
            "No agent is bound to this adapter. An operator can bind one on "
            f"the daemon machine with: {pin_command}"
        )

    def _fallback_quote_context(
        self,
        message: LarkInboundMessage,
        route_status: str,
    ) -> str:
        if route_status != "pin-fallback" or not message.reply_to:
            return message.text
        try:
            quote = self._lark.get_message(message.reply_to)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            del error
            return self._unresolved_quote(message)
        if quote is None:
            return self._unresolved_quote(message)
        return (
            f"{message.text}\n\n"
            f"[Quoted message {quote.message_id} "
            f"(from {quote.sender_id}, {quote.message_type})]\n"
            f"{quote.text}"
        )

    @staticmethod
    def _unresolved_quote(message: LarkInboundMessage) -> str:
        return (
            f"{message.text}\n\n"
            f"[Quoted message {message.reply_to} (unavailable)]\n"
            "Quoted context could not be resolved."
        )

    def handle_delivery(
        self, delivery: HarnessDelivery, *, settle: bool = True
    ) -> DeliveryOutcome:
        """Send one correlated native reply.

        Normal adapter delivery uses ``settle=True`` and reports acceptance or
        rejection through the supplied Harness port.  The daemon reply bridge
        uses ``settle=False`` because its durable outbox owns settlement: the
        worker's positive control-socket response is the receipt that removes
        that row and writes the terminal record.  Calling back into the daemon
        while it waits for the control response would deadlock the serialized
        IPC transaction and would also settle the wrong inbox leg.
        """

        correlation = self._state.request(delivery.reply_to)
        if correlation is None:
            error = f"no Lark correlation for {delivery.reply_to}"
            if settle:
                self._harness.reject_delivery(
                    delivery.delivery_id, error, deterministic=True
                )
            return DeliveryOutcome(status="rejected", error=error)

        try:
            native_message_id = self._lark.reply(
                correlation.message_id,
                self._render_reply(delivery, native_reply_to=correlation.message_id),
                idempotency_key=(
                    f"{correlation.message_id}:delivery:{delivery.message_id}"
                ),
            )
            self._state.record_reply(
                ReplyRoute(
                    message_id=native_message_id,
                    actor_id=delivery.from_actor.actor_id,
                    actor_key=delivery.from_actor.actor_key,
                    harness_message_id=delivery.reply_to,
                    conversation_id=correlation.conversation_id,
                    chat_id=correlation.chat_id,
                )
            )
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform/state boundary
            if settle:
                self._harness.reject_delivery(
                    delivery.delivery_id, str(error), deterministic=False
                )
            return DeliveryOutcome(status="rejected", error=str(error))

        if settle:
            self._harness.accept_delivery(delivery.delivery_id, native_message_id)
        cleanup_error = (
            self._clear_ack_reaction(correlation.message_id) if settle else None
        )
        return DeliveryOutcome(
            status="accepted",
            native_message_id=native_message_id,
            cleanup_error=cleanup_error,
        )

    def clear_delivery_reaction(self, reply_to: str) -> str | None:
        """Best-effort cleanup after the reply-bridge receipt is emitted."""

        correlation = self._state.request(reply_to)
        if correlation is None:
            return f"no Lark correlation for {reply_to}"
        return self._clear_ack_reaction(correlation.message_id)

    def persist_delivery_reaction(self, reply_to: str) -> str | None:
        """Durably queue reply-wins cleanup before publishing its receipt."""

        if self._reaction_effects is None:
            return "reaction-effect-ledger-unavailable"
        correlation = self._state.request(reply_to)
        if correlation is None:
            return f"no Lark correlation for {reply_to}"
        admission = self._reaction_effects.persist_reply(correlation.message_id)
        return (
            None
            if admission is ReactionEffectAdmission.ACCEPTED
            else f"reaction-effect-{admission.value}"
        )

    def _clear_ack_reaction(self, native_message_id: str) -> str | None:
        if self._reaction_effects is not None:
            admission = self._reaction_effects.reply(native_message_id)
            return (
                None
                if admission is ReactionEffectAdmission.ACCEPTED
                else f"reaction-effect-{admission.value}"
            )
        try:
            with self._reaction_lock:
                self._reaction_lark.clear_reaction(native_message_id, ACK_EMOJI)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            # Delivery is already accepted; never redeliver for cleanup.
            return str(error)
        return None

    def handle_alarm_delivery(
        self, correlation_id: str, text: str, *, idempotency_key: str
    ) -> DeliveryOutcome:
        """Reply to the original Lark message without creating a reply leg."""

        correlation = self._state.request(correlation_id)
        if correlation is None:
            return DeliveryOutcome(
                status="rejected",
                error=f"no Lark correlation for {correlation_id}",
            )
        try:
            native_message_id = self._lark.reply(
                correlation.message_id,
                text,
                idempotency_key=idempotency_key,
            )
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - platform boundary
            return DeliveryOutcome(status="rejected", error=str(error))
        return DeliveryOutcome(
            status="accepted", native_message_id=native_message_id
        )

    @staticmethod
    def _render_reply(delivery: HarnessDelivery, *, native_reply_to: str) -> str:
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        return (
            f"Agent: {delivery.from_actor.display_name}\n"
            f"Replied-At: {timestamp}\n"
            f"Reply-To: {native_reply_to}\n\n"
            f"{delivery.text}"
        )
