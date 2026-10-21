"""Harness-neutral process seams owned by the daemon layer."""

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

from hyprial.kernel import HarnessLaunchSpec


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
    #: The stored ``origin`` block (chat type, resolved human sender) when
    #: the reporting adapter sent one; ``None`` means it said nothing.
    origin: Mapping[str, Any] | None = None
    #: Opaque text returned by a configured delivery hook.  The daemon never
    #: parses or rewrites it; ``None`` preserves the historical prompt bytes.
    hook_text: str | None = None
    #: Internal recursion fence for the mechanism's own actor exchange.
    hook_request: bool = False
    #: True only when reconcile sourced this delivery from ``system_notices``.
    notice: bool = False

    def __post_init__(self) -> None:
        if self.origin is not None:
            object.__setattr__(self, "origin", _freeze_delivery_json(self.origin))


def describe_sender(origin: Mapping[str, Any] | None) -> str | None:
    """One line naming the human behind an adapter-relayed message.

    Worded so an unconfirmed name cannot pass for an owner: only ``verified``
    with an owner prints "owner", a confirmed guest says it has none, and
    every other standing says it is not confirmed.  A
    platform display name is free text anyone can set, and one on record
    today equals an owner's real name under a different union_id.
    """

    sender = origin.get("sender") if isinstance(origin, Mapping) else None
    if not isinstance(sender, Mapping):
        return None
    name = sender.get("displayName") or sender.get("platformId") or "unknown"
    standing = sender.get("standing")
    if standing == "verified" and sender.get("owner"):
        return f"{name} (owner {sender['owner']}, verified)"
    if standing == "verified":
        return f"{name} (verified person, no hyprial owner)"
    candidate_users = [
        item for item in sender.get("candidateUsers") or () if isinstance(item, str)
    ]
    if standing == "ambiguous" and candidate_users:
        # From the user store: the candidates are people, and a guest among
        # them has no owner, so naming owners alone could print nothing.
        users = ", ".join(candidate_users)
        return f"{name} (NOT verified: sender ambiguous between users {users})"
    if standing == "ambiguous":
        candidates = ", ".join(sender.get("candidateOwners") or ())
        return f"{name} (NOT verified: owner ambiguous between {candidates})"
    if standing == "observed":
        return f"{name} (NOT verified: display name only, no owner)"
    return f"{name} (NOT verified: sender unresolved)"


def delivery_prompt(delivery: HarnessDelivery) -> str:
    """The text a model receives for one delivery: a header, then the body.

    Headless workers used to receive the bare body, so they could not tell
    who wrote it or which of their identities it was addressed to.  The header
    uses the same bracketed shape as the pi interactive attach extension.
    """

    header = (
        f"[Harness Network message from {delivery.sender} to {delivery.recipient}"
    )
    chat_type = (
        delivery.origin.get("chatType")
        if isinstance(delivery.origin, Mapping)
        else None
    )
    if isinstance(chat_type, str) and chat_type:
        header += f" via {chat_type} chat"
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
    human = describe_sender(delivery.origin)
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
        # A forward whose recipient cannot be resolved will never resolve on
        # redelivery; the sender is told once (docs/design/design-user-proxy-harness.md §5).
        "FORWARD_TARGET_UNKNOWN",
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
    #: Set only by a harness that relays (user-proxy): the completed turn is
    #: sent AS the worker to this address instead of replied to the sender.
    #: The daemon performs the send; the harness never holds a send path.
    forward_to: str | None = None


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
