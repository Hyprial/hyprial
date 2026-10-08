"""Platform-neutral seams and values used by the Lark adapter."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from hyprial.daemon.impl.adapters.lark.contracts.wire import is_safe_runtime_target_field
MAX_LARK_TEXT_CONTENT_BYTES = 150_000
#: A forwarded conversation is expanded child-by-child.  Both bounds are on
#: the *rendered* forward, not on the platform response: exceeding either is
#: reported in the text rather than silently dropping messages.
MAX_MERGE_FORWARD_ITEMS = 50
MAX_MERGE_FORWARD_DEPTH = 3
#: Emitted when a merge-forward's children are not (or cannot be) fetched.
#: The live websocket event carries only the platform's placeholder string,
#: so this is the honest answer until ``im.v1.message.get`` supplies items.
MERGE_FORWARD_LABEL = "Lark merged-forward message"
_SAFE_MESSAGE_TYPE = re.compile(r"[a-z0-9_]{1,64}")
_SAFE_POST_SHAPE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: One component of a ``<message_id>/<key>`` media reference.  Deliberately
#: conservative: platform ids/keys are ASCII identifier material, and the
#: bound keeps ``<message_id>_<key>.<ext>`` under filesystem name limits.
#: The leading character may not be ``.`` so no component can ever spell a
#: hidden or relative path segment.
_SAFE_MEDIA_REF_PART = re.compile(r"(?=[^.])[A-Za-z0-9._=-]{1,120}")
_ATTACHMENT_LABELS = {
    "image": "[Lark image]",
    "audio": "[Lark audio]",
    "media": "[Lark video]",
    "sticker": "[Lark sticker]",
    "folder": "[Lark folder]",
    "share_chat": "[Lark shared chat]",
    "share_user": "[Lark shared contact]",
    "location": "[Lark location]",
    "hongbao": "[Lark red packet]",
    "calendar": "[Lark calendar event]",
    "general_calendar": "[Lark calendar event]",
    "video_chat": "[Lark video call]",
    "system": "[Lark system message]",
}
_KNOWN_MESSAGE_TYPES = frozenset(
    {
        *_ATTACHMENT_LABELS,
        "text",
        "post",
        "merge_forward",
        "file",
        "interactive",
        "todo",
        "vote",
    }
)
_POST_ATTACHMENT_KEYS = frozenset(
    {"attachment", "attachments", "file_key", "files", "image_key", "media_key"}
)


@dataclass(frozen=True, slots=True)
class PostUnparsedShape:
    """Secret-free shape of one post element the adapter could not parse."""

    tags: tuple[str, ...]
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalizedLarkContent:
    text: str
    status: str = "supported"
    message_type: str = "unknown"
    unparsed_post_shapes: tuple[PostUnparsedShape, ...] = ()


@dataclass(frozen=True, slots=True)
class VisibleText:
    value: str


@dataclass(frozen=True, slots=True)
class VisibleParagraph:
    children: tuple[VisibleNode, ...]


@dataclass(frozen=True, slots=True)
class VisibleHeading:
    text: VisibleText


@dataclass(frozen=True, slots=True)
class VisibleLinkLabel:
    children: tuple[VisibleNode, ...]


@dataclass(frozen=True, slots=True)
class VisibleMention:
    name: str | None = None


@dataclass(frozen=True, slots=True)
class VisibleAttachmentLabel:
    label: str
    #: A stable ``<message_id>/<key>`` reference the agent can hand to
    #: ``hyprial adapter media get`` for a directed pull of the media bytes.
    #: 2026-08-14 ruling: dropping the retrieval id made every media message
    #: a dead end that only a human with API credentials could follow up.
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class VisiblePlaceholder:
    label: str


VisibleNode = (
    VisibleText
    | VisibleParagraph
    | VisibleHeading
    | VisibleLinkLabel
    | VisibleMention
    | VisibleAttachmentLabel
    | VisiblePlaceholder
)


@dataclass(frozen=True, slots=True)
class VisibleMessage:
    nodes: tuple[VisibleNode, ...]


def media_ref(message_id: object, key: object) -> str | None:
    """Build the ``<message_id>/<key>`` reference printed inside stand-ins.

    Returns ``None`` unless both parts are conservative identifier material:
    a malformed or adversarial value degrades to the bare label rather than
    letting platform data forge stand-in text or filesystem paths.
    """

    if (
        isinstance(message_id, str)
        and _SAFE_MEDIA_REF_PART.fullmatch(message_id)
        and isinstance(key, str)
        and _SAFE_MEDIA_REF_PART.fullmatch(key)
    ):
        return f"{message_id}/{key}"
    return None


def canonical_lark_message_type(value: object) -> str:
    """Return a closed-set type safe for metadata and persistence."""

    if (
        isinstance(value, str)
        and _SAFE_MESSAGE_TYPE.fullmatch(value)
        and value in _KNOWN_MESSAGE_TYPES
    ):
        return value
    return "unknown"


def _bounded_visible_text(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_LARK_TEXT_CONTENT_BYTES:
        return value
    marker = "\n[truncated]"
    budget = MAX_LARK_TEXT_CONTENT_BYTES - len(marker.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker


def _matching_delimiter(
    value: str,
    start: int,
    opening: str,
    closing: str,
) -> int:
    depth = 0
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _visible_scalar(value: object, *, strip: bool = True) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    visible = "".join(
        character
        for character in value
        if character in "\n\t"
        or not unicodedata.category(character).startswith("C")
    )
    return visible.strip() if strip else visible


def _markdown_nodes(value: object) -> tuple[VisibleNode, ...]:
    """Parse only visible Markdown constructs; destinations have no AST field."""

    value = _visible_scalar(value)
    if not value:
        return ()
    nodes: list[VisibleNode] = []
    text: list[str] = []

    def flush() -> None:
        if text:
            nodes.append(VisibleText("".join(text)))
            text.clear()

    index = 0
    while index < len(value):
        image = (
            value[index] == "!"
            and index + 1 < len(value)
            and value[index + 1] == "["
        )
        label_start = index + 1 if image else index
        if value[label_start : label_start + 1] == "[":
            label_end = _matching_delimiter(value, label_start, "[", "]")
            target_start = label_end + 1
            if label_end >= 0 and value[target_start : target_start + 1] == "(":
                target_end = _matching_delimiter(value, target_start, "(", ")")
                if target_end >= 0:
                    label = value[label_start + 1 : label_end]
                    flush()
                    if image:
                        nodes.append(VisibleAttachmentLabel("image"))
                    else:
                        label_nodes = _markdown_nodes(label)
                        nodes.append(
                            VisibleLinkLabel(
                                label_nodes
                                or (VisiblePlaceholder("link"),)
                            )
                        )
                    index = target_end + 1
                    continue
        if value[index] == "\\" and index + 1 < len(value):
            text.append(value[index + 1])
            index += 2
            continue
        text.append(value[index])
        index += 1
    flush()
    return tuple(nodes)


def _post_shape(value: object) -> PostUnparsedShape:
    if not isinstance(value, Mapping):
        return PostUnparsedShape((), ())
    tag = value.get("tag")
    tags = (
        (tag,)
        if isinstance(tag, str) and _SAFE_POST_SHAPE_NAME.fullmatch(tag)
        else ()
    )
    keys = tuple(
        sorted(
            {
                key if _SAFE_POST_SHAPE_NAME.fullmatch(key) else "<invalid>"
                for key in value
                if isinstance(key, str)
            }
        )
    )
    return PostUnparsedShape(tags, keys)


def _attachment_shapes(value: object) -> tuple[PostUnparsedShape, ...]:
    """Find attachment-like mappings without retaining any field values."""

    shapes: list[PostUnparsedShape] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            names = {
                key.lower()
                for key in item
                if isinstance(key, str)
            }
            attachment_like = any(
                name in _POST_ATTACHMENT_KEYS
                or name.startswith("media_")
                or name.endswith("_media_key")
                for name in names
            )
            if attachment_like:
                shapes.append(_post_shape(item))
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(shapes)


def _post_message(
    value: object,
    *,
    message_id: str | None = None,
    unparsed: list[PostUnparsedShape] | None = None,
) -> VisibleMessage:
    if not isinstance(value, Mapping):
        return VisibleMessage(())
    if "content" not in value and "content_v2" not in value:
        locales = sorted(
            (
                (str(key), item)
                for key, item in value.items()
                if isinstance(item, Mapping)
                and ("content" in item or "content_v2" in item)
            ),
            key=lambda item: (
                item[0] not in {"zh_cn", "en_us"},
                item[0] != "zh_cn",
                item[0],
            ),
        )
        if locales:
            selected = dict(locales[0][1])
            # Some client-authored posts keep attachments beside the locale
            # map rather than inside it. They still belong to the selected
            # post and use the carrying message's resource endpoint.
            if "files" in value and "files" not in selected:
                selected["files"] = value["files"]
            message = _post_message(
                selected, message_id=message_id, unparsed=unparsed
            )
            locale_keys = {name for name, _item in locales}
            outside = {
                key: item
                for key, item in value.items()
                if str(key) not in locale_keys and key != "files"
            }
            shapes = _attachment_shapes(outside)
            if unparsed is not None:
                unparsed.extend(shapes)
            return VisibleMessage(
                (
                    *message.nodes,
                    *(
                        VisiblePlaceholder("attachment not parsed")
                        for _ in shapes
                    ),
                )
            )
    nodes: list[VisibleNode] = []
    title = _visible_scalar(value.get("title"))
    if title:
        nodes.append(VisibleHeading(VisibleText(title)))
    # REST/event responses can carry both forms. ``content_v2`` is the richer
    # receive shape (including the documented ``md`` element); selecting one
    # representation avoids rendering duplicate rows when both are present.
    # An empty content_v2 must not hide a populated content (review 901).
    rows = value.get("content_v2")
    if not isinstance(rows, list) or not rows:
        rows = value.get("content")
    if isinstance(rows, list):
        for row in rows:
            children: list[VisibleNode] = []
            if not isinstance(row, list):
                children.append(VisiblePlaceholder("attachment not parsed"))
                if unparsed is not None:
                    unparsed.append(_post_shape(row))
            else:
                for item in row:
                    if not isinstance(item, Mapping):
                        children.append(VisiblePlaceholder("attachment not parsed"))
                        if unparsed is not None:
                            unparsed.append(_post_shape(item))
                        continue
                    tag = item.get("tag")
                    if tag in {"text", "code_block", "md"}:
                        visible = _visible_scalar(item.get("text"), strip=False)
                        if visible:
                            children.append(VisibleText(visible))
                    elif tag == "a":
                        label = _visible_scalar(item.get("text"), strip=False)
                        children.append(
                            VisibleLinkLabel(
                                (VisibleText(label),)
                                if label
                                else (VisiblePlaceholder("link"),)
                            )
                        )
                    elif tag == "hr":
                        # A horizontal rule carries no content and is not an
                        # attachment; it never becomes a stand-in.
                        continue
                    elif tag == "at":
                        name = _visible_scalar(item.get("user_name")) or None
                        children.append(VisibleMention(name))
                    elif tag in {"img", "emotion", "emoji", "media", "file"}:
                        label = {"img": "image", "emotion": "emoji"}.get(tag, tag)
                        ref = (
                            media_ref(message_id, item.get("image_key"))
                            if tag == "img"
                            else media_ref(message_id, item.get("file_key"))
                            if tag in {"file", "media"}
                            else None
                        )
                        children.append(VisibleAttachmentLabel(label, ref))
                    else:
                        children.append(VisiblePlaceholder("attachment not parsed"))
                        if unparsed is not None:
                            unparsed.append(_post_shape(item))
            if children:
                nodes.append(VisibleParagraph(tuple(children)))

    files = value.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, Mapping):
                nodes.append(VisiblePlaceholder("attachment not parsed"))
                if unparsed is not None:
                    unparsed.append(_post_shape(item))
                continue
            name = _visible_scalar(item.get("file_name"))
            if item.get("is_folder") is True:
                nodes.append(
                    VisibleAttachmentLabel(f"folder: {name}" if name else "folder")
                )
                continue
            ref = media_ref(message_id, item.get("file_key"))
            nodes.append(
                VisibleAttachmentLabel(
                    f"Lark file: {name}" if name else "Lark file", ref
                )
            )
            if ref is None:
                nodes.append(VisiblePlaceholder("attachment not parsed"))
                if unparsed is not None:
                    unparsed.append(_post_shape(item))
    elif files is not None:
        nodes.append(VisiblePlaceholder("attachment not parsed"))
        if unparsed is not None:
            unparsed.append(_post_shape(files))

    outside = {
        key: item
        for key, item in value.items()
        if key not in {"content", "content_v2", "files"}
    }
    shapes = _attachment_shapes(outside)
    nodes.extend(VisiblePlaceholder("attachment not parsed") for _ in shapes)
    if unparsed is not None:
        unparsed.extend(shapes)
    return VisibleMessage(tuple(nodes))


_CARD_WRAPPER_CHILDREN = {
    "action": ("actions",),
    "column": ("elements",),
    "column_set": ("columns",),
    "div": ("text", "fields", "extra"),
    "form": ("elements",),
    "note": ("elements",),
}

_CARD_TAGS_BY_CONTEXT = {
    "actions": frozenset({"button"}),
    "button-text": frozenset({"plain_text"}),
    "columns": frozenset({"column"}),
    "elements": frozenset(
        {"action", "column_set", "div", "form", "markdown", "note"}
    ),
    "extra": frozenset({"button"}),
    "fields": frozenset({"lark_md", "plain_text"}),
    "text": frozenset({"lark_md", "plain_text"}),
    "title": frozenset({"plain_text"}),
}


def _card_message(value: object) -> VisibleMessage:
    nodes: list[VisibleNode] = []
    plural_contexts = frozenset(
        {"actions", "columns", "elements", "fields"}
    )

    def visit(item: object, *, context: str) -> None:
        if isinstance(item, Mapping):
            tag = item.get("tag")
            if tag is None:
                if context == "root":
                    for child_key in ("header", "elements"):
                        visit(item.get(child_key), context=child_key)
                elif context == "header":
                    visit(item.get("title"), context="title")
                else:
                    nodes.append(VisiblePlaceholder("unknown card element"))
                return
            if tag not in _CARD_TAGS_BY_CONTEXT.get(context, ()):
                nodes.append(VisiblePlaceholder("unknown card element"))
                return
            if tag in {"markdown", "lark_md"}:
                children = _markdown_nodes(item.get("content"))
                if children:
                    nodes.append(VisibleParagraph(children))
                return
            if tag == "plain_text":
                visible = _visible_scalar(item.get("content"))
                if visible:
                    if context == "title":
                        nodes.append(VisibleHeading(VisibleText(visible)))
                    else:
                        nodes.append(VisibleText(visible))
                return
            if tag == "button":
                visit(item.get("text"), context="button-text")
                return
            child_keys = _CARD_WRAPPER_CHILDREN.get(tag)
            if child_keys is None:
                nodes.append(VisiblePlaceholder("unknown card element"))
                return
            for child_key in child_keys:
                visit(item.get(child_key), context=child_key)
        elif isinstance(item, list):
            if context in plural_contexts:
                for child in item:
                    visit(child, context=context)
            else:
                nodes.append(VisiblePlaceholder("unknown card element"))
        elif item is not None:
            nodes.append(VisiblePlaceholder("unknown card element"))

    visit(value, context="root")
    return VisibleMessage(tuple(nodes))


def _render_node(node: VisibleNode) -> str:
    if isinstance(node, VisibleText):
        return node.value
    if isinstance(node, VisibleHeading):
        return _render_node(node.text)
    if isinstance(node, VisibleParagraph):
        return "".join(_render_node(child) for child in node.children).strip()
    if isinstance(node, VisibleLinkLabel):
        return "".join(_render_node(child) for child in node.children)
    if isinstance(node, VisibleMention):
        return f"@{node.name}" if node.name else "[mention]"
    if isinstance(node, VisibleAttachmentLabel):
        if node.ref is not None:
            return f" [{node.label} ref:{node.ref}]"
        return f" [{node.label}]"
    if isinstance(node, VisiblePlaceholder):
        return f"[{node.label}]"
    raise TypeError(f"unsupported visible node: {type(node).__name__}")


def _render_message(message: VisibleMessage) -> str:
    return "\n".join(
        text
        for node in message.nodes
        if (text := _render_node(node).strip())
    )


def _visible_message_for_content(
    message_type: str,
    content: Mapping[str, Any],
    *,
    message_id: str | None = None,
) -> VisibleMessage:
    """Convert one closed-set Lark type from explicitly visible fields only.

    ``message_id`` is the id of the message *carrying* this content.  When
    known, image/file stand-ins embed a ``ref:<message_id>/<key>`` reference
    (2026-08-14 ruling) so the agent can pull the bytes on demand through
    ``hyprial adapter media get`` instead of a human replaying platform APIs.
    Media resource keys are the only ids that gained this exemption; URLs,
    tokens and action payloads still never reach Harness text.
    """

    if message_type == "text":
        raw = content.get("text")
        # Preserve the existing text contract: downstream command validation
        # must still observe control characters and reject unsafe targets.
        return (
            VisibleMessage((VisibleText(raw.strip()),))
            if isinstance(raw, str)
            else VisibleMessage(())
        )
    if message_type == "post":
        return _post_message(content, message_id=message_id)
    if message_type == "merge_forward":
        # Neither the websocket event nor the shell's own REST body carries the
        # forwarded conversation; only ``im.v1.message.get`` returns the child
        # items.  Callers that hold those items use
        # :func:`normalize_merge_forward` instead of this label.
        return VisibleMessage((VisibleAttachmentLabel(MERGE_FORWARD_LABEL),))
    if message_type == "file":
        name = _visible_scalar(content.get("file_name"))
        label = f"Lark file: {name}" if name else "Lark file"
        return VisibleMessage(
            (
                VisibleAttachmentLabel(
                    label, media_ref(message_id, content.get("file_key"))
                ),
            )
        )
    if message_type == "interactive":
        return VisibleMessage(
            (VisibleAttachmentLabel("Lark card"), *_card_message(content).nodes)
        )
    if message_type == "todo":
        return VisibleMessage(
            (
                VisibleAttachmentLabel("Lark task"),
                *_post_message(content.get("summary")).nodes,
            )
        )
    if message_type == "vote":
        nodes: list[VisibleNode] = [VisibleAttachmentLabel("Lark poll")]
        topic = _visible_scalar(content.get("topic"))
        if topic:
            nodes.append(VisibleHeading(VisibleText(topic)))
        options = content.get("options")
        if isinstance(options, list):
            for option in options:
                visible = _visible_scalar(option)
                if visible:
                    nodes.append(VisibleParagraph((VisibleText(visible),)))
        return VisibleMessage(tuple(nodes))
    if message_type in _ATTACHMENT_LABELS:
        # Only an image message is a retrievable media resource among these;
        # the rest keep their bare labels.
        ref = (
            media_ref(message_id, content.get("image_key"))
            if message_type == "image"
            else None
        )
        return VisibleMessage(
            (VisibleAttachmentLabel(_ATTACHMENT_LABELS[message_type][1:-1], ref),)
        )
    return VisibleMessage(())


def normalize_lark_message_content(
    message_type: object,
    raw_content: object,
    *,
    message_id: str | None = None,
) -> NormalizedLarkContent:
    """Convert Lark's JSON-string body to bounded, non-opaque readable text.

    URLs, tokens and action payloads are intentionally never serialized into
    Harness text. Unknown types are explicit audit outcomes, not an empty
    value that history reconciliation silently filters out.

    Exception by ruling (2026-08-14): when ``message_id`` names the carrying
    message, image/file stand-ins embed ``ref:<message_id>/<key>`` so media
    is retrievable via ``hyprial adapter media get`` — a directed pull by the
    agent, never an automatic inline of the bytes.
    """

    safe_type = canonical_lark_message_type(message_type)
    try:
        content = (
            json.loads(raw_content)
            if isinstance(raw_content, str)
            else raw_content
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        content = None
    if not isinstance(content, Mapping):
        # Verified against ``im.v1.message.get``: a merge_forward shell's REST
        # body is the bare platform string ``Merged and Forwarded Message``,
        # not JSON.  Treating that as an unreadable body dead-lettered the
        # whole forward on the recovery/reconcile paths.  The tolerance is
        # deliberately scoped to this one type; every other type keeps the
        # loud ``invalid`` outcome.
        if safe_type != "merge_forward":
            return NormalizedLarkContent(
                f"[Unreadable Lark {safe_type} message]", "invalid", safe_type
            )
        content = {}
    if safe_type == "unknown":
        return NormalizedLarkContent(
            "[Unsupported Lark message type]", "unsupported", "unknown"
        )
    unparsed: list[PostUnparsedShape] = []
    message = (
        _post_message(content, message_id=message_id, unparsed=unparsed)
        if safe_type == "post"
        else _visible_message_for_content(safe_type, content, message_id=message_id)
    )
    text = _render_message(message)
    if not text:
        return NormalizedLarkContent(
            f"[Unreadable Lark {safe_type} message]", "invalid", safe_type
        )
    return NormalizedLarkContent(
        _bounded_visible_text(text),
        message_type=safe_type,
        unparsed_post_shapes=tuple(unparsed),
    )


@dataclass(frozen=True, slots=True)
class LarkForwardedItem:
    """One item of an ``im.v1.message.get`` response for a merge-forward.

    The platform returns the forward as a flat list: index 0 is the shell (its
    body is only the placeholder string) and every following item is a child
    whose ``upper_message_id`` points back at the shell.  ``content`` is the
    raw body string and is only ever handed to the shared normalizer, so
    resource keys, ids and tokens cannot reach Harness text through here.
    """

    message_id: str
    message_type: str
    content: object
    upper_message_id: str | None = None
    #: The child's own sender (open_id for a user, the app's bot open_id for
    #: an ``app`` sender).  Carried so an expanded forward can say who said
    #: what — the 2026-08-14 field gap: children rendered anonymously.
    sender_id: str | None = None
    sender_type: str | None = None


def _forward_child_lines(
    text: str, *, number: str, depth: int, sender: str | None = None
) -> list[str]:
    """Render one child so multi-line bodies keep a visible boundary."""

    indent = "  " * depth
    first, *rest = text.split("\n")
    heading = f"{number} {sender}: {first}" if sender else f"{number} {first}"
    lines = [f"{indent}{heading}".rstrip()]
    continuation = f"{indent}{' ' * (len(number) + 1)}"
    lines.extend(f"{continuation}{line}".rstrip() for line in rest)
    return lines


def _forward_sender_label(
    item: LarkForwardedItem,
    resolve_sender: Callable[[str], str | None] | None,
) -> str | None:
    """Who sent one forwarded child, preferring a recorded display name.

    ``resolve_sender`` is a read-only lookup into the adapter's recorded
    identities (and must not raise; the injection site owns its errors).
    Unresolved senders fall back to the raw platform id — an honest open_id
    beats an anonymous line — and anything unprintable is omitted rather
    than allowed to forge stand-in text.
    """

    sender_id = item.sender_id
    if not isinstance(sender_id, str) or not sender_id:
        return None
    if resolve_sender is not None:
        name = resolve_sender(sender_id)
        if isinstance(name, str):
            visible = _visible_scalar(name)
            if visible:
                return visible
    return sender_id if is_safe_runtime_target_field(sender_id) else None


def normalize_merge_forward(
    shell_message_id: str,
    items: Sequence[LarkForwardedItem],
    *,
    resolve_sender: Callable[[str], str | None] | None = None,
) -> NormalizedLarkContent:
    """Expand a forwarded conversation into the text its children carry.

    Each child is rendered by the *same* normalizer every other Lark type
    already uses, so ``post`` flattening, ``image``/``file`` labels and plain
    ``text`` behave identically inside a forward and outside one.  Each child
    line is annotated with its sender — a recorded display name when
    ``resolve_sender`` knows one, the raw platform id otherwise — and media
    stand-ins carry ``ref:<child_message_id>/<key>`` so any depth of nesting
    stays retrievable.

    Bounds are explicit, never silent: a forward wider than
    :data:`MAX_MERGE_FORWARD_ITEMS` or deeper than
    :data:`MAX_MERGE_FORWARD_DEPTH` still says how much was withheld.  A child
    that is itself a merge-forward renders as its own label plus whatever
    grandchildren the same response happened to carry -- no extra API call is
    made from this pure seam.
    """

    known_ids = {
        item.message_id for item in items if item.message_id != shell_message_id
    }
    by_parent: dict[str, list[LarkForwardedItem]] = {}
    total = 0
    for item in items:
        if item.message_id == shell_message_id:
            continue  # the shell's own body is only the platform placeholder
        total += 1
        parent = item.upper_message_id or shell_message_id
        if parent != shell_message_id and parent not in known_ids:
            # An orphan (its parent is absent from this response) still gets
            # rendered, at the top level, rather than silently withheld.
            parent = shell_message_id
        by_parent.setdefault(parent, []).append(item)

    if not total:
        return NormalizedLarkContent(
            f"[{MERGE_FORWARD_LABEL}]", "supported", "merge_forward"
        )

    lines: list[str] = []
    visited: set[str] = {shell_message_id}
    rendered = 0
    depth_limited = False

    def walk(parent_id: str, prefix: str, depth: int) -> None:
        nonlocal rendered, depth_limited
        children = by_parent.get(parent_id, ())
        if not children:
            return
        if depth > MAX_MERGE_FORWARD_DEPTH:
            depth_limited = True
            lines.append(
                f"{'  ' * (depth - 1)}[nested forwarded messages not expanded "
                f"(depth limit {MAX_MERGE_FORWARD_DEPTH})]"
            )
            return
        for index, child in enumerate(children, start=1):
            if rendered >= MAX_MERGE_FORWARD_ITEMS:
                return
            if child.message_id in visited:
                continue  # defensive: a cyclic upper_message_id must not hang
            visited.add(child.message_id)
            rendered += 1
            number = f"{prefix}{index}."
            child_text = normalize_lark_message_content(
                child.message_type,
                child.content,
                # The child's own id: media refs stay valid at any nesting
                # depth because each ref names its carrying message directly.
                message_id=child.message_id,
            ).text
            lines.extend(
                _forward_child_lines(
                    child_text,
                    number=number,
                    depth=depth - 1,
                    sender=_forward_sender_label(child, resolve_sender),
                )
            )
            walk(child.message_id, f"{number}", depth + 1)

    walk(shell_message_id, "", 1)

    header = f"[{MERGE_FORWARD_LABEL}: {total} messages]"
    if rendered < total:
        reason = (
            f"limits: {MAX_MERGE_FORWARD_ITEMS} messages, "
            f"depth {MAX_MERGE_FORWARD_DEPTH}"
            if depth_limited
            else f"limit: {MAX_MERGE_FORWARD_ITEMS} messages"
        )
        lines.append(
            f"[{total - rendered} of {total} forwarded messages not shown "
            f"({reason})]"
        )
    return NormalizedLarkContent(
        _bounded_visible_text("\n".join((header, *lines))),
        "supported",
        "merge_forward",
    )
