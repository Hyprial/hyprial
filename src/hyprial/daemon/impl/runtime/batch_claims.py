"""Transient batch exclusion, never inbox settlement or delivery authority.

Accepted steer hands ownership to the connector; fetched inbox rows prevent
re-injection after exclusion is released. Generation changes and a ten-minute
progress lease recover missing terminal signals without acknowledging messages.
The read-only snapshot is replaced atomically for status readers.
"""

from __future__ import annotations

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage

BATCH_CLAIM_STALE_MS = 10 * 60 * 1000


class _BridgeBatchClaimsMixin:
    def _publish_batch_claims(self) -> None:
        self._batch_claim_snapshot = tuple(
            (actor, members, self._batch_claim_metadata.get(actor, (self._clock_ms(), None))[0])
            for actor, members in self._all_batch_claims().items()
        )

    def batch_claim_details(self, now_ms: int) -> dict[str, dict[str, object]]:
        return {
            self.harness_actor_uri(actor): {
                "claimedBy": list(members),
                "claimAgeMs": max(0, now_ms - created),
            }
            for actor, members, created in self._batch_claim_snapshot
        }

    def _log_batch_claim(self, event: str, actor: str, members: tuple[str, ...], reason: str) -> None:
        if self._logger is not None:
            self._logger(
                "warn" if event == "drop" else "info", "daemon",
                f"dispatch.batch_claim.{event}",
                agent=self.harness_actor_uri(actor), members=list(members), reason=reason,
            )

    def _begin_batch_claim(self, actor: str, members: tuple[str, ...]) -> None:
        reader = getattr(self.harnesses, "streaming_generations", None)
        generation = reader().get(actor) if callable(reader) else None
        self._batch_claim_metadata.setdefault(actor, (self._clock_ms(), generation))
        self._log_batch_claim("create", actor, members, "offer")

    def _claim_batch(
        self,
        actor: str,
        members: tuple[str, ...],
        messages: tuple[InboxMessage, ...],
        *,
        stamp: bool,
    ) -> None:
        recipient = self.harness_actor_uri(actor)
        for message in messages:
            self._note_delivery_seen(actor, message)
            self._refresh_queued_hold(
                message.message_id, worker=recipient,
                now_ms=self._clock_ms(), force=True,
            )
            if self._delivery_hooks is not None:
                self._delivery_hooks.mark_dispatched(message.message_id)
        self._batch_claims[actor] = (*self._batch_claims.get(actor, ()), *members)
        if actor not in self._batch_claim_metadata:
            self._begin_batch_claim(actor, members)
        self._publish_batch_claims()
        if stamp:
            self._confirm_batch_fetched(recipient, members)

    def _all_batch_claims(self) -> dict[str, tuple[str, ...]]:
        return {
            **{actor: members for actor, (members, _) in self._pending_batch_claims.items()},
            **self._batch_claims,
        }

    def _release_batch_claim(
        self, delivery_id: str, message_ids: tuple[str, ...] = (),
        *, reason: str = "result", event: str = "release",
    ) -> None:
        members = frozenset(message_ids or (delivery_id,))
        for actor, claimed in self._all_batch_claims().items():
            released = tuple(member for member in claimed if member in members)
            if not released:
                continue
            self._pending_batch_claims.pop(actor, None)
            remaining = tuple(member for member in claimed if member not in members)
            if remaining:
                self._batch_claims[actor] = remaining
            else:
                self._batch_claims.pop(actor, None)
                self._batch_claim_metadata.pop(actor, None)
            self._log_batch_claim(event, actor, released, reason)
        self._publish_batch_claims()

    def _release_steered_batch(self, actor: str, delivery_id: str) -> None:
        members = self._batch_claims.get(actor, ())
        if not members or members[0] != delivery_id:
            return
        self._confirm_batch_fetched(self.harness_actor_uri(actor), members)
        self._release_batch_claim(delivery_id, members, reason="steer-accepted")

    def _reconcile_batch_claims(self, active: set[str], generations: dict[str, int]) -> None:
        now = self._clock_ms()
        for actor, members in self._all_batch_claims().items():
            created, generation = self._batch_claim_metadata.setdefault(
                actor, (now, generations.get(actor))
            )
            reason = None
            if actor not in active:
                reason = "connector-stopped"
            elif generation != generations.get(actor):
                reason = "connector-generation-changed"
            else:
                pending = {m.message_id for m in self.inbox.pending_messages(self.harness_actor_uri(actor))}
                if not pending.intersection(members):
                    reason = "members-settled"
                else:
                    progress = max(
                        (attempt.last_progress_ms for member in members
                         if (attempt := self._inflight.get(member)) is not None),
                        default=created,
                    )
                    if now - max(created, progress) >= BATCH_CLAIM_STALE_MS:
                        reason = "no-progress-timeout"
            if reason is not None:
                self._release_batch_claim(members[0], members, reason=reason, event="drop")
        self._publish_batch_claims()
