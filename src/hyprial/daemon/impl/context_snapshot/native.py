"""Bounded native transcript parsers for Claude, Pi, and Codex."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hyprial.kernel import redact, redact_turn_excerpt

from hyprial.daemon.impl.context_snapshot.contract import (
    ContextSnapshotRequest,
    SnapshotGap,
    SnapshotSourceEvidence,
    SourceAnalysis,
)
from hyprial.daemon.impl.context_snapshot.io import read_bounded_lines, relative_ref
from hyprial.daemon.impl.transfer.archive.session_files import (
    SessionFileError,
    locate_session_file,
)


@dataclass(frozen=True, slots=True)
class _Item:
    text: str
    call_id: str | None = None
    kind: str = "message"


def _runtime_context(home: Path, harness: str) -> object:
    root = home / "state" / ("pi" if harness == "pi" else f"sessions/{harness}")
    return SimpleNamespace(harness=harness, roots=SimpleNamespace(session_root=root))


def _text(value: object) -> str:
    safe = redact(value)
    if isinstance(safe, str):
        rendered = safe
    else:
        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
    return redact_turn_excerpt(rendered, enforce_output_bound=False)


def _content_items(role: str, content: object) -> list[_Item]:
    if isinstance(content, str):
        return [_Item(f"[{role}] {_text(content)}")]
    if not isinstance(content, list):
        return []
    items: list[_Item] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"text", "input_text", "output_text"}:
            value = block.get("text")
            if isinstance(value, str):
                items.append(_Item(f"[{role}] {_text(value)}"))
        elif kind == "tool_use":
            call_id = str(block.get("id") or "unknown")
            items.append(
                _Item(
                    f"[assistant tool-use id={call_id} name={_text(block.get('name', 'unknown'))}] "
                    f"{_text(block.get('input', {}))}",
                    call_id,
                    "call",
                )
            )
        elif kind == "tool_result":
            call_id = str(block.get("tool_use_id") or "unknown")
            items.append(
                _Item(
                    f"[tool-result id={call_id}] {_text(block.get('content', ''))}",
                    call_id,
                    "result",
                )
            )
    return items


def _claude(value: dict[str, Any]) -> list[_Item]:
    if value.get("type") not in {"user", "assistant"}:
        return []
    message = value.get("message")
    if not isinstance(message, dict):
        return []
    role = str(message.get("role") or value["type"])
    return _content_items(role, message.get("content"))


def _pi(value: dict[str, Any]) -> list[_Item]:
    kind = value.get("type")
    if kind == "message":
        message = value.get("message")
        if isinstance(message, dict):
            return _content_items(str(message.get("role") or "message"), message.get("content"))
    if kind in {"toolResult", "tool_result"}:
        call_id = str(value.get("toolCallId") or value.get("tool_call_id") or "unknown")
        return [
            _Item(
                f"[tool-result id={call_id}] {_text(value.get('content', ''))}",
                call_id,
                "result",
            )
        ]
    if kind in {"toolCall", "tool_call"}:
        call_id = str(value.get("toolCallId") or value.get("tool_call_id") or "unknown")
        return [
            _Item(
                f"[assistant tool-use id={call_id} name={_text(value.get('name', 'unknown'))}] "
                f"{_text(value.get('arguments', value.get('input', {})))}",
                call_id,
                "call",
            )
        ]
    return []


def _codex(value: dict[str, Any]) -> list[_Item]:
    if value.get("type") not in {"response_item", "event_msg"}:
        return []
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return []
    kind = payload.get("type")
    if kind == "message":
        return _content_items(str(payload.get("role") or "message"), payload.get("content"))
    if kind in {"user_message", "agent_message"}:
        message = payload.get("message")
        return [_Item(f"[{kind}] {_text(message)}")] if isinstance(message, str) else []
    if kind == "function_call":
        call_id = str(payload.get("call_id") or "unknown")
        return [
            _Item(
                f"[assistant tool-use id={call_id} name={_text(payload.get('name', 'unknown'))}] "
                f"{_text(payload.get('arguments', ''))}",
                call_id,
                "call",
            )
        ]
    if kind == "function_call_output":
        call_id = str(payload.get("call_id") or "unknown")
        return [
            _Item(
                f"[tool-result id={call_id}] {_text(payload.get('output', ''))}",
                call_id,
                "result",
            )
        ]
    return []


def _unknown_version(harness: str, values: list[dict[str, Any]]) -> bool:
    if harness == "pi":
        headers = [value for value in values if value.get("type") == "session"]
        return any(value.get("version", 1) != 1 for value in headers)
    if harness == "codex":
        headers = [value.get("payload") for value in values if value.get("type") == "session_meta"]
        return any(
            isinstance(value, dict) and value.get("version", 1) != 1
            for value in headers
        )
    return any(value.get("version", 1) != 1 for value in values if "version" in value)


def _select_items(items: list[_Item], char_limit: int) -> tuple[tuple[str, ...], bool]:
    units: list[list[_Item]] = []
    call_units: dict[str, list[_Item]] = {}
    for item in items:
        if item.call_id is None:
            units.append([item])
            continue
        unit = call_units.get(item.call_id)
        if unit is None:
            unit = []
            call_units[item.call_id] = unit
            units.append(unit)
        unit.append(item)
    eligible: list[list[_Item]] = []
    omitted = False
    for unit in units:
        kinds = {item.kind for item in unit}
        if "result" in kinds and "call" not in kinds:
            omitted = True
            continue
        eligible.append(unit)
    selected: list[list[_Item]] = []
    used = 0
    for unit in reversed(eligible):
        text = "\n".join(item.text for item in unit)
        cost = len(text) + 1
        if used + cost > char_limit:
            continue
        selected.append(unit)
        used += cost
    selected.reverse()
    flattened = tuple(item.text for unit in selected for item in unit)
    return flattened, omitted or len(selected) != len(eligible)


def analyze_native_session(home: Path, request: ContextSnapshotRequest) -> SourceAnalysis:
    source = request.source
    try:
        path = locate_session_file(
            source.harness,
            source.cwd,
            source.session_ref,
            home=home,
            runtime_context=_runtime_context(home, source.harness),
        )
    except SessionFileError as error:
        return SourceAnalysis(
            coverage="unavailable",
            gaps=(
                SnapshotGap(
                    "native-session-unavailable",
                    f"native session locator refused the frozen ref: {type(error).__name__}",
                ),
            ),
        )
    bounded = read_bounded_lines(home, path, request.session_read_limit_bytes)
    ref = relative_ref(home, path)
    evidence = SnapshotSourceEvidence(
        kind="native-session",
        ref=ref,
        digest=bounded.digest,
        captured_bytes=bounded.captured_bytes,
        source_bytes=bounded.source_bytes,
    )
    gaps: list[SnapshotGap] = []
    if bounded.byte_truncated:
        gaps.append(
            SnapshotGap(
                "native-session-byte-limit",
                f"bytes [0,{bounded.start_offset}) were excluded by the read limit",
                ref,
            )
        )
    if bounded.torn_final_line or bounded.invalid_utf8:
        gaps.append(
            SnapshotGap(
                "native-session-torn-record",
                "an incomplete or invalid UTF-8 native event was excluded",
                ref,
            )
        )
    values: list[dict[str, Any]] = []
    for line in bounded.lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            gaps.append(
                SnapshotGap(
                    "native-session-torn-record",
                    "an invalid JSON native event was excluded",
                    ref,
                )
            )
            continue
        if isinstance(value, dict):
            values.append(value)
    if _unknown_version(source.harness, values):
        gaps.append(
            SnapshotGap(
                "native-format-unsupported",
                "the native session declares an unsupported format version",
                ref,
            )
        )
        return SourceAnalysis(
            coverage="partial",
            sources=(evidence,),
            gaps=tuple(gaps),
        )

    parser = {"claude": _claude, "pi": _pi, "codex": _codex}[source.harness]
    items = [item for value in values for item in parser(value)]
    excerpts, token_truncated = _select_items(
        items, request.injection_token_limit * 4
    )
    if token_truncated:
        gaps.append(
            SnapshotGap(
                "native-session-token-limit",
                "one or more complete message/tool association units were excluded",
                ref,
            )
        )
    if not excerpts:
        gaps.append(
            SnapshotGap(
                "native-session-no-messages",
                "no supported complete message or tool-result event was captured",
                ref,
            )
        )
    # A native tail cannot prove settlement-watermark coverage. It is a
    # conservative fallback only; recap alignment is the sole complete proof.
    return SourceAnalysis(
        coverage="partial" if excerpts else "unavailable",
        sources=(evidence,),
        gaps=tuple(gaps),
        excerpts=excerpts,
    )


__all__ = ["analyze_native_session"]
