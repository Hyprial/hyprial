"""Transient batch exclusion, never inbox settlement or delivery authority.

Accepted steer hands ownership to the connector; fetched inbox rows prevent
re-injection after exclusion is released. Generation changes and a ten-minute
progress lease recover missing terminal signals without acknowledging messages.
The read-only snapshot is replaced atomically for status readers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dataclasses import replace
import inspect
import time

if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.actor.contracts import CellDispatchStamp

from hyprial.daemon.impl.api import HarnessResult
from hyprial.daemon.impl.dispatch.runtime.offers import DispatchAgentTimeout
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant
from hyprial.kernel import is_agent_id_segment, parse_agent_uri

BATCH_CLAIM_STALE_MS = 10 * 60 * 1000


def _accepts_kwarg(fn: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        param.name == name or param.kind is inspect.Parameter.VAR_KEYWORD
        for param in parameters
    )


class _BridgeBatchClaimsMixin:
    def _actor_read_keys(self, actor: str) -> tuple[str, ...]:
        """Every inbox key this actor's worker drains, its network identity first.

        The wiring injects the daemon's consumer read keys; without it the
        worker drains exactly ``harness_actor_uri(actor)``.
        """

        recipient = self.harness_actor_uri(actor)
        reader = getattr(self, "_harness_read_keys", None)
        extra = reader(actor) if callable(reader) else ()
        return tuple(dict.fromkeys((recipient, *extra)))

    def _recipient_read_keys(self, recipient: str) -> tuple[str, ...]:
        """The consumer read keys of the agent ``recipient`` names, else just it.

        A batch result names one recipient (its first member's key).  An id
        URI reads the keys its registry record yields (the injected consumer
        read keys); a name URI reads the keys of the worker of that name.  Neither depends on
        the worker running or on a held batch claim.
        """

        reader = getattr(self, "_recipient_consumer_keys", None)
        keys = tuple(reader(recipient)) if callable(reader) else ()
        if len(keys) > 1 and recipient in keys:
            return keys
        parsed = parse_agent_uri(recipient)
        if parsed is None or is_agent_id_segment(parsed[2]):
            return (recipient,)
        keys = self._actor_read_keys(parsed[2])
        return keys if recipient in keys else (recipient,)

    def _batch_member_results(self, result: HarnessResult) -> tuple[HarnessResult, ...]:
        """One ordinary result per batch member, each under its own inbox key.

        A batch can mix rows from several consumer read keys; each member
        settles against the key its row is pending under.  A single-key worker reads nothing extra.
        """

        keys = self._recipient_read_keys(result.recipient)
        homes: dict[str, str] = {}
        if len(keys) > 1:
            members = frozenset(result.message_ids)
            for key in keys:
                for message in self.inbox.pending_messages(key):
                    if message.message_id in members:
                        homes.setdefault(message.message_id, key)
        return tuple(
            replace(
                result,
                delivery_id=member,
                message_ids=(),
                recipient=homes.get(member, result.recipient),
            )
            for member in result.message_ids
        )

    @staticmethod
    def _read_across(keys: tuple[str, ...], read) -> tuple[InboxMessage, ...]:
        """One actor's rows under every read key, in arrival order."""

        if len(keys) == 1:
            return read(keys[0])
        return tuple(
            sorted(
                (message for key in keys for message in read(key)),
                key=lambda message: message.created_at_ms,
            )
        )

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
        mark_fetched: bool,
        cell_stamp: "CellDispatchStamp | None",
    ) -> None:
        recipient = self.harness_actor_uri(actor)
        for message in messages:
            self._note_delivery_seen(actor, message, stamp=cell_stamp)
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
        if mark_fetched:
            for key in self._actor_read_keys(actor):
                self._confirm_batch_fetched(key, members)

    def _all_batch_claims(self) -> dict[str, tuple[str, ...]]:
        return {
            **{actor: members for actor, (members, _m, _s) in self._pending_batch_claims.items()},
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
        if reason in ("offer-refused", "offer-timeout", "stale", "generation"):
            self._release_origin_claims(tuple(members))
        self._publish_batch_claims()

    def _admitted_dispatchable_for(
        self,
        key: str,
        owner_actor: str,
        exclude: frozenset[str],
    ) -> tuple[InboxMessage, ...]:
        reader = getattr(self.inbox, "admitted_dispatchable", None)
        if not callable(reader):
            return self.inbox.pending_messages(key)
        kwargs: dict[str, Any] = {
            "now_ms": self._clock_ms(),
            "exclude": exclude,
        }
        if _accepts_kwarg(reader, "origin_claim"):
            kwargs["origin_claim"] = OriginClaimant.headless(owner_actor)
        return tuple(reader(key, **kwargs))

    def _record_origin_claims(
        self,
        message_ids: tuple[str, ...],
        owner_actor: str,
        claim_seq: int | None,
    ) -> None:
        if claim_seq is None or not message_ids:
            return
        claims = getattr(self, "_origin_claim_seqs", None)
        if claims is None:
            claims = self._origin_claim_seqs = {}
        retries = getattr(self, "_origin_claim_release_retries", None)
        for message_id in message_ids:
            claims[message_id] = (owner_actor, claim_seq)
            if isinstance(retries, set):
                retries.discard(message_id)

    def _forget_origin_claim_record(self, message_id: str) -> None:
        claims = getattr(self, "_origin_claim_seqs", None)
        if isinstance(claims, dict):
            claims.pop(message_id, None)
        retries = getattr(self, "_origin_claim_release_retries", None)
        if isinstance(retries, set):
            retries.discard(message_id)

    def _mark_origin_claim_release_retry(self, message_ids: tuple[str, ...]) -> None:
        retries = getattr(self, "_origin_claim_release_retries", None)
        claims = getattr(self, "_origin_claim_seqs", None)
        if isinstance(retries, set) and isinstance(claims, dict):
            retries.update(message_id for message_id in message_ids if message_id in claims)

    def _release_origin_claims(self, message_ids: tuple[str, ...]) -> None:
        claims = getattr(self, "_origin_claim_seqs", None)
        if not isinstance(claims, dict) or not message_ids:
            return
        grouped: dict[tuple[str, int], list[str]] = {}
        for message_id in message_ids:
            record = claims.get(message_id)
            if record is not None:
                grouped.setdefault(record, []).append(message_id)
        release = getattr(self.inbox, "release_origin_claims", None)
        if not callable(release):
            self._mark_origin_claim_release_retry(message_ids)
            return
        for (owner_actor, claim_seq), group_ids in grouped.items():
            try:
                release_result = release(
                    tuple(group_ids),
                    owner_actor=owner_actor,
                    claim_seq=claim_seq,
                )
            except Exception:  # noqa: BLE001 - best-effort release on worker failure
                self._mark_origin_claim_release_retry(tuple(group_ids))
                continue
            if release_result not in (True, False):
                self._mark_origin_claim_release_retry(tuple(group_ids))
                continue
            try:
                active_claims = self.inbox.read_origin_claims()
            except Exception:  # noqa: BLE001 - retain evidence for a later tick
                self._mark_origin_claim_release_retry(tuple(group_ids))
                continue
            for message_id in group_ids:
                current = active_claims.get(message_id)
                if current is None or (
                    current.owner_actor,
                    current.claim_seq,
                ) != (owner_actor, claim_seq):
                    if claims.get(message_id) == (owner_actor, claim_seq):
                        claims.pop(message_id, None)
                    retries = getattr(self, "_origin_claim_release_retries", None)
                    if isinstance(retries, set):
                        retries.discard(message_id)
                else:
                    self._mark_origin_claim_release_retry((message_id,))

    def _retry_origin_claim_releases(self) -> None:
        retries = getattr(self, "_origin_claim_release_retries", None)
        claims = getattr(self, "_origin_claim_seqs", None)
        if isinstance(retries, set) and isinstance(claims, dict):
            self._release_origin_claims(tuple(retries.intersection(claims)))

    def _drain_origin_claim_releases_on_stop(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            retries = getattr(self, "_origin_claim_release_retries", None)
            if not retries:
                return True
            self._retry_origin_claim_releases()
            if not retries:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
            time.sleep(min(0.01, remaining))

    def _drain_claims_before_stop(self, timeout: float) -> None:
        if not self._drain_origin_claim_releases_on_stop(timeout):
            raise RuntimeError("origin claim releases did not settle")

    def _claim_scalar_origin(self, actor: str, original: InboxMessage) -> bool:
        claim_fn = getattr(self.inbox, "claim_origin", None)
        if not callable(claim_fn):
            return True
        owner_actor = self.harness_actor_uri(actor)
        try:
            result = claim_fn(
                original.recipient,
                (original.message_id,),
                OriginClaimant.headless(owner_actor),
                now_ms=self._clock_ms(),
            )
        except Exception:  # noqa: BLE001 - contention/shutdown defers to next tick
            return False
        if original.message_id not in result.claimed_ids:
            return False
        self._record_origin_claims(
            result.claimed_ids,
            owner_actor,
            result.claim_seq,
        )
        return True

    def _fetch_with_headless_origin(
        self,
        recipient: str,
        members: tuple[str, ...],
        *,
        completed_turn: bool = False,
    ) -> None:
        fetch = getattr(self.inbox, "fetch_pending", None)
        if not callable(fetch):
            return
        claims = getattr(self, "_origin_claim_seqs", None)
        owner_actor = recipient
        if isinstance(claims, dict) and members:
            recorded = claims.get(members[0])
            if recorded is not None:
                owner_actor = recorded[0]
        kwargs: dict[str, Any] = {"only": frozenset(members)}
        if completed_turn:
            kwargs["completed_turn"] = True
        if _accepts_kwarg(fetch, "origin_claim"):
            kwargs["origin_claim"] = OriginClaimant.headless(owner_actor)
        fetch(recipient, **kwargs)

    def _release_scalar_origin(self, message_id: str, *, notice: bool) -> None:
        if not notice:
            self._release_origin_claims((message_id,))

    def _submit_claimed_envelope(
        self,
        actor: str,
        envelope: Any,
        *,
        original: InboxMessage,
        retry: bool,
        notice: bool,
    ) -> bool:
        try:
            submitted = self._submit_envelope(
                actor, envelope, original=original, retry=retry, notice=notice
            )
        except DispatchAgentTimeout:
            self._release_scalar_origin(original.message_id, notice=notice)
            raise
        if not submitted:
            self._release_scalar_origin(original.message_id, notice=notice)
        return submitted

    def _prepare_batch_origin_claim(
        self,
        recipient: str,
        members: tuple[str, ...],
        claim_seq: int | None,
        candidates: tuple[InboxMessage, ...],
    ) -> tuple[InboxMessage, ...]:
        self._record_origin_claims(members, recipient, claim_seq)
        member_set = frozenset(members)
        claimed = tuple(m for m in candidates if m.message_id in member_set)
        if not claimed:
            self._release_origin_claims(members)
        return claimed

    def _confirm_batch_fetched(
        self, recipient: str, members: tuple[str, ...]
    ) -> None:
        try:
            mark = getattr(self.inbox, "mark_batch_fetched", None)
            if callable(mark):
                mark(recipient, frozenset(members))
            else:
                self._fetch_with_headless_origin(recipient, members)
        except Exception as error:  # noqa: BLE001 - retry from the pending map
            pending = getattr(self, "_pending_batch_fetches", None)
            if pending is None:
                pending = self._pending_batch_fetches = {}
            previous = pending.get(recipient, (recipient, ()))[1]
            pending[recipient] = (recipient, tuple(dict.fromkeys((*previous, *members))))
            if self._logger is not None:
                self._logger(
                    "warn",
                    "daemon",
                    "harness.batch.fetch_deferred",
                    worker=recipient,
                    count=len(members),
                    errorType=type(error).__name__,
                )

    def _retry_batch_fetches(self) -> None:
        pending = getattr(self, "_pending_batch_fetches", None)
        if not pending:
            return
        for key, (recipient, members) in tuple(pending.items()):
            try:
                mark = getattr(self.inbox, "mark_batch_fetched", None)
                if callable(mark):
                    mark(recipient, frozenset(members))
                else:
                    self._fetch_with_headless_origin(recipient, members)
            except Exception:
                continue
            pending.pop(key, None)

    def _release_steered_batch(self, actor: str, delivery_id: str) -> None:
        members = self._batch_claims.get(actor, ())
        if not members or members[0] != delivery_id:
            return
        for key in self._actor_read_keys(actor):
            self._confirm_batch_fetched(key, members)
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
                pending = {
                    m.message_id
                    for m in self._read_across(
                        self._actor_read_keys(actor), self.inbox.pending_messages
                    )
                }
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
