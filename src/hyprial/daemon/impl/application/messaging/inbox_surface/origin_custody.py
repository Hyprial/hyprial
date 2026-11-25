"""Application-side origin claimant classification and session-liveness closure.

Decision record:
- REVIEW3 fix: session claim liveness binds ``(actor, session_ref)`` together
  when consulting ``SessionDomain.read_sessions()`` so reusing ``sessionRef``
  on another actor never keeps a dead actor's claim alive.
- Lock ordering: ``_origin_session_current`` reads only the leaf-lock session
  projection (``read_sessions()``) and never calls back into inbox.
- Non-holding readers (local principal and ``worker-ref`` without an active
  headless claim) yield to other holders without minting new claims.
- I5-3B P1 fixes: interactive admission is fail-closed when ``_cell_delivery``
  or callable ``interactive_admission`` is absent; cursor replay checks whether
  any inbox member in the cursor still holds a live session claim owned by
  the calling ``(actor, sessionRef)``.
"""

from __future__ import annotations

from collections.abc import Mapping
import inspect
from typing import Any

from hyprial.daemon.impl.inbox.contracts.origin import (
    OriginClaimant,
    OriginOwnerKind,
)
from hyprial.kernel import parse_agent_uri


def _accepts_kwarg(fn: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        param.name == name or param.kind is inspect.Parameter.VAR_KEYWORD
        for param in parameters
    )


def _same_actor_or_owner_relocated(left: str, right: str) -> bool:
    if left == right:
        return True
    parsed_left = parse_agent_uri(left)
    parsed_right = parse_agent_uri(right)
    if parsed_left is None or parsed_right is None:
        return False
    return parsed_left[1:] == parsed_right[1:]


class _OriginCustodyMixin:
    """Application helper mixin; state owner is DaemonApplication."""

    def _origin_session_current(self, actor: str, session_ref: str) -> bool:
        """Leaf-lock read proving ``(actor, session_ref)`` still holds a session."""

        domains = getattr(self, "_agent_session_domains", None)
        if domains is None:
            return False
        session_domain = getattr(domains, "session", None)
        reader = getattr(session_domain, "read_sessions", None)
        if not callable(reader):
            return False
        return any(
            session.session_ref == session_ref
            and _same_actor_or_owner_relocated(session.actor, actor)
            for session in reader()
        )

    def _classify_origin_claimant(
        self, caller: Any, params: Mapping[str, object]
    ) -> tuple[OriginClaimant, bool]:
        """Classify IPC caller into ``(OriginClaimant, is_interactive)``."""

        session_ref = params.get("sessionRef")
        if (
            getattr(caller, "via", None) == "session"
            and isinstance(session_ref, str)
            and session_ref
        ):
            worker_ref_fn = getattr(self, "_worker_session_ref", None)
            if callable(worker_ref_fn) and worker_ref_fn(caller.subject) == session_ref:
                return OriginClaimant.worker_ref(caller.subject), False
            return OriginClaimant.session(caller.subject, session_ref), True
        return OriginClaimant.principal(), False

    def _admit_interactive_acquisition(
        self, actor: str, *, is_interactive: bool
    ) -> bool:
        """Gate interactive inbox acquisition on the cell entry snapshot."""

        if not is_interactive:
            return True
        port = getattr(self, "_cell_delivery", None)
        if port is None:
            return False
        admit = getattr(port, "interactive_admission", None)
        if not callable(admit):
            return False
        return admit(actor) is not None

    def _live_session_claimed_ids(
        self, claimant: OriginClaimant, message_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Return ``message_ids`` where ``claimant`` still holds a live claim."""

        if claimant.owner_kind is not OriginOwnerKind.SESSION or not message_ids:
            return ()
        reader = getattr(getattr(self, "_inbox", None), "read_origin_claims", None)
        if not callable(reader):
            return ()
        claims = reader()
        owned: list[str] = []
        for message_id in message_ids:
            claim = claims.get(message_id)
            if claim is None:
                continue
            if (
                claim.owner_kind is OriginOwnerKind.SESSION
                and claim.owner_id == claimant.owner_id
                and _same_actor_or_owner_relocated(
                    claim.owner_actor, claimant.owner_actor
                )
                and self._origin_session_current(claim.owner_actor, claim.owner_id)
            ):
                owned.append(message_id)
        return tuple(owned)

    def _fetch_pending_with_origin(
        self,
        fetch_pending: Any,
        key: str,
        *,
        only: frozenset[str] | None,
        origin_claim: OriginClaimant,
    ) -> tuple[tuple[Any, ...], bool]:
        """Call ``fetch_pending`` passing ``origin_claim`` when supported."""

        kwargs: dict[str, Any] = {}
        if only is not None and _accepts_kwarg(fetch_pending, "only"):
            kwargs["only"] = only
        supports_origin = _accepts_kwarg(fetch_pending, "origin_claim")
        if supports_origin:
            kwargs["origin_claim"] = origin_claim
        return tuple(fetch_pending(key, **kwargs)), supports_origin


__all__ = ["_OriginCustodyMixin"]
