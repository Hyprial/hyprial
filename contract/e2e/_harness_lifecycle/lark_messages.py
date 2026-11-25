"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def find_first_string(_entry: dict[str, Any], /, value, key):
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        for nested in value.values():
            found = _entry['find_first_string'](nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _entry['find_first_string'](nested, key)
            if found is not None:
                return found
    return None


def lark_read_with_retry(_entry: dict[str, Any], /, description, read, *, attempts):
    """Run a Lark READ-BACK with bounded backoff, logging every attempt.

    Why retry is safe here — and only here: the retried operation is pure
    OBSERVATION (reading back what the platform already holds).  The action
    under test (the send) is NEVER retried, so a retry can never mask a
    product regression; it only keeps a single network blip from turning
    "the read-back API hiccuped" into a red run that reads exactly like
    "the message never arrived" (runs 987/928).  Every attempt is logged;
    when the budget is exhausted the failure is still loud, raised as
    ReadbackExhausted so the caller can classify it as an observation-layer
    problem rather than a delivery failure.
    """

    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            return read()
        except (AssertionError, OSError, _entry['subprocess'].TimeoutExpired) as error:
            errors.append(f"attempt {attempt}: {type(error).__name__}: {error}")
            print(
                f"[readback-retry] {description}: attempt {attempt}/{attempts} "
                f"failed: {error}"
            )
            if attempt < attempts:
                _entry['time'].sleep(min(2 ** (attempt - 1), 4))
    raise _entry['ReadbackExhausted'](
        f"{description}: Lark read-back failed all {attempts} attempts "
        "(observation layer; says nothing about delivery) — "
        + " | ".join(errors)
    )


def _transcript_text(_entry: dict[str, Any], /, transcript):
    if transcript is None:
        return ""
    try:
        return transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def attachment_send_receipt(_entry: dict[str, Any], /, transcript, source):
    """Local proof the adapter actually delivered THIS run's attachment."""

    body = _entry['_transcript_text'](transcript)
    return str(source) in body and "resourceDeliveries" in body


def reply_send_receipt(_entry: dict[str, Any], /, transcript, marker):
    """Local proof the pinned worker produced its reply via harness_reply."""

    body = _entry['_transcript_text'](transcript)
    return "harness_reply" in body and marker in body


def lark_user_send(_entry: dict[str, Any], /, chat_id, text, idempotency_key):
    value = _entry['lark_cli'](
        "im", "+messages-send", "--as", "user", "--chat-id", chat_id,
        "--text", text, "--idempotency-key", idempotency_key, "--json",
    )
    message_id = _entry['find_first_string'](value.get("data", value), "message_id")
    if message_id is None:
        raise AssertionError("user send returned no message_id")
    return message_id


def onit_reactions(_entry: dict[str, Any], /, message_id):
    value = _entry['lark_read_with_retry'](
        f"reactions.list {message_id}",
        lambda: _entry['lark_cli'](
            "im", "reactions", "list", "--as", "user",
            "--message-id", message_id, "--reaction-type", "OnIt",
            "--user-id-type", "open_id", "--page-size", "50", "--json",
        ),
    )
    data = value.get("data")
    items = data.get("items", []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


def bot_reply_with_marker(_entry: dict[str, Any], /, chat_id, marker, since_ms):
    """A bot-sent message in the chat carrying ``marker``, newest first."""

    value = _entry['lark_read_with_retry'](
        f"chat-messages-list marker={marker}",
        lambda: _entry['lark_cli'](
            "im", "+chat-messages-list", "--as", "user", "--chat-id", chat_id,
            "--page-size", "20", "--order", "desc", "--no-reactions", "--json",
        ),
    )
    data = value.get("data")
    items = data.get("items") or data.get("messages") or [] if isinstance(
        data, dict
    ) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender")
        sender_type = (
            sender.get("sender_type") if isinstance(sender, dict) else None
        ) or item.get("sender_type")
        created = item.get("create_time")
        try:
            created_ms = int(created)
        except (TypeError, ValueError):
            created_ms = 0
        if created_ms and created_ms < since_ms:
            continue
        if sender_type is not None and sender_type != "app":
            continue
        body = _entry['json'].dumps(item, ensure_ascii=False)
        if marker in body:
            return item
    return None


def _lark_get_message_once(_entry: dict[str, Any], /, message_id):
    value = _entry['lark_cli'](
        "im", "+messages-mget", "--as", "user", "--message-ids", message_id,
        "--no-reactions", "--json",
    )
    data = value.get("data")
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or len(messages) != 1:
        raise AssertionError("message lookup did not return exactly one item")
    message = messages[0]
    if not isinstance(message, dict):
        raise AssertionError("message lookup item must be an object")
    return message


def lark_get_message(_entry: dict[str, Any], /, message_id):
    """messages-mget with read-back retry (consistency lag counts too)."""

    return _entry['lark_read_with_retry'](
        f"messages-mget {message_id}", lambda: _entry['_lark_get_message_once'](message_id)
    )


def _lark_message_content(_entry: dict[str, Any], /, message):
    raw = message.get("content")
    if raw is None and isinstance(message.get("body"), dict):
        raw = message["body"].get("content")
    if isinstance(raw, dict):
        return raw
    value: Any = None
    if isinstance(raw, str):
        try:
            value = _entry['json'].loads(raw)
        except _entry['json'].JSONDecodeError:
            # lark-cli renders resources for humans instead of preserving the
            # OpenAPI JSON body. Accept its documented markdown image form and
            # the bracket form emitted by 1.0.81 without weakening key checks.
            image = _entry['re'].fullmatch(
                r"(?:!\[Image\]\((img_[0-9A-Za-z_-]+)\)|"
                r"\[Image:\s*(img_[0-9A-Za-z_-]+)\])",
                raw,
            )
            if image is not None:
                return {"image_key": image.group(1) or image.group(2)}
            try:
                resource = _entry['ET'].fromstring(raw)
            except _entry['ET'].ParseError as error:
                raise AssertionError(
                    "Lark message content is neither JSON nor a supported "
                    "rendered resource marker"
                ) from error
            if resource.tag == "file":
                file_key = resource.attrib.get("key")
                if isinstance(file_key, str) and file_key.startswith("file_"):
                    return {
                        "file_key": file_key,
                        "file_name": resource.attrib.get("name"),
                    }
    if not isinstance(value, dict):
        raise AssertionError("Lark message content is not an object")
    return value


def bot_resource_message(_entry: dict[str, Any], /, chat_id, kind, since_ms, *, file_name, route_transcript, route_source):
    """Return this turn's user-visible resource and its platform key.

    Current lark-cli versions render ``create_time`` at minute precision, so
    the numeric timestamp guard cannot always exclude an older image.  Images
    also have no visible file name and every E2E-013 run intentionally uses
    the same tiny PNG.  When route evidence is supplied, require the candidate
    message id to occur beside this run's source path and resourceDeliveries
    in the pinned agent transcript before selecting it.
    """

    if (route_transcript is None) != (route_source is None):
        raise AssertionError(
            "resource lookup requires both route transcript and source"
        )

    value = _entry['lark_read_with_retry'](
        f"chat-messages-list kind={kind}",
        lambda: _entry['lark_cli'](
            "im", "+chat-messages-list", "--as", "user", "--chat-id", chat_id,
            "--page-size", "50", "--order", "desc", "--no-reactions", "--json",
        ),
    )
    data = value.get("data")
    items = data.get("items") or data.get("messages") or [] if isinstance(
        data, dict
    ) else []
    key_name = "image_key" if kind == "image" else "file_key"
    for item in items:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender")
        sender_type = (
            sender.get("sender_type") if isinstance(sender, dict) else None
        ) or item.get("sender_type")
        if sender_type is not None and sender_type != "app":
            continue
        try:
            created_ms = int(item.get("create_time") or 0)
        except (TypeError, ValueError):
            created_ms = 0
        if created_ms and created_ms < since_ms:
            continue
        message_id = item.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            continue
        full = _entry['lark_get_message'](message_id)
        if (full.get("msg_type") or full.get("message_type")) != kind:
            continue
        content = _entry['_lark_message_content'](full)
        if file_name is not None:
            visible_name = content.get("file_name") or content.get("fileName")
            if visible_name != file_name:
                continue
        resource_key = content.get(key_name)
        if not isinstance(resource_key, str) or not resource_key:
            continue
        if (
            route_transcript is not None
            and route_source is not None
            and not _entry['transcript_has_route_delivery'](
                route_transcript,
                source=route_source,
                native_message_id=message_id,
            )
        ):
            continue
        return full, resource_key
    return None


def assert_downloaded_resource(_entry: dict[str, Any], /, root, *, message, resource_key, kind, source):
    message_id = message.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise AssertionError("resource message has no native message id")
    # lark-cli infers and appends an extension when --output has none.  Keep
    # the source suffix explicit so the path we verify is the path it writes.
    output = root / f"user-downloaded-agent-{kind}{source.suffix}"
    _entry['lark_cli'](
        "im", "+messages-resources-download", "--as", "user",
        "--message-id", message_id, "--file-key", resource_key,
        "--type", kind, "--output", output.name, "--json", cwd=root,
    )
    if not output.is_file() or output.read_bytes() != source.read_bytes():
        raise AssertionError(
            f"user-downloaded {kind} content differs from the agent source"
        )
    return message_id


def transcript_has_route_delivery(_entry: dict[str, Any], /, transcript, *, source, native_message_id):
    """Whether the agent transcript has flushed complete route evidence."""

    try:
        body = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return (
        str(source) in body
        and native_message_id in body
        and "resourceDeliveries" in body
    )
