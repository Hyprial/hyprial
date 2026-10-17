"""Queries, admin operations and the change-source subscription.

Internal module of ``hyprial.daemon.impl.links.service`` (spec §9.5).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import hyprial.daemon.impl.links.model as model
import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.ports import (
    TOP_FIELD_CAPACITY_W,
    TOP_FIELD_OUTBOUND_IN_FLIGHT,
    Allow,
    Hold,
)
from hyprial.kernel import DaemonRequestError, ipc_errors, parse_agent_uri


class ViewsMixin:
    """Queries, admin operations and the change-source subscription."""

    # -- LinkQueries / LinkAdmin / LinkChangeSource ---------------------

    def list_links(self, actor: str) -> Mapping[str, Any]:
        now = self._clock_ms()
        index = self._store.index()
        committed_file = index.files.get(actor)
        committed = set(committed_file.entries) if committed_file is not None else set()
        with self._mem:
            st = self._state(actor)
            view = self._view(actor, now)
            links = []
            for peer, link in sorted(view.links.items()):
                entry = link.entry
                links.append(
                    {
                        "peer": peer,
                        "state": link.state,
                        "cwnd": (
                            view.limits.cwnd.get(peer)
                            if link.state == "established"
                            else None
                        ),
                        "inFlight": sum(1 for item in view.outbound if item.peer == peer),
                        "lifecycle": entry.lifecycle if entry is not None else None,
                        "taskId": entry.task_id if entry is not None else None,
                        "taskIds": sorted(st.task_bindings.get(peer, ())),
                        "mintedAtMs": entry.minted_at_ms if entry is not None else None,
                        "lastUsedAtMs": (
                            entry.last_used_ms if entry is not None else None
                        ),
                        "revision": entry.revision if entry is not None else None,
                        "mine": link.mine,
                        "theirs": link.theirs,
                        "committed": peer in committed,
                    }
                )
            held = [
                {
                    "messageId": message_id,
                    "target": row.target,
                    "holdReason": row.hold_reason,
                    "detail": st.held_detail.get(message_id, ""),
                    "heldMs": max(0, now - row.created_at_ms),
                }
                for message_id, row in sorted(
                    st.held.items(), key=lambda item: item[1].created_at_ms
                )
                if row.reply_to is None
            ]
            owner = {peer: (model.owner_of(peer) or "") for peer in view.links}
            return {
                "actor": actor,
                "orgAvailable": index.org_available,
                "k": view.limits.k,
                "W": view.limits.w,
                "inFlight": len(st.outbound),
                "owner": owner,
                "links": links,
                "held": held,
            }

    def stats(self, actor: str) -> Mapping[str, Any]:
        now = self._clock_ms()
        with self._mem:
            state = self._state(actor)
            view = self._view(actor, now)
            limits = view.limits
            node = limits.node
            links = []
            for peer in sorted(limits.links):
                window = limits.links[peer]
                links.append(
                    {
                        "peer": peer,
                        "rttMinMs": window.t_min_ms,
                        "rttMaxMs": window.t_max_ms,
                        "rttMs": window.t_bar_ms,
                        "cwnd": limits.cwnd.get(peer),
                        "inFlight": sum(
                            1 for item in view.outbound if item.peer == peer
                        ),
                    }
                )
            return {
                "actor": actor,
                "nActive": limits.n_active,
                "k": limits.k,
                "W": limits.w,
                "windowObserved": state.window_observed,
                "wTarget": node.w_target,
                "tMinMs": node.t_min_ms,
                "tMaxMs": node.t_max_ms,
                "tBarMs": node.t_bar_ms,
                "muMax": (
                    None if node.mu_max_per_ms is None else node.mu_max_per_ms * 1000.0
                ),
                "diff": limits.diff,
                "grad": limits.grad,
                "periodMs": limits.period_ms,
                "links": links,
            }

    def add(self, *, actor: str, peer: str, now_ms: int) -> Mapping[str, Any]:
        if (
            parse_agent_uri(peer) is None
            or peer == actor
            or model.is_owner_pair(actor, peer)
            or self._is_internal(peer)
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"cannot add a link to {peer!r}",
                data={"actor": actor, "peer": peer},
            )
        with self._mem:
            st = self._state(actor)
            view = self._view(actor, now_ms)
            existing = view.links.get(peer)
            if existing is not None and existing.mine:
                return {
                    "actor": actor,
                    "peer": peer,
                    "state": existing.state,
                    "revision": (
                        existing.entry.revision if existing.entry is not None else None
                    ),
                    "committed": self._is_committed(actor, peer),
                }
            head = self._queue_head(st, actor, peer)
            if head is not None:
                return {
                    "actor": actor,
                    "peer": peer,
                    "state": "absent",
                    "written": False,
                    "queued": False,
                    "holdReason": head[0],
                    "detail": head[1],
                }
            decision = policy.admit(
                view,
                sender=actor,
                target=peer,
                intent="request",
                reply_to=None,
                now_ms=now_ms,
            )
        if isinstance(decision, Allow):
            with self._mem:
                self._state(actor).last_used[peer] = now_ms
            return {"actor": actor, "peer": peer, "state": "established"}
        if isinstance(decision, Hold):
            return {
                "actor": actor,
                "peer": peer,
                "state": "absent",
                "written": False,
                "queued": False,
                "holdReason": decision.reason,
                "detail": decision.detail,
            }
        if decision.evict:
            with self._mem:
                self._state(actor).pending_reclaims.append(
                    (decision.evict, "evicted", now_ms)
                )
        revision = self._mint(actor, peer, now_ms, lifecycle="regular")
        with self._mem:
            # An operator asked for this reference explicitly; the best-effort
            # retraction for a minted message must not undo it.
            self._state(actor).explicit.add(peer)
        return {
            "actor": actor,
            "peer": peer,
            "state": "pending",
            "committed": False,
            "revision": revision,
            "written": False,
        }

    def drop(self, *, actor: str, peer: str, now_ms: int) -> Mapping[str, Any]:
        with self._mem:
            view = self._view(actor, now_ms)
            link = view.links.get(peer)
            if link is None or not link.mine:
                return {"actor": actor, "peer": peer, "state": "absent"}
            # Accept the request, do not write it: the service thread performs
            # the file retraction, residue drain and apply (§3.5, P-3).
            self._state(actor).pending_reclaims.append((peer, "dropped", now_ms))
        self.kick()
        return {
            "actor": actor,
            "peer": peer,
            "state": "reclaiming",
            "written": False,
        }

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe


    # -- composition only ----------------------------------------------

    def top_fields(self, actor: str) -> Mapping[str, int | None]:
        if not self._is_local_agent(actor):
            return {TOP_FIELD_CAPACITY_W: None, TOP_FIELD_OUTBOUND_IN_FLIGHT: None}
        with self._mem:
            st = self._state(actor)
            self._ensure_controller(st, self._clock_ms())
            if st.limits is None:
                return {
                    TOP_FIELD_CAPACITY_W: None,
                    TOP_FIELD_OUTBOUND_IN_FLIGHT: None,
                }
            return {
                TOP_FIELD_CAPACITY_W: st.limits.w,
                TOP_FIELD_OUTBOUND_IN_FLIGHT: len(st.outbound),
            }

    def _is_committed(self, actor: str, peer: str) -> bool:
        committed = self._store.index().files.get(actor)
        return committed is not None and peer in committed.entries
