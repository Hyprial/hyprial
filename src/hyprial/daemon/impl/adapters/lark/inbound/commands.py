"""Lark event conversion, routing, acknowledgement, and receipt correlation."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from hyprial.daemon.impl.adapters.lark.contracts.messages import (
    MAX_LARK_TEXT_CONTENT_BYTES,
)
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    MAX_RUNTIME_TARGET_ITEM_BYTES,
    AgentDirectoryEntry,
    InboundOutcome,
    LarkInboundMessage,
    RouteDirectory,
    encode_lark_text_content,
    is_safe_runtime_target_field,
)
from hyprial.daemon.impl.adapters.lark.state.records import (
    PendingCommandResponse,
)
_FIXED_OFFSET = re.compile(r"UTC([+-])(\d{2}):(\d{2})(?::(\d{2}))?")
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

class _CommandResponseMixin:
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
