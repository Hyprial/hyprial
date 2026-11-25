"""Harness-neutral process seams owned by the daemon layer.

Decision (Allen, 2026-11-19, 「同意，来源信息里有不认识的字段时，把那些字段丢掉」):
a message's ``origin`` is published in one closed shape, and reads are robust
against whatever is stored.  ``published_origin`` keeps only the fields that
shape declares, each with a usable value, and drops everything else; a sender
block without a platform account is dropped whole.  Storage is never
rewritten: only the published view is normalised, so one stored row can never
fail a read that publishes several.  Writes apply the same sender and chat-kind
rules (``origin_sender``, ``ORIGIN_CHAT_KINDS``) to what an adapter reports.
"""

from __future__ import annotations

from hyprial.kernel import (
    ManagedHarnessProcess,
    ProcessLiveness as ProcessLiveness,
    ProcessLivenessProbeError as ProcessLivenessProbeError,
)

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from hyprial.kernel import ipc_errors

from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    HarnessLaunchSpec,
)


ORIGIN_CHAT_KINDS = frozenset({"dm", "group"})
ORIGIN_SENDER_FIELDS = ("kind", "platformId", "displayName")
ORIGIN_FIELD_MAX_CHARS = 256
_ORIGIN_TEXT_FIELDS = frozenset(
    {"via", "provider", "platformMessageId", "platformCreatedAt", "onBehalfOf", "mechanism"}
)


def origin_sender(value: object) -> dict[str, Any] | None:
    """The closed sender block, or ``None`` when it names no platform account."""

    if not isinstance(value, Mapping):
        return None
    sender: dict[str, Any] = {}
    for field in ORIGIN_SENDER_FIELDS:
        text = value.get(field)
        sender[field] = (
            text[:ORIGIN_FIELD_MAX_CHARS] if isinstance(text, str) and text else None
        )
    return sender if sender["platformId"] is not None else None


def published_origin(origin: object) -> dict[str, Any]:
    """The declared fields of a stored origin, in stored order; nothing else."""

    if not isinstance(origin, Mapping):
        return {}
    published: dict[str, Any] = {}
    for key, value in origin.items():
        if key == "sender":
            sender = origin_sender(value)
            if sender is not None:
                published[key] = sender
        elif key == "chatKind":
            if isinstance(value, str) and value in ORIGIN_CHAT_KINDS:
                published[key] = value
        elif key in _ORIGIN_TEXT_FIELDS and isinstance(value, str) and value:
            published[key] = value
    return published


def published_message_origin(body: object, sender: str) -> dict[str, Any]:
    """The stored origin in the published shape, never failing on its content."""

    stored = body.get("origin") if isinstance(body, dict) else None
    published = published_origin(stored)
    if published:
        return published
    return visible_message_origin({}, sender)


def visible_message_origin(body: object, sender: str) -> dict[str, Any]:
    """Project stored provenance without granting trust to a missing record."""

    origin = body.get("origin") if isinstance(body, dict) else None
    if isinstance(origin, dict) and origin:
        return origin
    if sender.startswith(ADAPTER_URI_PREFIX):
        via = "adapter"
    elif sender.startswith("user:"):
        via = "operator"
    else:
        via = "daemon"
    return {"via": via}


