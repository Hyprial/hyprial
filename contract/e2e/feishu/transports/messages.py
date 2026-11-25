"""E2E-004 transports / messages; explicit callbacks keep entrypoint seams live."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from feishu.content import _user_content_args, find_first_string
from feishu.evidence import ContractFailure


@dataclass(frozen=True)
class TransportsMessagesPorts:
    """Only this module's observed collaborators and configuration."""

    POLL_SECONDS: float
    REACTION_TIMEOUT_SECONDS: float
    lark_cli: Callable[..., Any]
    get_onit_reactions: Callable[..., Any]


def user_send(
    chat_id: str,
    text: str,
    idempotency_key: str,
    *,
    mention: str | None = None,
    _ports: TransportsMessagesPorts,
) -> str:
    value = _ports.lark_cli(
        "im",
        "+messages-send",
        "--as",
        "user",
        "--chat-id",
        chat_id,
        *_user_content_args(text, mention),
        "--idempotency-key",
        idempotency_key,
        "--json",
    )
    message_id = find_first_string(value.get("data", value), "message_id")
    if message_id is None:
        raise ContractFailure("user send returned no message_id")
    return message_id


def user_reply(
    message_id: str,
    text: str,
    idempotency_key: str,
    *,
    mention: str | None = None,
    _ports: TransportsMessagesPorts,
) -> str:
    value = _ports.lark_cli(
        "im",
        "+messages-reply",
        "--as",
        "user",
        "--message-id",
        message_id,
        *_user_content_args(text, mention),
        "--idempotency-key",
        idempotency_key,
        "--json",
    )
    native_message_id = find_first_string(value.get("data", value), "message_id")
    if native_message_id is None:
        raise ContractFailure("user reply returned no message_id")
    return native_message_id


def user_send_content(
    chat_id: str,
    message_type: str,
    content: Mapping[str, Any],
    idempotency_key: str,
    *,
    _ports: TransportsMessagesPorts,
) -> str:
    value = _ports.lark_cli(
        "im",
        "+messages-send",
        "--as",
        "user",
        "--chat-id",
        chat_id,
        "--msg-type",
        message_type,
        "--content",
        json.dumps(content, ensure_ascii=False),
        "--idempotency-key",
        idempotency_key,
        "--json",
    )
    message_id = find_first_string(value.get("data", value), "message_id")
    if message_id is None:
        raise ContractFailure(f"user {message_type} send returned no message_id")
    return message_id


def user_send_attachment(
    chat_id: str,
    flag: str,
    path: Path,
    idempotency_key: str,
    *,
    _ports: TransportsMessagesPorts,
) -> str:
    """Send an image/file message; lark-cli only accepts cwd-relative paths."""

    value = _ports.lark_cli(
        "im",
        "+messages-send",
        "--as",
        "user",
        "--chat-id",
        chat_id,
        flag,
        path.name,
        "--idempotency-key",
        idempotency_key,
        "--json",
        cwd=path.parent,
    )
    message_id = find_first_string(value.get("data", value), "message_id")
    if message_id is None:
        raise ContractFailure(f"user {flag} send returned no message_id")
    return message_id


def get_message(message_id: str, *, _ports: TransportsMessagesPorts) -> dict[str, Any]:
    value = _ports.lark_cli(
        "im",
        "+messages-mget",
        "--as",
        "user",
        "--message-ids",
        message_id,
        "--no-reactions",
        "--json",
    )
    data = value.get("data")
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or len(messages) != 1:
        raise ContractFailure(
            f"message lookup did not return exactly one item: {message_id}"
        )
    message = messages[0]
    if not isinstance(message, dict):
        raise ContractFailure("message lookup item must be an object")
    return message


def get_onit_reactions(
    message_id: str, *, _ports: TransportsMessagesPorts
) -> list[dict[str, Any]]:
    value = _ports.lark_cli(
        "im",
        "reactions",
        "list",
        "--as",
        "user",
        "--message-id",
        message_id,
        "--reaction-type",
        "OnIt",
        "--user-id-type",
        "open_id",
        "--page-size",
        "50",
        "--json",
    )
    data = value.get("data")
    items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        raise ContractFailure("reaction list items must be an array")
    return [item for item in items if isinstance(item, dict)]


def wait_for_onit(
    message_id: str, *, _ports: TransportsMessagesPorts
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + _ports.REACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reactions = _ports.get_onit_reactions(message_id)
        if reactions:
            return reactions
        time.sleep(_ports.POLL_SECONDS)
    raise ContractFailure(f"OnIt did not appear on {message_id}")


def wait_for_no_onit(message_id: str, *, _ports: TransportsMessagesPorts) -> None:
    deadline = time.monotonic() + _ports.REACTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _ports.get_onit_reactions(message_id):
            return
        time.sleep(_ports.POLL_SECONDS)
    raise ContractFailure(f"OnIt did not clear from {message_id}")
