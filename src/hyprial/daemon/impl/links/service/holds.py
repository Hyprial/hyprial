"""Hold re-evaluation, task bindings and the apply merge.

Internal module of ``hyprial.daemon.impl.links.service`` (spec §9.5).

Under the emergency switch ``HYPRIAL_LINKS_BYPASS`` every live held row is
released on the next pass (no queue-head ordering, a would-be ``Mint`` still
stages its reference), and single-sided references are not retracted: a
bypassed send is delivered rather than held, so "no held mail" no longer
means "nobody is waiting on this reference".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

import hyprial.daemon.impl.links.model as model
import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.controller import DwellSample, ReplySample
from hyprial.daemon.impl.links.ports import Allow, Mint
from hyprial.daemon.impl.links.store import (
    HeldRow,
    LedgerResult,
    LedgerRow,
    LedgerUpdate,
)
from hyprial.kernel import DaemonRequestError, ipc_errors

from ._base import _MAX_TASK_IDS, _ActorState


class HoldsMixin:
    """Hold re-evaluation, task bindings and the apply merge."""

    # -- composition only ----------------------------------------------

    def bind_task(
        self, *, principal: str, worker: str, task_id: str, now_ms: int
    ) -> Callable[[str], None]:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")
        pairs: list[tuple[str, str]] = []
        if self._is_local_agent(principal) and model.is_link_endpoint(worker):
            pairs.append((principal, worker))
        if self._is_local_agent(worker) and model.is_link_endpoint(principal):
            pairs.append((worker, principal))
        for actor, peer in pairs:
            with self._mem:
                st = self._state(actor)
                view = self._view(actor, now_ms)
                existing = view.links.get(peer)
                if existing is None or not existing.mine:
                    # Any-thread safe: stages a pending file, never writes the
                    # store from the PAC sender thread (§3.5).
                    self._mint_locked(
                        actor, peer, now_ms, lifecycle="pac_task", task_id=task_id
                    )
                bindings = st.task_bindings.setdefault(peer, set())
                bindings.add(task_id)
                while len(bindings) > _MAX_TASK_IDS:
                    bindings.pop()
                st.pending_bind.append((actor, peer, task_id, now_ms))
        self.kick()
        return lambda message_id: self._task_delivered(pairs, message_id, now_ms)

    def _task_delivered(
        self, pairs: Sequence[tuple[str, str]], message_id: str, now_ms: int
    ) -> None:
        if not pairs:
            return
        principal, peer = pairs[0]
        if not self._is_local_agent(principal):
            return
        with self._mem:
            st = self._state(principal)
            if any(item.message_id == message_id for item in st.outbound):
                return
            st.pending_open.append(
                LedgerRow(
                    actor=principal,
                    message_id=message_id,
                    direction="out",
                    peer=peer,
                    intent="request",
                    reply_to=None,
                    state="open",
                    opened_at_ms=now_ms,
                    expires_at_ms=now_ms + self._message_ttl_ms,
                )
            )
            st.outbound.append(policy.OutboundRequest(message_id, peer, now_ms))
        self.kick()

    def release_task(
        self, *, task_id: str, now_ms: int, worker: str | None = None
    ) -> None:
        del worker
        with self._mem:
            for actor in self._local_agents():
                st = self._states.get(actor)
                if st is None:
                    continue
                for peer in list(st.task_bindings):
                    if task_id in st.task_bindings[peer]:
                        st.task_bindings[peer].discard(task_id)
                        st.pending_unbind.append((actor, peer, task_id))
        self.kick()

    def observe_hook(self, event: object) -> None:
        del event
        self.kick()


    # -- internals: durable merge ---------------------------------------

    def _merge_held(self, held: Sequence[HeldRow]) -> None:
        ids: set[str] = set()
        for row in held:
            ids.add(row.message_id)
            st = self._state(row.sender)
            st.held[row.message_id] = row
            st.held_pending.pop(row.message_id, None)
        for state in self._states.values():
            for message_id in [item for item in state.held if item not in ids]:
                state.held.pop(message_id, None)
                state.held_detail.pop(message_id, None)
                state.held_pending.pop(message_id, None)

    def _merge_bindings(self, bindings: Sequence[tuple[str, str, str, int]]) -> None:
        seen: set[tuple[str, str, str]] = set()
        for actor, peer, task_id, _bound_at in bindings:
            if not self._is_local_agent(actor):
                continue
            seen.add((actor, peer, task_id))
            self._state(actor).task_bindings.setdefault(peer, set()).add(task_id)
        for actor, state in self._states.items():
            pending = {(actor, peer, task) for peer, task, _ in (
                (item[1], item[2], item[3]) for item in state.pending_bind
            )}
            for peer in list(state.task_bindings):
                for task_id in list(state.task_bindings[peer]):
                    if (actor, peer, task_id) in seen or (actor, peer, task_id) in pending:
                        continue
                    state.task_bindings[peer].discard(task_id)
                if not state.task_bindings[peer]:
                    del state.task_bindings[peer]


    # -- internals: hold re-evaluation ----------------------------------

    def _evaluate_holds(
        self, local: Sequence[str], rows: Sequence[LedgerRow], now_ms: int
    ) -> LedgerUpdate:
        del rows
        release: list[str] = []
        reasons: dict[str, str] = {}
        fail: dict[str, str] = {}
        for actor in local:
            with self._mem:
                st = self._state(actor)
                ordered = sorted(
                    st.held.items(), key=lambda item: item[1].created_at_ms
                )
            tentative: list[policy.OutboundRequest] = []
            released_here: set[str] = set()
            cwnd_peers: set[str] = set()
            for message_id, row in ordered:
                if row.expires_at_ms <= now_ms:
                    continue
                with self._mem:
                    state = self._state(actor)
                    if message_id not in state.held:
                        continue
                    if not row.reply_to and not self._bypass:
                        head = self._queue_head_excluding(state, row, released_here)
                        if head is not None:
                            continue
                    view = self._view(actor, now_ms, tentative=tentative)
                    try:
                        decision = policy.admit(
                            view,
                            sender=actor,
                            target=row.target,
                            intent=row.intent,
                            reply_to=row.reply_to,
                            now_ms=now_ms,
                        )
                    except DaemonRequestError as error:
                        fail[message_id] = error.code
                        continue
                if isinstance(decision, Allow):
                    if decision.mint:
                        self._mint(actor, row.target, now_ms, lifecycle="regular")
                    if decision.bypassed:
                        self._bypass_note(
                            actor, row.target, decision.bypassed, direction="release"
                        )
                    release.append(message_id)
                    released_here.add(message_id)
                    tentative.append(
                        policy.OutboundRequest(message_id, row.target, now_ms)
                    )
                    with self._mem:
                        self._state(actor).held_detail.pop(message_id, None)
                elif isinstance(decision, Mint):
                    if decision.evict:
                        self._reclaim(actor, decision.evict, "evicted", now_ms)
                    self._mint(actor, row.target, now_ms, lifecycle="regular")
                    reasons[message_id] = ipc_errors.LINK_MINTING
                    with self._mem:
                        state = self._state(actor)
                        current = state.held.get(message_id)
                        if current is not None:
                            state.held[message_id] = replace(
                                current, hold_reason=ipc_errors.LINK_MINTING
                            )
                        state.held_detail[message_id] = "minting"
                else:
                    if row.hold_reason != decision.reason:
                        reasons[message_id] = decision.reason
                        with self._mem:
                            state = self._state(actor)
                            current = state.held.get(message_id)
                            if current is not None:
                                state.held[message_id] = replace(
                                    current, hold_reason=decision.reason
                                )
                    with self._mem:
                        self._state(actor).held_detail[message_id] = decision.detail
                    if (
                        decision.reason == ipc_errors.LINK_WINDOW_FULL
                        and decision.detail == "cwnd"
                    ):
                        cwnd_peers.add(row.target)
            with self._mem:
                self._state(actor).held_cwnd_peers = frozenset(cwnd_peers)
        return LedgerUpdate(release=tuple(release), reasons=reasons, fail=fail)

    def _queue_head_excluding(
        self, st: _ActorState, current: HeldRow, released_here: set[str]
    ) -> tuple[str, str] | None:
        rows = sorted(
            (
                (row.created_at_ms, message_id, row)
                for message_id, row in st.held.items()
                if row.message_id != current.message_id
                and message_id not in released_here
                and row.sender == current.sender
                and row.target == current.target
                and row.reply_to is None
                and row.created_at_ms <= current.created_at_ms
            ),
            key=lambda item: (item[0], item[1]),
        )
        if not rows:
            return None
        row = rows[0][2]
        return (row.hold_reason, st.held_detail.get(row.message_id, ""))


    # -- internals: apply merge -----------------------------------------

    def _merge_apply(
        self,
        local: Sequence[str],
        update: LedgerUpdate,
        result: LedgerResult,
        now_ms: int,
    ) -> None:
        released_rows: dict[str, HeldRow] = {}
        for actor in local:
            st = self._states.get(actor)
            if st is None:
                continue
            for message_id in result.released:
                row = st.held.get(message_id)
                if row is None:
                    continue
                released_rows[message_id] = row
                del st.held[message_id]
                st.held_detail.pop(message_id, None)
                st.held_pending.pop(message_id, None)
        for actor in local:
            st = self._states.get(actor)
            if st is None:
                continue
            for message_id, row in released_rows.items():
                if row.sender != actor:
                    continue
                if row.reply_to:
                    request = st.pending_inbound.pop(row.reply_to, None)
                    censored = st.censored_inbound.pop(row.reply_to, None)
                    if request is not None:
                        st.dwell.append(
                            DwellSample(
                                at_ms=now_ms,
                                dwell_ms=max(1, now_ms - request.received_at_ms),
                            )
                        )
                    elif censored is None:
                        pass
                    st.replies.append(ReplySample(at_ms=now_ms, direction="sent"))
                else:
                    st.outbound.append(
                        policy.OutboundRequest(message_id, row.target, now_ms)
                    )
                st.last_used[row.target] = now_ms
            for message_id in (*result.failed, *result.expired):
                row = st.held.pop(message_id, None)
                st.held_detail.pop(message_id, None)
                st.held_pending.pop(message_id, None)
                if row is not None:
                    st.released_outbound.add(message_id)
            for message_id in result.cancelled:
                st.released_outbound.add(message_id)
            for actor_, message_id in result.drained:
                if actor_ == actor:
                    st.pending_drain.discard(message_id)
            for actor_, message_id in update.sampled:
                if actor_ == actor:
                    st.sampled_writeback.discard(message_id)
            for _actor, message_id, event_ms in update.close_inbound:
                if _actor != actor:
                    continue
                request = st.pending_inbound.pop(message_id, None)
                if request is not None:
                    st.dwell.append(
                        DwellSample(
                            at_ms=event_ms,
                            dwell_ms=max(1, event_ms - request.received_at_ms),
                        )
                    )
            for row in update.open_outbound:
                if row.actor != actor:
                    continue
                if any(item.message_id == row.message_id for item in st.outbound):
                    continue
                st.outbound.append(
                    policy.OutboundRequest(
                        row.message_id,
                        row.peer,
                        (
                            row.sent_at_ms
                            if row.sent_at_ms is not None
                            else row.opened_at_ms
                        ),
                    )
                )

    def _retract_single_sided(self, local: Sequence[str], now_ms: int) -> None:
        if self._bypass:
            return
        for actor in local:
            st = self._states.get(actor)
            if st is None:
                continue
            held_targets = {
                row.target for row in st.held.values() if row.reply_to is None
            } | {
                value[0] for value in st.held_pending.values() if value[2] is None
            }
            view = self._view(actor, now_ms)
            index = self._store.index()
            for peer, link in view.links.items():
                if not link.mine or link.theirs or link.state == "established":
                    continue
                if link.entry is not None and link.entry.lifecycle != "regular":
                    # A PAC-bound reference is released by its task, not by
                    # this best-effort retraction (§3.7).
                    continue
                if peer in st.reclaimed or peer in held_targets or peer in st.explicit:
                    continue
                # B11: a missing copy is not a withdrawal, so a single-sided
                # reference waits instead of being retracted.
                if peer in index.absent or peer in index.conflicted:
                    continue
                # Rework 3 (Q2b): stage the withdrawal through the one
                # pending slot instead of ``store.write`` — a direct write
                # here would bypass the slot and do store I/O under ``_mem``
                # (§3.5).  The end-of-pass ``_apply_pending`` hands it over.
                base = (
                    st.pending_file
                    or self._store.intents().get(actor)
                    or index.files.get(actor)
                )
                if base is None or peer not in base.entries:
                    continue
                st.pending_file = model.without_reference(base, peer)


    # -- internals: apply payload ---------------------------------------

    def _drain_bindings(self, local: Sequence[str]) -> LedgerUpdate:
        bind: list[tuple[str, str, str, int]] = []
        unbind: list[tuple[str, str, str]] = []
        for actor in local:
            st = self._states.get(actor)
            if st is None:
                continue
            bind.extend(st.pending_bind)
            unbind.extend(st.pending_unbind)
            st.pending_bind.clear()
            st.pending_unbind.clear()
        return LedgerUpdate(bind_tasks=tuple(bind), unbind_tasks=tuple(unbind))

    def _drain_pending_drains(
        self, local: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        drains: dict[str, tuple[str, ...]] = {}
        for actor in local:
            state = self._states.get(actor)
            if state is None or not state.pending_drain:
                continue
            drains[actor] = tuple(sorted(state.pending_drain))
        return drains

    def _drain_pending_open(self, local: Sequence[str]) -> tuple[LedgerRow, ...]:
        rows: list[LedgerRow] = []
        for actor in local:
            st = self._states.get(actor)
            if st is None:
                continue
            rows.extend(st.pending_open)
            st.pending_open.clear()
        return tuple(rows)
