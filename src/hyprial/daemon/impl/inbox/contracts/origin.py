"""In-memory origin custody contract types for I5-3 mutual exclusion.

Decision record:
- Every origin claimant carries ``(owner_kind, owner_actor, owner_id)``.
- REVIEW3 fix: session claim validity binds ``(owner_actor, owner_id)``
  together via ``SessionCurrentFn`` so ``sessionRef`` reuse on another actor
  never keeps a dead actor's claim alive.
- Non-holding readers (``PRINCIPAL`` and ``WORKER_REF`` without an active
  headless claim) never mint new claims and yield to any foreign live claim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

SessionCurrentFn = Callable[[str, str], bool]


class OriginOwnerKind(str, Enum):
    """Acquisition plane that owns one origin message row."""

    HEADLESS = "headless"
    SESSION = "session"
    WORKER_REF = "worker-ref"
    PRINCIPAL = "principal"


@dataclass(frozen=True, slots=True)
class OriginClaimant:
    """Caller identity presented at inbox origin claim/filter boundaries."""

    owner_kind: OriginOwnerKind
    owner_actor: str
    owner_id: str

    @classmethod
    def headless(cls, owner_actor: str) -> OriginClaimant:
        """Build a headless bridge claimant for one canonical actor URI."""

        return cls(OriginOwnerKind.HEADLESS, owner_actor, owner_actor)

    @classmethod
    def session(cls, owner_actor: str, session_ref: str) -> OriginClaimant:
        """Build an interactive session claimant bound to ``(actor, sessionRef)``."""

        return cls(OriginOwnerKind.SESSION, owner_actor, session_ref)

    @classmethod
    def worker_ref(cls, owner_actor: str) -> OriginClaimant:
        """Build a non-holding worker-ref reader for one managed worker actor."""

        return cls(OriginOwnerKind.WORKER_REF, owner_actor, "")

    @classmethod
    def principal(cls) -> OriginClaimant:
        """Build a non-holding operator/principal reader."""

        return cls(OriginOwnerKind.PRINCIPAL, "", "")


@dataclass(frozen=True, slots=True)
class OriginClaim:
    """One active in-memory origin claim recorded on the inbox actor."""

    message_id: str
    owner_kind: OriginOwnerKind
    owner_actor: str
    owner_id: str
    claim_seq: int


@dataclass(frozen=True, slots=True)
class OriginClaimResult:
    """Outcome of an atomic ``claim_origins`` or ``ClaimOriginCommand`` pass."""

    claimed_ids: tuple[str, ...]
    claim_seq: int | None


__all__ = [
    "OriginClaim",
    "OriginClaimResult",
    "OriginClaimant",
    "OriginOwnerKind",
    "SessionCurrentFn",
]
