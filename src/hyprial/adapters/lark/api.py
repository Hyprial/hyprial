"""Platform-neutral seams and values used by the Lark adapter."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


MAX_RUNTIME_TARGET_FIELD_BYTES = 4096
MAX_RUNTIME_TARGET_ITEM_BYTES = 4096
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


@dataclass(frozen=True, slots=True)
class NormalizedLarkContent:
    text: str
    status: str = "supported"
    message_type: str = "unknown"


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


def _post_message(
    value: object, *, message_id: str | None = None
) -> VisibleMessage:
    if not isinstance(value, Mapping):
        return VisibleMessage(())
    if "content" not in value:
        locales = sorted(
            (
                (str(key), item)
                for key, item in value.items()
                if isinstance(item, Mapping) and "content" in item
            ),
            key=lambda item: (
                item[0] not in {"zh_cn", "en_us"},
                item[0] != "zh_cn",
                item[0],
            ),
        )
        return (
            _post_message(locales[0][1], message_id=message_id)
            if locales
            else VisibleMessage(())
        )
    nodes: list[VisibleNode] = []
    title = _visible_scalar(value.get("title"))
    if title:
        nodes.append(VisibleHeading(VisibleText(title)))
    rows = value.get("content")
    if isinstance(rows, list):
        for row in rows:
            children: list[VisibleNode] = []
            if not isinstance(row, list):
                children.append(VisiblePlaceholder("unknown rich-text element"))
            else:
                for item in row:
                    if not isinstance(item, Mapping):
                        children.append(VisiblePlaceholder("unknown rich-text element"))
                        continue
                    tag = item.get("tag")
                    if tag in {"text", "code_block"}:
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
                    elif tag == "at":
                        name = _visible_scalar(item.get("user_name")) or None
                        children.append(VisibleMention(name))
                    elif tag in {"img", "emotion", "emoji", "media", "file"}:
                        label = {"img": "image", "emotion": "emoji"}.get(tag, tag)
                        # An embedded image is retrievable through the message
                        # that carries it, so its stand-in keeps the reference.
                        ref = (
                            media_ref(message_id, item.get("image_key"))
                            if tag == "img"
                            else None
                        )
                        children.append(VisibleAttachmentLabel(label, ref))
                    else:
                        children.append(VisiblePlaceholder("unknown rich-text element"))
            if children:
                nodes.append(VisibleParagraph(tuple(children)))
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
    text = _render_message(
        _visible_message_for_content(safe_type, content, message_id=message_id)
    )
    if not text:
        return NormalizedLarkContent(
            f"[Unreadable Lark {safe_type} message]", "invalid", safe_type
        )
    return NormalizedLarkContent(_bounded_visible_text(text), message_type=safe_type)


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


def encode_lark_text_content(text: str) -> str:
    """Encode the exact UTF-8 JSON string handed to the generated SDK."""

    return json.dumps({"text": text}, ensure_ascii=False)


def is_safe_runtime_target_field(value: object) -> bool:
    """Reject target data that could forge UI lines or exhaust a reply."""

    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8")) > MAX_RUNTIME_TARGET_FIELD_BYTES:
            return False
    except UnicodeError:
        return False
    return not any(unicodedata.category(character).startswith("C") for character in value)


@dataclass(frozen=True, slots=True)
class ActorTarget:
    actor_id: str
    actor_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RuntimeTarget:
    """Legacy presence address, not a stable Agent entity."""

    target_uri: str
    actor: str
    alias: str
    status: str

    def as_actor_target(self) -> ActorTarget:
        return ActorTarget(
            actor_id=self.target_uri,
            actor_key=self.actor,
            display_name=self.target_uri,
        )


@dataclass(frozen=True, slots=True)
class AgentDirectoryEntry:
    """The Lark-facing projection of one daemon agent-directory row."""

    name: str
    status: str
    pinned_adapters: tuple[str, ...] = ()
    preferred_harness: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessRequest:
    message_id: str
    from_actor_id: str
    to: ActorTarget
    text: str
    conversation_id: str
    provider_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HarnessReceipt:
    message_id: str


@dataclass(frozen=True, slots=True)
class HarnessDelivery:
    delivery_id: str
    message_id: str
    reply_to: str
    from_actor: ActorTarget
    text: str


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    message_id: str
    sender_id: str
    sender_type: str
    text: str
    #: Defaulted so existing constructions stay valid; quoting is no longer
    #: restricted to ``text``, so the rendered header must not claim it is.
    message_type: str = "text"


@dataclass(frozen=True, slots=True)
class InboundOutcome:
    status: str
    harness_message_id: str | None = None
    target_actor_id: str | None = None
    ack_error: str | None = None
    new_dead_letter: bool = False
    pending_capacity: PendingCommandCapacityStatus | None = None


@dataclass(frozen=True, slots=True)
class PendingCommandCapacityStatus:
    """Secret-free status for the pending-response map's aggregate budget.

    This does not describe or cap the complete Lark state file; correlation,
    recovery, and audit maps have separate retention rules.
    """

    status: str
    count: int
    bytes: int
    max_count: int
    max_bytes: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "status": self.status,
            "count": self.count,
            "bytes": self.bytes,
            "maxCount": self.max_count,
            "maxBytes": self.max_bytes,
        }


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    status: str
    native_message_id: str | None = None
    error: str | None = None
    cleanup_error: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Outcome of one post-reconnect history reconciliation sweep."""

    chats_scanned: int
    messages_scanned: int
    forwarded: int
    duplicates: int
    dead_lettered: int
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class LarkInboundMessage:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    text: str
    sender_id: str
    sender_type: str
    root_id: str | None = None
    thread_id: str | None = None
    reply_to: str | None = None
    create_time: str | None = None
    message_type: str = "text"
    content_status: str = "supported"
    #: The sender's cross-App union_id when the delivery path carries one
    #: (live websocket events do; REST recovery models do not).  open_ids are
    #: namespaced per App, so this is the only cross-adapter identity join.
    sender_union_id: str | None = None
    #: Display names of @-mentions, for humans and logs only.  Routing never
    #: matches on a name: names are mutable and collide.
    mentions: tuple[str, ...] = field(default_factory=tuple)
    #: The open_ids behind those mentions.  This is the identity key the
    #: adapter compares against its own bot open_id to decide "was THIS bot
    #: addressed" (bot-mention routing to the pinned agent).
    mention_open_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def conversation_id(self) -> str:
        # A direct-message pin belongs to the whole DM. Root/thread ids are
        # delivery metadata and must not split the conversation identity.
        return f"lark:{self.chat_id}:{self.chat_id}"


