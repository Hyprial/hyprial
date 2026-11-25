"""E2E-004 content; explicit callbacks keep entrypoint seams live."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree
from feishu.evidence import ContractFailure


@dataclass(frozen=True)
class ContentPorts:
    """Only this module's observed collaborators and configuration."""

    ATTACHMENT_FILE_NAME: str
    get_message: Callable[..., Any]


def find_first_string(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        for nested in value.values():
            found = find_first_string(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_first_string(nested, key)
            if found is not None:
                return found
    return None


def _user_content_args(text: str, mention: str | None) -> list[str]:
    if mention is None:
        return ["--text", text]
    # Group mode: the platform only delivers group messages that @mention
    # the bot, so the user text carries the mention prefix.
    return [
        "--msg-type",
        "text",
        "--content",
        json.dumps({"text": f"{mention} {text}"}, ensure_ascii=False),
    ]


#: The flattened text a `post` (rich text) message MUST produce.  Written out
#: by hand rather than derived from the adapter, so the assertion pins the
#: contract instead of restating the implementation: title on its own line,
#: then one line per paragraph, link nodes contributing their visible label
#: (never the href).  The 2026-08-13 P0 dropped `post` messages entirely, so
#: "the body survives normalization" is the assertion that would have caught it.
def expected_post_text(nonce: str) -> str:
    return (
        f"CONTRACT_LARK_POST_TITLE_{nonce}\n"
        f"CONTRACT_LARK_POST_BODY_{nonce}hyprial-contract-link\n"
        f"CONTRACT_LARK_POST_TAIL_{nonce}"
    )


def post_content(nonce: str, mention: str | None) -> dict[str, Any]:
    """A rich-text post whose flattening is fully determined by the contract."""

    paragraph: list[dict[str, Any]] = [
        {"tag": "text", "text": f"CONTRACT_LARK_POST_BODY_{nonce}"},
        {
            "tag": "a",
            "text": "hyprial-contract-link",
            "href": "https://example.invalid/hyprial-contract",
        },
    ]
    if mention is not None:
        # Group mode only: the mention render depends on the bot's platform
        # display name, so a group post is asserted structurally, not by digest.
        paragraph.insert(0, {"tag": "at", "user_id": mention})
    return {
        "zh_cn": {
            "title": f"CONTRACT_LARK_POST_TITLE_{nonce}",
            "content": [
                paragraph,
                [{"tag": "text", "text": f"CONTRACT_LARK_POST_TAIL_{nonce}"}],
            ],
        }
    }


def media_key_from_content(content: object, field: str) -> str | None:
    """Extract a media key from API JSON or lark-cli's rendered tag."""

    try:
        parsed = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        parsed = None

    key = parsed.get(field) if isinstance(parsed, dict) else None
    if isinstance(key, str) and key:
        return key

    if not isinstance(content, str):
        return None
    if field == "image_key":
        image = re.fullmatch(r"\[Image:\s*([^\]\s]+)\]\s*", content)
        if image is not None:
            return image.group(1)
    try:
        element = ElementTree.fromstring(content)
    except (ElementTree.ParseError, ValueError):
        return None
    expected_tag = field.removesuffix("_key")
    actual_tag = element.tag.rsplit("}", 1)[-1]
    if actual_tag != expected_tag:
        return None
    key = element.attrib.get("key") or element.attrib.get(field)
    return key if isinstance(key, str) and key else None


def _lark_message_content(message: Mapping[str, Any]) -> dict[str, Any]:
    """Recover resource keys from OpenAPI JSON or lark-cli rendering."""

    raw = message.get("content")
    if raw is None and isinstance(message.get("body"), dict):
        raw = message["body"].get("content")
    if isinstance(raw, dict):
        return raw
    value: Any = None
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # lark-cli renders resources for humans instead of preserving the
            # OpenAPI JSON body. Accept its documented markdown image form and
            # the bracket form emitted by 1.0.81 without weakening key checks.
            image = re.fullmatch(
                r"(?:!\[Image\]\((img_[0-9A-Za-z_-]+)\)|"
                r"\[Image:\s*(img_[0-9A-Za-z_-]+)\])",
                raw,
            )
            if image is not None:
                return {"image_key": image.group(1) or image.group(2)}
            try:
                resource = ET.fromstring(raw)
            except ET.ParseError as error:
                raise ContractFailure(
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
        raise ContractFailure("Lark message content is not an object")
    return value


def sent_media_key(message_id: str, field: str, *, _ports: ContentPorts) -> str:
    """Read back the media key of a message this runner itself just sent."""

    message = _ports.get_message(message_id)
    parsed = _lark_message_content(message)
    key = parsed.get(field) if isinstance(parsed, dict) else None
    if not isinstance(key, str) or not key:
        raise ContractFailure(f"sent message {message_id} carries no {field}")
    return key


def expected_image_text(message_id: str, *, _ports: ContentPorts) -> str:
    return f"[Lark image ref:{message_id}/{sent_media_key(message_id, 'image_key', _ports=_ports)}]"


def expected_file_text(message_id: str, *, _ports: ContentPorts) -> str:
    return (
        f"[Lark file: {_ports.ATTACHMENT_FILE_NAME} "
        f"ref:{message_id}/{sent_media_key(message_id, 'file_key', _ports=_ports)}]"
    )
