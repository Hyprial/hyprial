"""Presence projections shared by status, targets, and delivery views."""

from __future__ import annotations

from collections.abc import Callable

from hyprial.daemon.impl.transport import LivelinessDirectory


class _LocalPresence:
    """Presence for ps/targets/delivery, all sharing this one judgment call.

    Raw zenoh liveliness is trusted as-is for this node's own identity, for
    daemon-managed harness workers (their token tracks a real supervised
    subprocess -- see ``_reconcile_harness_actors``), and for genuinely remote
    peers. For a daemon-owned interactive-session route, the raw token alone
    cannot tell a dead connector from a live one (see the TTL constant's
    docstring), so ``interactive_liveness`` -- when it recognizes the actor --
    overrides the raw token with a real heartbeat-freshness verdict.
    """

    def __init__(
        self,
        inner: LivelinessDirectory,
        actor: str,
        *,
        interactive_liveness: Callable[[str], bool | None] | None = None,
    ) -> None:
        self.inner = inner
        self.actor = actor
        self._interactive_liveness = interactive_liveness

    def actor_online(self, actor: str) -> bool:
        if actor == self.actor:
            return True
        if not self.inner.actor_online(actor):
            return False
        if self._interactive_liveness is not None:
            verdict = self._interactive_liveness(actor)
            if verdict is not None:
                return verdict
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return self.inner.online_mailboxes()

    def online_actors(self) -> tuple[str, ...]:
        candidates = self.raw_online_actors()
        if self._interactive_liveness is None:
            return candidates
        return tuple(sorted(actor for actor in candidates if self.liveness_keeps(actor)))

    def liveness_keeps(self, actor: str) -> bool:
        """The interactive-liveness half of ``online_actors``, for one candidate.

        Identity-first batch paths (``hosts``, ``org.fetch``) reuse exactly
        this predicate on the host-kind survivors of their filter, so their
        output stays byte-identical to ``online_actors()``-then-filter --
        including the name-collision case where a local worker's bare name
        equals a remote node id and a stopped worker answers False
        (hyprial-developer 2026-09-15 批复①:逐字保真,不依赖「病态配置不会
        出现」).  Inside a request snapshot the verdict is table lookups --
        no store lock, no SQLite -- so the recheck costs nothing per host.
        """

        if actor == self.actor or self._interactive_liveness is None:
            return True
        return self._interactive_liveness(actor) is not False

    def raw_online_actors(self) -> tuple[str, ...]:
        """The candidate set ``online_actors`` starts from, before any liveness.

        Host-identity batch paths (``hosts`` rows, ``org.fetch`` candidate
        enumeration) filter on identity first and only then pay the per-actor
        interactive-liveness verdict for the host-kind survivors: a bare
        node id normally carries no local verdict (``AgentLiveness.verdict``
        answers None for one), and the survivors' recheck is what keeps the
        name-collision case byte-faithful.  Same union as ``online_actors``
        -- this node's own token plus the raw directory -- so the two views
        cannot drift apart on the candidate set itself.
        """

        return tuple(sorted({self.actor, *self.inner.online_actors()}))