@dataclass(frozen=True, slots=True)
class LarkBotInfo:
    """This app's own bot identity (``bot/v3/info``).

    ``open_id`` is the bot's presence id in *this App's* namespace;
    ``app_name`` is the display name the bot bears in chats.
    """

    open_id: str
    app_name: str | None = None


@dataclass(frozen=True, slots=True)
class LarkChatSummary:
    """One row of the group list this app belongs to (``im/v1/chats``)."""

    chat_id: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class LarkChatMember:
    """One member row of a group (``im/v1/chats/{chat_id}/members``)."""

    open_id: str
    name: str | None = None


class LarkHistoryBatch(tuple[LarkInboundMessage, ...]):
    """Tuple-compatible history result with a bounded-completeness signal."""

    complete: bool

    def __new__(
        cls,
        values: tuple[LarkInboundMessage, ...] | list[LarkInboundMessage] = (),
        *,
        complete: bool = True,
    ) -> LarkHistoryBatch:
        instance = super().__new__(cls, values)
        instance.complete = complete
        return instance


@runtime_checkable
class LarkApiPort(Protocol):
    def add_reaction(self, message_id: str, emoji_type: str) -> None: ...

    def bot_open_id(self) -> str | None: ...

    def clear_reaction(self, message_id: str, emoji_type: str) -> None: ...

    def reply(
        self, message_id: str, text: str, *, idempotency_key: str
    ) -> str: ...

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str: ...

    def get_message(self, message_id: str) -> QuotedMessage | None: ...

    def get_inbound_message(
        self, message_id: str, *, chat_type: str | None = None
    ) -> LarkInboundMessage | None: ...

    def list_chat_messages(
        self,
        chat_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 50,
        max_pages: int = 20,
        chat_type: str | None = None,
    ) -> LarkHistoryBatch: ...


@runtime_checkable
class HarnessPort(Protocol):
    def send_request(self, request: HarnessRequest) -> HarnessReceipt: ...

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None: ...

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None: ...


@runtime_checkable
class RouteDirectory(Protocol):
    """Routing lookups the adapter needs from the worker.

    ``resolve_mention`` is gone: it matched mention display names against
    route names and returned the route's native chat id where an actor id
    belongs -- a TS-era leftover that could only misroute.  Mention routing
    is now identity-based inside the adapter (bot open_id -> pinned agent).
    """

    def actor_by_key(self, actor_key: str) -> ActorTarget | None: ...

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None: ...

    def list_runtime_targets(self) -> tuple[RuntimeTarget, ...]: ...

    def list_agent_directory(self) -> tuple[AgentDirectoryEntry, ...]: ...

    def resolve_runtime_target(self, token: str) -> tuple[ActorTarget, ...]: ...
