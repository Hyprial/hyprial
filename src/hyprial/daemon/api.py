"""Harness-neutral process seams owned by the daemon layer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .desired_state import HarnessLaunchSpec


@dataclass(frozen=True, slots=True)
class HarnessDelivery:
    """One daemon-owned inbox item handed to a managed streaming harness."""

    delivery_id: str
    conversation_id: str
    sender: str
    recipient: str
    message: str


class HarnessResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ProcessLivenessState(StrEnum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProcessLiveness:
    state: ProcessLivenessState
    observed: bool
    pid: int | None = None
    marker: str | None = None
    detail: str | None = None


class ProcessLivenessProbeError(RuntimeError):
    """A liveness probe failed; callers must surface, never default, it."""


_PERMANENT_FAILURE_CODES = frozenset(
    {
        "PROVIDER_USAGE_LIMIT",
        "PROVIDER_AUTHENTICATION_FAILED",
        "PROVIDER_BILLING_ERROR",
        "PROVIDER_PERMISSION_DENIED",
        "PROVIDER_INVALID_REQUEST",
        # A forward whose recipient cannot be resolved will never resolve on
        # redelivery; the sender is told once (docs/design-user-proxy-harness.md §5).
        "FORWARD_TARGET_UNKNOWN",
    }
)


def classify_harness_failure(error: str | None) -> str:
    """Map model-vendor prose to a stable code without persisting secret text."""

    detail = (error or "").casefold()
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
            "rate limit",
            "rate_limit",
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
class ManagedHarnessProcess(Protocol):
    """A harness child whose implementation belongs to ``hyprial.harnesses``."""

    @property
    def running(self) -> bool: ...

    @property
    def pid(self) -> int | None:
        """OS process id of the live child, or None when stopped or unknown.

        ``None`` is a valid answer for harnesses with no dedicated child
        process (for example an HTTP-driven session).
        """
        ...

    def liveness(self) -> ProcessLiveness: ...

    def stop(self) -> None: ...


@runtime_checkable
class StreamingHarnessProcess(Protocol):
    """Optional injection seam implemented by managed streaming runtimes."""

    @property
    def running(self) -> bool: ...

    @property
    def pid(self) -> int | None: ...

    def stop(self) -> None: ...

    def enqueue(self, delivery: HarnessDelivery) -> bool: ...

    def drain_results(self) -> tuple[HarnessResult, ...]: ...

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool: ...


@runtime_checkable
class HarnessLauncher(Protocol):
    """Placeholder seam used while harness implementations land independently."""

    def start(self, spec: HarnessLaunchSpec) -> ManagedHarnessProcess: ...
