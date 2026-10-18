"""``LinkGate``, ``LinkSendObserver`` and ``LinkLedgerSink`` entry points.

Internal module of ``hyprial.daemon.impl.links.service`` (spec §9.5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import hyprial.daemon.impl.links.model as model
import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.controller import (
    ControllerOutput,
    DwellSample,
    ReplySample,
    RttSample,
)
from hyprial.daemon.impl.links.model import LinkEntry, LinkFile
from hyprial.daemon.impl.links.ports import (
    Allow,
    Hold,
    SendDecision,
    SettleHint,
)
from hyprial.kernel import DaemonRequestError, ipc_errors, uri_scheme

from ._base import RESERVATION_TTL_MS, _ActorState


class GateMixin:
    """The gate, observer and ledger-sink methods of ``LinkService``."""

    # -- LinkGate -------------------------------------------------------

    def admit_send(
        self,
        *,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        now_ms: int,
    ) -> SendDecision:
        if uri_scheme(sender) != "agent":
            return Allow(peer=None)
        if not model.is_link_endpoint(sender) or not self._is_local_agent(sender):
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                f"sender {sender!r} is not a managed agent of this node",
                data={"sender": sender},
            )
        extra: dict[str, policy.PendingRequest] | None = None
        if reply_to:
            extra = self._lookup_reply_request(sender, reply_to)
        # H1: bounds skipped because ``window_mode`` is ``observe``; counted and
        # logged once ``_mem`` is released, so the lock never wraps a log write.
        observed: list[tuple[str, str]] = []
        # The ``user:`` branch reserves for itself and answers directly; every
        # other branch replies through ``_after_decision``, which reserves too.
        reserved = False
        with self._mem:
            st = self._state(sender)
            if reply_to and reply_to in st.drained:
                raise _reclaimed(reply_to, target)
            view = self._view(sender, now_ms, for_reply=bool(reply_to), extra=extra)
            if reply_to:
                request = view.pending_inbound.get(reply_to)
                if request is None:
                    raise DaemonRequestError(
                        ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                        f"no open request {reply_to!r} to reply to",
                    )
                if uri_scheme(target) != "route" and request.requester != target:
                    raise DaemonRequestError(
                        ipc_errors.INVALID_ARGUMENT,
                        f"request {reply_to!r} came from {request.requester!r}, "
                        f"not {target!r}",
                    )
            if self._exempt_target(sender, target):
                return Allow(peer=None)
            if uri_scheme(target) == "user":
                # Ruling 53 bounds only a NEW request; a validated reply closes
                # an open item and must never be refused (§2.2 step 1.4, P-1).
                if (
                    reply_to is None
                    and not model.is_owner_pair(sender, target)
                    and policy.unclosed(view) >= view.limits.w
                ):
                    if self._window_mode == "enforce":
                        raise DaemonRequestError(
                            ipc_errors.LINK_WINDOW_FULL,
                            f"window W is full for {sender!r}",
                            data={"target": target, "detail": "W"},
                        )
                    observed.append(("W", "user"))
                if reply_to is None:
                    st.reservations.append((target, now_ms))
                reserved = True
                decision = Allow(peer=None)
            else:
                if not reply_to:
                    head = self._queue_head(st, sender, target)
                    if head is not None:
                        if (
                            head[0] == ipc_errors.LINK_WINDOW_FULL
                            and self._window_mode != "enforce"
                        ):
                            # A durable window hold from an ``enforce`` run must
                            # not keep holding now that the switch observes.
                            observed.append((head[1], "queued"))
                        else:
                            return Hold(head[0], target, head[1])
                decision = policy.admit(
                    view,
                    sender=sender,
                    target=target,
                    intent=intent,
                    reply_to=reply_to,
                    now_ms=now_ms,
                )
                if isinstance(decision, Allow) and decision.window_observed:
                    observed.append((decision.window_observed, "send"))
        for bound, site in observed:
            self._window_observed(sender, target, bound, site)
        if reserved:
            return decision
        return self._after_decision(st, sender, target, decision, reply_to, now_ms)

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        if self._is_internal(sender):
            return True
        index = self._store.index()
        if index.known(sender):
            return True
        if self._is_local_agent(sender) and index.org_available:
            return sender in index.files
        return False

    def admit_dispatch(
        self,
        *,
        recipient: str,
        sender: str,
        message_id: str,
        now_ms: int,
        intent: str = "request",
        reply_to: str | None = None,
    ) -> bool:
        if self._is_internal(sender) or self._is_internal(recipient):
            return True
        observed = False
        with self._mem:
            st = self._state(recipient)
            credit = st.reply_credits.get(message_id)
            if credit is not None and credit == (sender, reply_to):
                st.admitted.add(message_id)
                return True
            if message_id in st.admitted:
                return True
            if message_id in st.settled:
                if message_id in st.drained:
                    # R23 / E26-08: a drained id is a reclaimed link's
                    # residue.  A drain books the terminal for a request the
                    # gate may never have admitted (it was never injected),
                    # so the retained-row fallback of ``message.read_body``
                    # must not read its body back.  Only rows that settled
                    # through processing (closed/acked/expired) keep Ruling
                    # 70's read-only pass-through.
                    return False
                # Ruling 70: a closed/acked/expired id stays settled
                # for the ledger's retention window; re-asking admits it for
                # good without a window seat or an in-flight count.  Its
                # terminal ledger row is what a read_body fallback reads.
                return True
            view = self._view(recipient, now_ms)
            admitted = policy.admit_dispatch(
                view,
                sender=sender,
                message_id=message_id,
                now_ms=now_ms,
                intent=intent,
                reply_to=reply_to,
            )
            if admitted:
                st.admitted.add(message_id)
                if not reply_to:
                    st.pending_inbound[message_id] = policy.PendingRequest(
                        message_id, sender, now_ms
                    )
                observed = policy.dispatch_window_observed(
                    view,
                    sender=sender,
                    message_id=message_id,
                    intent=intent,
                    reply_to=reply_to,
                )
        if observed:
            # The inbox actor thread must not do log I/O (LinkGate contract).
            self._window_observed(recipient, sender, "W", "dispatch", log=False)
        return admitted

    def settle_hint(self, *, actor: str, message_id: str) -> SettleHint:
        state = self._states.get(actor)
        if state is not None and message_id in state.drained:
            return "ack"
        return "keep"

    def submitted(
        self,
        *,
        sender: str,
        target: str,
        message_id: str,
        reply_to: str | None,
        hold_reason: str | None,
        now_ms: int,
    ) -> None:
        kind = "held" if hold_reason else ("reply" if reply_to else "request")
        key = (sender, message_id, kind)
        wake = False
        with self._mem:
            if key in self._observed:
                return
            self._observed.add(key)
            st = self._state(sender)
            if hold_reason:
                st.held_pending[message_id] = (target, hold_reason, reply_to)
                wake = True
            elif reply_to:
                request = st.pending_inbound.pop(reply_to, None)
                censored = st.censored_inbound.pop(reply_to, None)
                if request is not None:
                    st.dwell.append(
                        DwellSample(
                            at_ms=now_ms,
                            dwell_ms=max(1, now_ms - request.received_at_ms),
                        )
                    )
                elif censored is not None:
                    pass
                st.replies.append(ReplySample(at_ms=now_ms, direction="sent"))
                st.last_used[target] = now_ms
                wake = True
            else:
                _consume_reservation(st, target)
                st.outbound.append(policy.OutboundRequest(message_id, target, now_ms))
                st.last_used[target] = now_ms
                wake = True
        if wake:
            self.kick()

    def injected(
        self,
        *,
        actor: str,
        message_id: str,
        sender: str,
        intent: str,
        reply_to: str | None,
        received_at_ms: int,
    ) -> None:
        if reply_to is not None:
            return
        if uri_scheme(sender) != "agent" and intent != "request":
            return
        with self._mem:
            st = self._state(actor)
            st.admitted.add(message_id)
            st.pending_inbound[message_id] = policy.PendingRequest(
                message_id, sender, received_at_ms
            )

    def reply_received(
        self,
        *,
        actor: str,
        reply_message_id: str,
        reply_to: str,
        sender: str,
        received_at_ms: int,
    ) -> None:
        wake = False
        with self._mem:
            st = self._state(actor)
            if reply_to in st.released_outbound:
                return
            match = next(
                (
                    item
                    for item in st.outbound
                    if item.message_id == reply_to and item.peer == sender
                ),
                None,
            )
            if match is None:
                return
            st.outbound.remove(match)
            st.rtt.append(
                RttSample(
                    peer=sender,
                    at_ms=received_at_ms,
                    rtt_ms=max(1, received_at_ms - match.sent_at_ms),
                )
            )
            st.replies.append(
                ReplySample(at_ms=received_at_ms, peer=sender, direction="received")
            )
            st.last_used[sender] = received_at_ms
            st.reply_credits[reply_message_id] = (sender, reply_to)
            wake = True
        if wake:
            self.kick()

    def discard(self, *, actor: str, message_ids: Any) -> None:
        with self._mem:
            st = self._state(actor)
            for message_id in message_ids:
                st.admitted.discard(message_id)
                st.pending_inbound.pop(message_id, None)
                st.censored_inbound.pop(message_id, None)


    # -- internals: view ------------------------------------------------

    def _view(
        self,
        actor: str,
        now_ms: int,
        *,
        for_reply: bool = False,
        extra: Mapping[str, policy.PendingRequest] | None = None,
        tentative: Sequence[policy.OutboundRequest] = (),
    ) -> policy.NodeView:
        st = self._state(actor)
        self._ensure_controller(st, now_ms)
        index = self._store.index()
        committed = index.files.get(actor)
        mine = st.pending_file or self._store.intents().get(actor) or committed
        raw = model.links_of(actor, mine, index.files, reclaimed=set(st.reclaimed))
        committed_peers = set(committed.entries) if committed is not None else set()
        links: dict[str, model.Link] = {}
        for peer, link in raw.items():
            entry = link.entry
            if entry is not None:
                # last_used is a local runtime fact that is not written back;
                # overlay it here and treat the zero sentinel as minting time
                # so legacy files do not display 1970 or compare as epoch.
                effective = max(entry.last_used_ms, entry.minted_at_ms)
                effective = max(effective, st.last_used.get(peer, 0))
                link = replace(
                    link,
                    entry=replace(entry, last_used_ms=effective),
                )
            if link.mine and peer not in committed_peers:
                links[peer] = model.Link(
                    actor=actor,
                    peer=peer,
                    state="pending",
                    mine=True,
                    theirs=link.theirs,
                    entry=link.entry,
                )
            else:
                links[peer] = link
        pending = dict(st.pending_inbound)
        if extra:
            pending.update(extra)
        if for_reply:
            for message_id, record in st.censored_inbound.items():
                if message_id not in pending:
                    pending[message_id] = policy.PendingRequest(
                        message_id, record.requester, record.received_at_ms
                    )
        outbound = list(st.outbound) + list(tentative)
        live_reservations = [
            item
            for item in st.reservations
            if now_ms - item[1] < RESERVATION_TTL_MS
        ]
        if len(live_reservations) != len(st.reservations):
            st.reservations = live_reservations
        for position, (target, reserved_at) in enumerate(live_reservations):
            outbound.append(
                policy.OutboundRequest(f"reservation:{position}", target, reserved_at)
            )
        limits = st.limits
        assert limits is not None
        rtt_max = {
            peer: window.t_max_ms
            for peer, window in limits.links.items()
            if window.t_max_ms is not None
        }
        return policy.NodeView(
            actor=actor,
            links=links,
            pending_inbound=pending,
            drained=st.drained,
            outbound=outbound,
            limits=limits,
            recent_reclaims_ms=st.recent_reclaims_ms,
            rtt_max_ms=rtt_max,
            org_available=index.org_available,
            constants=self._constants,
            admitted_ids=st.admitted,
            peer_full=self._peer_full(links, limits),
            window_mode=self._window_mode,
        )

    def _peer_full(
        self, links: Mapping[str, model.Link], limits: ControllerOutput
    ) -> frozenset[str]:
        index = self._store.index()
        full: set[str] = set()
        for peer, link in links.items():
            if not link.mine or link.theirs:
                continue
            file = index.files.get(peer)
            if file is None:
                continue
            regular = sum(
                1 for entry in file.entries.values() if entry.lifecycle == "regular"
            )
            if regular >= limits.k:
                full.add(peer)
        return frozenset(full)

    def _queue_head(
        self, st: _ActorState, sender: str, target: str
    ) -> tuple[str, str] | None:
        rows = sorted(
            (
                (row.created_at_ms, message_id, row)
                for message_id, row in st.held.items()
                if row.sender == sender
                and row.target == target
                and row.reply_to is None
            ),
            key=lambda item: (item[0], item[1]),
        )
        if rows:
            row = rows[0][2]
            return (row.hold_reason, st.held_detail.get(row.message_id, ""))
        pending = sorted(
            (target_id, value)
            for target_id, value in st.held_pending.items()
            if value[0] == target and value[2] is None
        )
        if pending:
            _message_id, value = pending[0]
            return (value[1], "")
        return None

    def _exempt_target(self, sender: str, target: str) -> bool:
        if target == sender:
            return True
        if uri_scheme(target) == "route":
            return True
        return self._is_internal(target)


    # -- internals: decisions -------------------------------------------

    def _after_decision(
        self,
        st: _ActorState,
        sender: str,
        target: str,
        decision: SendDecision,
        reply_to: str | None,
        now_ms: int,
    ) -> SendDecision:
        if isinstance(decision, Allow):
            with self._mem:
                if reply_to is None:
                    st.reservations.append((target, now_ms))
                st.last_used[target] = now_ms
            return decision
        if isinstance(decision, Hold):
            return decision
        if decision.evict:
            # Reclaiming is inbox I/O and publishing; the service thread does
            # it during a pass (§3.5, P-3).
            with self._mem:
                self._state(sender).pending_reclaims.append(
                    (decision.evict, "evicted", now_ms)
                )
        self._mint(sender, target, now_ms, lifecycle="regular")
        with self._mem:
            st.minted_side[target] = "minter"
            st.last_used[target] = now_ms
        return decision

    def _mint(
        self,
        actor: str,
        peer: str,
        now_ms: int,
        *,
        lifecycle: str = "regular",
        task_id: str | None = None,
    ) -> int:
        """Record a one-sided reference as a pending local write (any thread).

        The IPC, admin and PAC threads must not call ``LinkStore.write``
        (§3.5): they only stage the intended file here and let the service
        thread drain it.  The staged file is what ``_view`` shows, so callers
        see a pending (uncommitted) link at once.
        """

        with self._mem:
            revision = self._mint_locked(
                actor, peer, now_ms, lifecycle=lifecycle, task_id=task_id
            )
        self.kick()
        return revision

    def _mint_locked(
        self,
        actor: str,
        peer: str,
        now_ms: int,
        *,
        lifecycle: str = "regular",
        task_id: str | None = None,
    ) -> int:
        """``_mint`` for callers already holding ``_mem``; never kicks."""

        st = self._state(actor)
        index = self._store.index()
        base = (
            st.pending_file
            or self._store.intents().get(actor)
            or index.files.get(actor)
            or LinkFile(actor=actor, revision=0, entries={})
        )
        current = base.entries.get(peer)
        if current is not None:
            return current.revision
        entry = LinkEntry(
            peer=peer,
            minted_at_ms=now_ms,
            revision=base.revision + 1,
            lifecycle=lifecycle,  # type: ignore[arg-type]
            task_id=task_id,
        )
        st.pending_file = model.with_reference(base, entry)
        return st.pending_file.revision

    def _lookup_reply_request(
        self, actor: str, reply_to: str
    ) -> dict[str, policy.PendingRequest] | None:
        with self._mem:
            st = self._state(actor)
            if reply_to in st.pending_inbound or reply_to in st.censored_inbound:
                return None
        row = self._inbox.inbound_row(reply_to)
        if row is None or row.recipient != actor or row.reply_to is not None:
            return None
        return {
            reply_to: policy.PendingRequest(reply_to, row.sender, row.received_at_ms)
        }




def _reclaimed(message_id: str, peer: str) -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.LINK_RECLAIMED,
        "the link for this reply has been reclaimed; the residue is drained",
        data={"message_id": message_id, "peer": peer},
    )

def _consume_reservation(state: _ActorState, target: str) -> None:
    for position, (peer, _at) in enumerate(state.reservations):
        if peer == target:
            del state.reservations[position]
            return
