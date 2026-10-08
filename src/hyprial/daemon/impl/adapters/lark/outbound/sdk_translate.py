"""Official ``lark-oapi`` SDK boundary used by the adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from hyprial.daemon.impl.adapters.lark.contracts.messages import (
    LarkForwardedItem,
    NormalizedLarkContent,
    normalize_lark_message_content,
    normalize_merge_forward,
)
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    LarkInboundMessage,
)

if TYPE_CHECKING:
    from hyprial.kernel import Logger


def _log_unparsed_post(
    logger: Logger | None,
    *,
    native_message_id: object,
    normalized: NormalizedLarkContent,
) -> None:
    if logger is None:
        return
    for shape in normalized.unparsed_post_shapes:
        logger.log(
            "warn",
            "lark.post.attachment_unparsed",
            nativeMessageId=native_message_id,
            shape={"tags": list(shape.tags), "keys": list(shape.keys)},
        )

def _permission_payload(response: Any) -> dict[str, Any]:
    error = getattr(response, "error", None)

    def field(value: object, name: str) -> object:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    violations = field(error, "permission_violations")
    normalized: list[dict[str, Any]] = []
    for item in (violations if isinstance(violations, (list, tuple)) else ()):
        normalized.append({"subject": field(item, "subject")})
    raw_helps = field(error, "helps")
    helps = [
        {"url": field(item, "url")}
        for item in (raw_helps if isinstance(raw_helps, (list, tuple)) else ())
    ]
    return {
        "code": getattr(response, "code", None),
        "error": {
            "permission_violations": normalized,
            "helps": helps,
        },
    }


def _rest_message_type(message: Any) -> Any:
    # The REST models name the field ``msg_type``; event payloads use
    # ``message_type``.  Accept either so fakes and SDK generations both work.
    return getattr(message, "msg_type", None) or getattr(
        message, "message_type", None
    )


def _forwarded_items(items: Any) -> tuple[LarkForwardedItem, ...]:
    """Project an ``im.v1.message.get`` response onto the neutral item shape."""

    collected: list[LarkForwardedItem] = []
    for item in items or ():
        message_id = getattr(item, "message_id", None)
        body = getattr(item, "body", None)
        if not isinstance(message_id, str) or not message_id or body is None:
            continue
        sender = getattr(item, "sender", None)
        sender_id = getattr(sender, "id", None)
        sender_type = getattr(sender, "sender_type", None)
        collected.append(
            LarkForwardedItem(
                message_id=message_id,
                # ``canonical_lark_message_type`` closes this set downstream;
                # an unknown child becomes an explicit unsupported line.
                message_type=str(_rest_message_type(item) or ""),
                content=getattr(body, "content", None),
                upper_message_id=getattr(item, "upper_message_id", None),
                sender_id=(
                    sender_id
                    if isinstance(sender_id, str) and sender_id
                    else None
                ),
                sender_type=(
                    sender_type
                    if isinstance(sender_type, str) and sender_type
                    else None
                ),
            )
        )
    return tuple(collected)


def _normalize_rest_body(
    message: Any,
    items: Any = (),
    *,
    resolve_sender: Callable[[str], str | None] | None = None,
    logger: Logger | None = None,
) -> NormalizedLarkContent:
    """Normalize one REST message, expanding a merge-forward when possible.

    ``im.v1.message.get`` returns the forwarded conversation in the *same*
    response as the shell, so expansion here costs no extra platform call.
    Callers without that list (``im.v1.message.list``) get the label.
    """

    message_type = _rest_message_type(message)
    body = getattr(message, "body", None)
    content = getattr(body, "content", None)
    message_id = getattr(message, "message_id", None)
    if message_type == "merge_forward":
        forwarded = _forwarded_items(items)
        if forwarded:
            return normalize_merge_forward(
                message.message_id, forwarded, resolve_sender=resolve_sender
            )
    normalized = normalize_lark_message_content(
        message_type,
        content,
        message_id=message_id if isinstance(message_id, str) else None,
    )
    _log_unparsed_post(
        logger, native_message_id=message_id, normalized=normalized
    )
    return normalized


def _inbound_from_rest_message(
    message: Any,
    *,
    event_id: str,
    chat_type: str | None,
    items: Any = (),
    resolve_sender: Callable[[str], str | None] | None = None,
    logger: Logger | None = None,
) -> LarkInboundMessage | None:
    """Normalize a REST ``Message`` model (get/list) into the inbound shape.

    Returns ``None`` only for non-user senders or missing routing fields.
    Message bodies share the live-event normalizer, including explicit
    unsupported/invalid outcomes, so history never silently drops a type.
    """

    sender = message.sender
    body = message.body
    if not message.message_id or not message.chat_id or sender is None or body is None:
        return None
    if sender.sender_type != "user":
        return None
    normalized = _normalize_rest_body(
        message, items, resolve_sender=resolve_sender, logger=logger
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
        # REST ``Mention`` carries ``id`` as a plain string qualified by
        # ``id_type``; only an open_id is comparable to the bot's identity.
        # Tolerate an event-shaped ``id`` object too, so fakes and future SDK
        # generations that reuse the UserId model keep working.
        identity = getattr(mention, "id", None)
        id_type = getattr(mention, "id_type", None)
        nested_open_id = getattr(identity, "open_id", None)
        if isinstance(nested_open_id, str) and nested_open_id:
            mention_open_ids.append(nested_open_id)
        elif (
            isinstance(identity, str)
            and identity
            and (id_type is None or id_type == "open_id")
        ):
            mention_open_ids.append(identity)
    create_time = message.create_time
    return LarkInboundMessage(
        event_id=event_id,
        message_id=message.message_id,
        chat_id=message.chat_id,
        chat_type=chat_type or "unknown",
        text=text.strip(),
        sender_id=sender.id or "unknown",
        sender_type=sender.sender_type,
        root_id=message.root_id,
        thread_id=getattr(message, "thread_id", None),
        reply_to=message.parent_id,
        create_time=str(create_time) if create_time is not None else None,
        message_type=normalized.message_type,
        content_status=normalized.status,
        mentions=tuple(mention_names),
        mention_open_ids=tuple(mention_open_ids),
    )
