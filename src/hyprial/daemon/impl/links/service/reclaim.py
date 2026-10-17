"""Reclaim, barrier, rebuild and reference synchronisation.

Internal module of ``hyprial.daemon.impl.links.service`` (spec §9.5).
"""

from __future__ import annotations

from collections.abc import Sequence

import hyprial.daemon.impl.links.model as model
import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.model import LinkEntry, LinkFile
from hyprial.daemon.impl.links.ports import EVENT_LINK_MINTED, EVENT_LINK_RECLAIMED
from hyprial.daemon.impl.links.store import (
    CensoredRecord,
    HeldRow,
    LedgerRow,
    LedgerUpdate,
)
from hyprial.kernel import ipc_errors

from ._base import _DRAINED_ID_LIMIT, _ActorState


class ReclaimMixin:
    """Reclaim, barrier, rebuild and reference synchronisation."""

    # -- internals: durable merge ---------------------------------------

    def _ensure_local_files(self, local: Sequence[str]) -> None:
        """Every resident local agent publishes an empty file once (B13).

        An empty committed file is the visible proof that this agent joined the
        link graph; a peer's ``sender_known`` depends on it.  A workflow-owned
        temporary worker is not a proactive participant: it talks to its owner
        and through the PAC dispatch channel, so publishing one empty file per
        worker only cost the org directory space a durable commit each (about
        320 workers on E2E-021).  A worker that really mints a governed
        reference still publishes the same file lazily through
        ``_apply_pending_files``, which keeps the ``sender_known`` proof.
        """

        index = self._store.index()
        if not index.org_available:
            return
        intents = self._store.intents()
        for actor in local:
            if self._pac is not None and self._pac.is_workflow_worker(actor):
                continue
            st = self._states.get(actor)
            if (
                actor in index.files
                or actor in intents
                or (st is not None and st.pending_file is not None)
            ):
                continue
            self._store.write(LinkFile(actor=actor, revision=0, entries={}))

    def _apply_pending_files(self, local: Sequence[str]) -> None:
        """Service thread: hand staged local files to the store (P-3, Q2b).

        The slot stays occupied until the store has recorded the intent:
        clearing it first reopens the window in which a concurrent mint
        builds on the committed file and resurrects a withdrawn peer.  The
        CAS afterwards keeps a file a mint staged mid-write intact.
        """

        for actor in local:
            with self._mem:
                st = self._states.get(actor)
                if st is None or st.pending_file is None:
                    continue
                file = st.pending_file
            try:
                self._store.write(file)
            except ValueError as error:  # noqa: BLE001 - not ours to write
                self._log("link.intent_rejected", actor=actor, detail=str(error))
                with self._mem:
                    if st.pending_file is file:
                        st.pending_file = None
                continue
            with self._mem:
                if st.pending_file is file:
                    st.pending_file = None

    def _apply_pending_reclaims(self, local: Sequence[str]) -> None:
        """Service thread: apply drop/evict requests queued by IPC/admin (P-3)."""

        for actor in local:
            with self._mem:
                st = self._states.get(actor)
                if st is None or not st.pending_reclaims:
                    continue
                queued = st.pending_reclaims
                st.pending_reclaims = []
            for peer, reason, now_ms in queued:
                self._reclaim(actor, peer, reason, now_ms)

    def _apply_pending(self, local: Sequence[str]) -> None:
        """Drain staged intents, then the reclaims they may have raced."""

        self._apply_pending_files(local)
        self._apply_pending_reclaims(local)

    def _rebuild(
        self,
        rows: Sequence[LedgerRow],
        held: Sequence[HeldRow],
        bindings: Sequence[tuple[str, str, str, int]],
    ) -> None:
        for row in rows:
            if row.actor not in self._states and not self._is_local_agent(row.actor):
                continue
            st = self._state(row.actor)
            if row.direction == "in":
                if row.state == "open" and row.reply_to is None:
                    st.admitted.add(row.message_id)
                    st.pending_inbound[row.message_id] = policy.PendingRequest(
                        row.message_id, row.peer, row.opened_at_ms
                    )
                elif row.state == "censored":
                    st.censored_inbound[row.message_id] = CensoredRecord(
                        actor=row.actor,
                        message_id=row.message_id,
                        requester=row.peer,
                        received_at_ms=row.opened_at_ms,
                        threshold_ms=row.threshold_ms or 0,
                        expires_at_ms=row.expires_at_ms,
                    )
                elif row.state == "drained":
                    st.drained.add(row.message_id)
                    st.settled[row.message_id] = row.expires_at_ms
                elif row.state in ("closed", "acked", "expired"):
                    # Ruling 70: settled ids survive a restart.
                    st.settled[row.message_id] = row.expires_at_ms
                    if row.state == "closed":
                        st.pending_inbound.pop(row.message_id, None)
            elif row.state == "open":
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
            elif row.state == "released":
                st.released_outbound.add(row.message_id)
        self._merge_held(held)
        self._merge_bindings(bindings)

    def _reconcile(self, rows: Sequence[LedgerRow], now_ms: int) -> None:
        by_actor: dict[str, list[LedgerRow]] = {}
        for row in rows:
            by_actor.setdefault(row.actor, []).append(row)
        horizon = now_ms - self._message_ttl_ms
        for actor, state in self._states.items():
            # Ruling 70: settled ids live exactly as long as the ledger keeps
            # their terminal rows; the purge horizon truncates the set.
            state.settled = {
                message_id: expiry
                for message_id, expiry in state.settled.items()
                if expiry > horizon
            }
            actor_rows = by_actor.get(actor, [])
            for row in actor_rows:
                if row.direction != "in":
                    continue
                if row.state in ("closed", "acked", "expired", "drained"):
                    state.admitted.discard(row.message_id)
                    state.pending_inbound.pop(row.message_id, None)
                    state.censored_inbound.pop(row.message_id, None)
                    state.settled[row.message_id] = row.expires_at_ms
                    if row.state == "drained":
                        state.drained.add(row.message_id)
                elif row.state == "open" and row.reply_to is None:
                    state.admitted.add(row.message_id)
                    state.pending_inbound[row.message_id] = policy.PendingRequest(
                        row.message_id, row.peer, row.opened_at_ms
                    )
                elif row.state == "censored":
                    if row.message_id not in state.censored_inbound:
                        state.censored_inbound[row.message_id] = CensoredRecord(
                            actor=actor,
                            message_id=row.message_id,
                            requester=row.peer,
                            received_at_ms=row.opened_at_ms,
                            threshold_ms=row.threshold_ms or 0,
                            expires_at_ms=row.expires_at_ms,
                        )
        self._trim_samples()


    # -- internals: reference sync --------------------------------------

    def _sync_references(self, local: Sequence[str], now_ms: int) -> None:
        for actor in local:
            with self._mem:
                st = self._state(actor)
                view = self._view(actor, now_ms)
                accepted: list[tuple[str, LinkEntry]] = []
                for peer, link in view.links.items():
                    if link.theirs and not link.mine and peer not in st.reclaimed:
                        theirs = self._store.index().files.get(peer)
                        entry = theirs.entries.get(actor) if theirs else None
                        if entry is None:
                            continue
                        if policy.accept_reference(
                            view,
                            requester=peer,
                            now_ms=now_ms,
                            lifecycle=entry.lifecycle,
                        ):
                            accepted.append((peer, entry))
                withdrawn = self._withdrawn(state=st, view=view)
                self._clear_barriers(actor=actor, state=st)
            for peer, entry in accepted:
                self._mint(
                    actor,
                    peer,
                    now_ms,
                    lifecycle=entry.lifecycle,
                    task_id=entry.task_id,
                )
                with self._mem:
                    self._state(actor).minted_side.setdefault(peer, "acceptor")
            for peer in withdrawn:
                self._reclaim(actor, peer, "peer_withdrew", now_ms)
            self._emit_established(actor, now_ms)

    def _withdrawn(self, *, state: _ActorState, view: policy.NodeView) -> list[str]:
        index = self._store.index()
        gone: list[str] = []
        for peer, revision in list(state.established.items()):
            if peer in state.reclaimed:
                continue
            file = index.files.get(peer)
            if file is None or peer in index.absent or peer in index.conflicted:
                continue
            link = view.links.get(peer)
            if link is not None and link.theirs:
                continue
            if file.revision > revision:
                gone.append(peer)
        return gone

    def _clear_barriers(self, *, actor: str, state: _ActorState) -> None:
        index = self._store.index()
        for peer, (our_rev, their_rev) in list(state.barrier.items()):
            mine = index.files.get(actor)
            theirs = index.files.get(peer)
            if mine is None or theirs is None:
                continue
            if peer in index.absent:
                continue
            if (
                mine.revision > our_rev
                and peer not in mine.entries
                and theirs.revision > their_rev
                and actor not in theirs.entries
            ):
                del state.barrier[peer]
                state.reclaimed.pop(peer, None)

    def _emit_established(self, actor: str, now_ms: int) -> None:
        with self._mem:
            st = self._state(actor)
            view = self._view(actor, now_ms)
            index = self._store.index()
            minted: list[tuple[str, LinkEntry | None, str]] = []
            for peer, link in view.links.items():
                if link.state != "established" or peer in st.established:
                    continue
                file = index.files.get(peer)
                st.established[peer] = file.revision if file is not None else 0
                minted.append((peer, link.entry, st.minted_side.pop(peer, "minter")))
        for peer, entry, side in minted:
            self._emit(
                EVENT_LINK_MINTED,
                actor,
                now_ms,
                {
                    "peer": peer,
                    "lifecycle": entry.lifecycle if entry is not None else "regular",
                    "taskId": entry.task_id if entry is not None else None,
                    "revision": entry.revision if entry is not None else 0,
                    "mintedAtMs": entry.minted_at_ms if entry is not None else now_ms,
                    "atMs": now_ms,
                    "side": side,
                },
            )


    # -- internals: reclaim ---------------------------------------------

    def _reclaim(
        self, actor: str, peer: str, reason: str, now_ms: int
    ) -> dict[str, int]:
        inbound = self._inbox.inbound_live(actor, peer)
        rows = [
            row
            for row in self._inbox.ledger_snapshot(now_ms)
            if row.actor == actor
            and row.direction == "out"
            and row.peer == peer
            and row.state == "open"
        ]
        cancel = {
            row.message_id: ipc_errors.LINK_RECLAIMED
            for row in rows
            if row.sent_at_ms is None
        }
        release = tuple(
            (actor, row.message_id) for row in rows if row.sent_at_ms is not None
        )
        index = self._store.index()
        with self._mem:
            st = self._state(actor)
            # Rework 3 (Q2b): the withdrawal goes through the one pending slot
            # instead of a direct ``store.write``, so the slot keeps covering
            # the file until the store holds the intent and a concurrent mint
            # can never build on the pre-withdrawal file again.  ``intents``
            # and ``index`` are lock-free memory reads (§3.5).
            base_file = (
                st.pending_file
                or self._store.intents().get(actor)
                or index.files.get(actor)
            )
            if base_file is not None and peer in base_file.entries:
                st.pending_file = model.without_reference(base_file, peer)
            view = self._view(actor, now_ms)
            drain = set(policy.residue(view, peer=peer).drain_ack)
            drain.update(row.message_id for row in inbound)
            # Censored requests are still live residue even though they no
            # longer occupy W (B9/B10).
            drain.update(
                message_id
                for message_id, record in st.censored_inbound.items()
                if record.requester == peer
            )
            for message_id in drain:
                st.drained.add(message_id)
                st.admitted.discard(message_id)
                st.pending_inbound.pop(message_id, None)
                st.censored_inbound.pop(message_id, None)
            released = {message_id for _actor, message_id in release}
            released.update(cancel)
            st.outbound = [
                item for item in st.outbound if item.message_id not in released
            ]
            st.released_outbound |= released
            st.reservations = [item for item in st.reservations if item[0] != peer]
            entry = view.links.get(peer)
            lifecycle = (
                entry.entry.lifecycle if entry is not None and entry.entry else None
            )
            task_ids = sorted(st.task_bindings.pop(peer, set()))
            ours = (
                base_file.entries[peer].revision
                if base_file is not None and peer in base_file.entries
                else 0
            )
            theirs = index.files.get(peer)
            st.barrier[peer] = (ours, theirs.revision if theirs is not None else 0)
            st.reclaimed[peer] = now_ms
            st.established.pop(peer, None)
            st.minted_side.pop(peer, None)
            st.recent_reclaims_ms.append(now_ms)
        update = LedgerUpdate(
            drain={actor: tuple(sorted(drain))} if drain else {},
            cancel_outbound=cancel,
            release_outbound=release,
        )
        if drain:
            with self._mem:
                self._state(actor).pending_drain.update(drain)
        try:
            self._inbox.apply(update, now_ms=now_ms)
        except Exception as error:  # noqa: BLE001 - drain is retried next pass
            self._log("link.drain_failed", errorType=type(error).__name__)
        else:
            with self._mem:
                self._state(actor).pending_drain.difference_update(drain)
        self._emit(
            EVENT_LINK_RECLAIMED,
            actor,
            now_ms,
            {
                "peer": peer,
                "lifecycle": lifecycle or "regular",
                "taskId": task_ids[0] if task_ids else None,
                "reason": reason,
                "drained": len(drain),
                "drainedMessageIds": sorted(drain)[:_DRAINED_ID_LIMIT],
                "released": len(release),
                "cancelled": len(cancel),
                "atMs": now_ms,
            },
        )
        self.kick()
        self._notify()
        return {
            "drained": len(drain),
            "released": len(release),
            "cancelled": len(cancel),
        }