def _freeze_delivery_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Harness delivery origin keys must be strings")
        return MappingProxyType(
            {key: _freeze_delivery_json(child) for key, child in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_delivery_json(child) for child in value)
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise TypeError(
        f"Harness delivery origin value is unsupported: {type(value).__name__}"
    )


@dataclass(frozen=True, slots=True)
class HarnessDelivery:
    """One daemon-owned inbox item handed to a managed streaming harness."""

    delivery_id: str
    conversation_id: str
    sender: str
    recipient: str
    message: str
    origin: Mapping[str, Any] | None = None
    #: Opaque text returned by a configured delivery hook.  The daemon never
    #: parses or rewrites it; ``None`` preserves the historical prompt bytes.
    hook_text: str | None = None
    #: Internal recursion fence for the mechanism's own actor exchange.
    hook_request: bool = False
    #: True only when reconcile sourced this delivery from ``system_notices``.
    notice: bool = False
    #: The frozen snapshot cursor when ``message`` is a complete
    #: ``inbox-batch-v1`` rendering.  Final model boundaries pass
    #: ``message`` verbatim in that case; the scalar prompt header is only
    #: for the retained single-message/mechanism path.
    batch_cursor: str | None = None
    #: Ordered members this batch delivery claims and settles independently.
    message_ids: tuple[str, ...] = ()
    #: Authoritative PAC classification, resolved before this delivery exists.
    workflow: bool = False

    def __post_init__(self) -> None:
        if self.message_ids and self.delivery_id not in self.message_ids:
            raise ValueError("a batch delivery id must be one of its member message ids")
        if self.origin is not None:
            object.__setattr__(self, "origin", _freeze_delivery_json(self.origin))

    def model_prompt(self) -> str:
        """The exact model input for this delivery.

        A batch delivery already carries the daemon's complete
        ``inbox-batch-v1`` rendering, so the scalar header would wrap it a
        second time.  Everything else keeps the historical prompt, including
        mechanism hook text.
        """

        return self.message if self.batch_cursor is not None else delivery_prompt(self)


def describe_sender(origin: Mapping[str, Any] | None, via: str) -> str | None:
    """One line naming the platform account behind an adapter-relayed message.

    ``via`` is the adapter address the message came from (its ``from``),
    printed verbatim.  The line is the same for every adapter kind and makes
    no claim about who the person is: an IM sender is whoever owns the
    account, and a display name is only what the adapter observed.
    """

    sender = origin.get("sender") if isinstance(origin, Mapping) else None
    if not isinstance(sender, Mapping):
        return None
    account = sender.get("platformId")
    if not isinstance(account, str) or not account:
        return None
    name = sender.get("displayName")
    shown = name if isinstance(name, str) and name else account
    return f"{shown} (via {via}, account {account})"


def delivery_prompt(delivery: HarnessDelivery) -> str:
    """The text a model receives for one delivery: a header, then the body.

    Headless workers used to receive the bare body, so they could not tell
    who wrote it or which of their identities it was addressed to.  The header
    uses the same bracketed shape as the pi interactive attach extension.
    """

    header = (
        f"[Harness Network message from {delivery.sender} to {delivery.recipient}"
    )
    chat_kind = (
        delivery.origin.get("chatKind")
        if isinstance(delivery.origin, Mapping)
        else None
    )
    if isinstance(chat_kind, str) and chat_kind:
        header += f" via {chat_kind} chat"
    provenance = (
        delivery.origin.get("via")
        if isinstance(delivery.origin, Mapping)
        else None
    )
    if isinstance(provenance, str) and provenance:
        header += f"; provenance: {provenance}"
    on_behalf_of = (
        delivery.origin.get("onBehalfOf")
        if isinstance(delivery.origin, Mapping)
        else None
    )
    if isinstance(on_behalf_of, str) and on_behalf_of:
        header += f"; on behalf of: {on_behalf_of}"
    human = describe_sender(delivery.origin, delivery.sender)
    if human is not None:
        header += f"; sender: {human}"
    prompt = f"{header}]\n{delivery.message}"
    if delivery.hook_text is None:
        return prompt
    return (
        f"{prompt}\n\n"
        "<<<HYPRIAL-HOOK>>>\n"
        f"{delivery.hook_text}\n"
        "<<<END-HYPRIAL-HOOK>>>"
    )


class HarnessResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


_PERMANENT_FAILURE_CODES = frozenset(
    {
        # Permanent for THIS delivery: one attempt, one notice, no
        # redelivery.  Unlike PROVIDER_USAGE_LIMIT it is deliberately NOT a
        # blocking reason -- a rate-limit window resets, so the agent must
        # keep receiving its next message (see the code's comment in
        # classify_harness_failure).
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_USAGE_LIMIT",
        "PROVIDER_AUTHENTICATION_FAILED",
        "PROVIDER_BILLING_ERROR",
        "PROVIDER_PERMISSION_DENIED",
        "PROVIDER_INVALID_REQUEST",
        ipc_errors.CONTEXT_EXHAUSTED,
    }
)


def classify_harness_failure(error: str | None) -> str:
    """Map model-vendor prose to a stable code without persisting secret text."""

    detail = (error or "").casefold()
    if any(
        token in detail
        for token in (
            "context exhausted",
            # The model vendor refusing the request because the conversation
            # is over the model's window.  2026-10-17 runtime incident: a
            # session on a model id Claude Code cannot size ran to the
            # vendor's 1,048,576 limit, and the rejection -- which arrives as
            # the vendor's own prose, often wrapped in ``invalid_request_error``
            # -- was caught by the generic invalid-request rule below and
            # reported as PROVIDER_INVALID_REQUEST.  That code reads as "your
            # request was malformed", so an operator sees a configuration
            # mistake where the real cause is a conversation that outgrew the
            # window.  Only wordings that name the limit itself are matched;
            # a bare "context length" keeps its historical invalid-request
            # mapping.  Both codes are permanent, so settlement is unchanged:
            # this renames the cause, it does not change what delivery does.
            "prompt is too long",
            "maximum context length",
            "context_length_exceeded",
        )
    ):
        return ipc_errors.CONTEXT_EXHAUSTED
    if any(
        token in detail
        for token in (
            "usage limit",
            "usage_limit",
            "quota exceeded",
            "quota exhausted",
            "insufficient_quota",
            "exceeded your current quota",
        )
    ):
        return "PROVIDER_USAGE_LIMIT"
    if any(
        token in detail
        for token in (
            # §8.2: a 429/rate-limit stops the current turn for good, so the
            # daemon must not redeliver it either -- the runtime refused to
            # repeat the model-vendor call, and the daemon must agree.  It stays
            # distinct from PROVIDER_USAGE_LIMIT because the condition is
            # time-bounded: the window resets on its own, so the code is
            # permanent for this message but never a reason to block the
            # agent (no block reason maps it; the quota watchdog ignores it).
            "429",
            "too many requests",
            "rate limit",
            "rate_limit",
            "resource_exhausted",
        )
    ):
        return "PROVIDER_RATE_LIMITED"
    if any(
        token in detail
        for token in (
            "authentication failed",
            # Claude's wording (2026-09-17): "Failed to authenticate. API
            # Error: 403 Request not allowed".  Matched on the leading phrase;
            # a bare "403" or "not allowed" is too generic to substring-match.
            "failed to authenticate",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "oauth_org_not_allowed",
            # Terminal for the turn runtime already; the daemon must agree or
            # it redelivers a turn the runtime refused to repeat.
            "oauth",
            "token refresh",
            "expired token",
        )
    ):
        return "PROVIDER_AUTHENTICATION_FAILED"
    if any(
        token in detail
        for token in ("billing error", "billing_error", "payment required")
    ):
        return "PROVIDER_BILLING_ERROR"
    if any(token in detail for token in ("permission denied", "forbidden")):
        return "PROVIDER_PERMISSION_DENIED"
    if any(
        token in detail
        for token in ("invalid request", "invalid_request", "context length")
    ):
        return "PROVIDER_INVALID_REQUEST"
    if any(
        token in detail
        for token in (
            "overloaded",
            "timeout",
            "connection",
        )
    ):
        return "PROVIDER_TRANSIENT_FAILURE"
    return "HARNESS_TRANSIENT_FAILURE"


def harness_failure_is_permanent(code: str) -> bool:
    return code in _PERMANENT_FAILURE_CODES


@dataclass(frozen=True, slots=True)
class HarnessResult:
    """A harness turn outcome with a stable failure classification."""

    delivery_id: str
    recipient: str
    status: HarnessResultStatus
    output: str = ""
    error: str | None = None
    failure_code: str | None = None
    #: Ordered inbox members represented by one managed batch turn.  The turn
    #: has no separate identity, so ``delivery_id`` is one of these member IDs.
    message_ids: tuple[str, ...] = ()
    route_label: str | None = None
    route_outcome: str = "done"

    def __post_init__(self) -> None:
        if self.message_ids and self.delivery_id not in self.message_ids:
            raise ValueError("a batch result id must be one of its member message ids")
        if self.route_outcome not in {"done", "next", "refused"}:
            raise ValueError("route_outcome must be done, next, or refused")
        if self.route_outcome == "next":
            if (
                not isinstance(self.route_label, str)
                or not self.route_label
                or not self.route_label.strip()
            ):
                raise ValueError(
                    "route_label must be a non-empty string when route_outcome is next"
                )
        elif self.route_label is not None:
            raise ValueError("route_label is only allowed when route_outcome is next")


@runtime_checkable
class StreamingHarnessProcess(Protocol):
    """Optional injection seam implemented by managed streaming runtimes."""

    @property
    def running(self) -> bool: ...

    @property
    def pid(self) -> int | None: ...

    def stop(self) -> None: ...

    def enqueue(self, delivery: HarnessDelivery) -> bool: ...

    def drain_results(self, limit: int | None = None) -> tuple[HarnessResult, ...]: ...

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool: ...


@runtime_checkable
class DaemonInterruptibleHarnessProcess(Protocol):
    """Optional seam for recording a daemon-owned turn stop before I/O."""

    def prepare_daemon_interruption(self, reason: str) -> None: ...


@runtime_checkable
class HarnessLauncher(Protocol):
    """Placeholder seam used while harness implementations land independently."""

    def start(self, spec: HarnessLaunchSpec) -> ManagedHarnessProcess: ...
