"""In-memory origin custody table owned by the inbox authority coordinator.

Decision record:
- Minimal in-memory table (I5-3 ruling section 2.4): keyed by ``message_id``,
  zero SQLite schema changes, lives on ``DeliveryCustodyCoordinator`` so claims
  survive inbox actor generation restarts and clear on daemon restart.
- REVIEW3 fix: session liveness checks ``session_current(owner_actor, owner_id)``
  and actor equivalence permits owner-only relocation while rejecting different
  machines or actor names.
- Lock ordering invariant: the initial liveness snapshot is read outside
  ``self._lock``. Reacquiring ``self._lock`` revalidates session liveness before
  deleting a stale claim.
"""

from __future__ import annotations

from collections.abc import Iterable
import threading

from hyprial.daemon.impl.inbox.contracts.origin import (
    OriginClaim,
    OriginClaimResult,
    OriginClaimant,
    OriginOwnerKind,
    SessionCurrentFn,
)
from hyprial.kernel import parse_agent_uri


def _same_actor_or_owner_relocated(left: str, right: str) -> bool:
    if left == right:
        return True
    parsed_left = parse_agent_uri(left)
    parsed_right = parse_agent_uri(right)
    if parsed_left is None or parsed_right is None:
        return False
    return parsed_left[1:] == parsed_right[1:]


class OriginCustody:
    """Bounded in-memory custody table serializing headless vs session grabs."""

    def __init__(self, *, session_current: SessionCurrentFn | None = None) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, OriginClaim] = {}
        self._seq = 0
        self._session_current = session_current

    def _claim_is_live(self, claim: OriginClaim) -> bool:
        if claim.owner_kind is OriginOwnerKind.HEADLESS:
            return True
        if claim.owner_kind is OriginOwnerKind.SESSION:
            if self._session_current is None:
                return True
            return bool(self._session_current(claim.owner_actor, claim.owner_id))
        return False

    @staticmethod
    def _same_owner(claim: OriginClaim, claimant: OriginClaimant) -> bool:
        if claimant.owner_kind is OriginOwnerKind.HEADLESS:
            return (
                claim.owner_kind is OriginOwnerKind.HEADLESS
                and _same_actor_or_owner_relocated(
                    claim.owner_actor, claimant.owner_actor
                )
            )
        if claimant.owner_kind is OriginOwnerKind.SESSION:
            return (
                claim.owner_kind is OriginOwnerKind.SESSION
                and claim.owner_id == claimant.owner_id
                and _same_actor_or_owner_relocated(
                    claim.owner_actor, claimant.owner_actor
                )
            )
        if claimant.owner_kind is OriginOwnerKind.WORKER_REF:
            return (
                claim.owner_kind is OriginOwnerKind.HEADLESS
                and _same_actor_or_owner_relocated(
                    claim.owner_actor, claimant.owner_actor
                )
            )
        return False

    def _evict_stale(self, message_ids: tuple[str, ...]) -> None:
        with self._lock:
            snapshots = {
                mid: claim
                for mid in message_ids
                if (claim := self._claims.get(mid)) is not None
            }
        stale: dict[str, OriginClaim] = {}
        for mid, existing in snapshots.items():
            if not self._claim_is_live(existing):
                stale[mid] = existing
        if not stale:
            return
        with self._lock:
            for mid, expected in stale.items():
                if self._claims.get(mid) is expected and not self._claim_is_live(expected):
                    self._claims.pop(mid, None)

    def claim_origins(
        self, message_ids: Iterable[str], claimant: OriginClaimant
    ) -> OriginClaimResult:
        """Atomically admit and (for holders) mint claims for one ordered group."""

        ids = tuple(dict.fromkeys(message_ids))
        self._evict_stale(ids)
        with self._lock:
            admitted: list[str] = []
            to_mint: list[str] = []
            existing_seq: int | None = None
            for mid in ids:
                current = self._claims.get(mid)
                if current is None:
                    admitted.append(mid)
                    if claimant.owner_kind in (
                        OriginOwnerKind.HEADLESS,
                        OriginOwnerKind.SESSION,
                    ):
                        to_mint.append(mid)
                    continue
                if self._same_owner(current, claimant):
                    admitted.append(mid)
                    if (
                        claimant.owner_kind is OriginOwnerKind.HEADLESS
                        and existing_seq is None
                    ):
                        existing_seq = current.claim_seq
            minted_seq: int | None = None
            if to_mint:
                self._seq += 1
                minted_seq = self._seq
                for mid in to_mint:
                    self._claims[mid] = OriginClaim(
                        message_id=mid,
                        owner_kind=claimant.owner_kind,
                        owner_actor=claimant.owner_actor,
                        owner_id=claimant.owner_id,
                        claim_seq=minted_seq,
                    )
            result_seq = (
                minted_seq
                if minted_seq is not None
                else (
                    existing_seq
                    if claimant.owner_kind is OriginOwnerKind.HEADLESS
                    else None
                )
            )
            return OriginClaimResult(
                claimed_ids=tuple(admitted),
                claim_seq=result_seq,
            )

    def filter_unheld_origins(
        self, message_ids: Iterable[str], claimant: OriginClaimant
    ) -> tuple[str, ...]:
        """Filter candidates without minting claims, evicting stale session claims."""

        ids = tuple(dict.fromkeys(message_ids))
        self._evict_stale(ids)
        with self._lock:
            admitted: list[str] = []
            for mid in ids:
                current = self._claims.get(mid)
                if current is None or self._same_owner(current, claimant):
                    admitted.append(mid)
            return tuple(admitted)

    def release_headless_claims(
        self,
        message_ids: Iterable[str],
        *,
        owner_actor: str,
        claim_seq: int,
    ) -> bool:
        """CAS-release headless claims matching ``(owner_actor, claim_seq)``."""

        released = False
        with self._lock:
            for mid in tuple(dict.fromkeys(message_ids)):
                current = self._claims.get(mid)
                if (
                    current is not None
                    and current.owner_kind is OriginOwnerKind.HEADLESS
                    and current.claim_seq == claim_seq
                    and _same_actor_or_owner_relocated(
                        current.owner_actor, owner_actor
                    )
                ):
                    self._claims.pop(mid, None)
                    released = True
        return released

    def forget_origin(self, message_id: str) -> None:
        """Unconditionally clear any origin claim on settlement, expiry, or reuse."""

        with self._lock:
            self._claims.pop(message_id, None)

    def snapshot(self) -> dict[str, OriginClaim]:
        """Return a shallow copy of current origin claims for inspection."""

        with self._lock:
            return dict(self._claims)


__all__ = ["OriginCustody"]
