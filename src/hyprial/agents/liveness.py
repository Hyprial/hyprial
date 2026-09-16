"""One liveness model for agents, replacing the three that disagreed (A7).

Before this module ``ps`` answered "is it alive?" three unrelated ways:

1. ``connectors[]`` reported the harness supervisor's ``running`` flag for a
   daemon-managed headless worker;
2. ``interactiveSessions[].online`` reported a heartbeat-gated verdict for a
   daemon-owned interactive route;
3. ``targets[].status`` reported the string ``"online"`` — a **literal**, built
   from a list that only contained online actors, so it was structurally
   incapable of ever saying offline.

Three answers, no single owner, and one of them could not be wrong because it
was not a judgement at all.  This class is the single owner.  It does not
invent a fourth signal: it consumes the two real ones (a supervised
subprocess's ``running`` flag; the per-actor daemon-contact heartbeat added by
the connector-liveness fix) and turns them into one verdict that every caller
— ``ps``, ``targets``, and delivery's ``is_online`` — reads.

It also owns the **runtime binding**: which harness and which session an agent
is currently running under.  That deliberately does not live on
:class:`~hyprial.agents.registry.Agent` (design §3) — an agent may start on
``claude`` and swap to ``pi`` without its record changing — and it is
in-memory per daemon generation on purpose: a freshly started daemon has not
heard from anyone yet and must not assume a persisted registration is a live
process.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AGENT_HEARTBEAT_TTL_SECONDS",
    "RUNTIME_HEADLESS",
    "RUNTIME_INTERACTIVE",
    "AgentAlreadyRunning",
    "AgentBinding",
    "AgentLiveness",
]

#: Freshness window for the per-actor daemon-contact heartbeat.  The production
#: channel poll loop runs at 0.5s with a 5s failure backoff cap, so this
#: generously outlives a few stalled retries while still catching a connector
#: that died without unregistering.  Previously duplicated as a private
#: constant in the daemon; the agent module owns it now (A7 also folds the
#: scattered agent-related configuration together).
AGENT_HEARTBEAT_TTL_SECONDS = 12.0

RUNTIME_HEADLESS = "headless"
RUNTIME_INTERACTIVE = "interactive"


@dataclass(frozen=True, slots=True)
class AgentBinding:
    """Which harness/session currently runs an agent, for one daemon generation."""

    actor: str
    harness: str
    runtime: str
    session_id: str | None
    bound_at_ms: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.harness, self.runtime)

    def to_json(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "harness": self.harness,
            "runtime": self.runtime,
            "sessionId": self.session_id,
            "boundAtMs": self.bound_at_ms,
        }


class AgentAlreadyRunning(RuntimeError):
    """``hyprial start`` may not attach to an agent that already has a connector.

    This is **not** a name conflict.  ``foo`` is one agent and stays one
    agent; the objection is only that something is already speaking for it
    right now.  Refusing -- rather than stopping the old connector
    automatically -- is what keeps a headless worker from losing in-flight
    work silently, and keeps an interactive TUI from being deregistered while
    its window is still open and apparently healthy.

    Guarantees the property the whole change exists for: at most one connector
    speaks for an actor at any time.
    """

    code = "AGENT_ALREADY_RUNNING"

    def __init__(self, actor: str, existing: AgentBinding, harness: str) -> None:
        where = (
            "exit that session"
            if existing.runtime == RUNTIME_INTERACTIVE
            else f"run 'hyprial down {actor.rsplit(':', 1)[-1]}'"
        )
        super().__init__(
            f"agent {actor} already has a live connector on "
            f"{existing.harness} ({existing.runtime}"
            + (f", session {existing.session_id}" if existing.session_id else "")
            + f"); hyprial start will not attach to a running agent. To move it to "
            f"{harness}, stop the current connector first — {where} — then "
            f"start it again. This is not a name conflict: it stays the same "
            f"agent with the same identity, and its previous harness and "
            f"session id are reported on the next start so you can restore "
            f"that conversation by hand."
        )
        self.actor = actor
        self.existing = existing
        self.harness = harness


class AgentLiveness:
    """The single source of truth for "is this agent alive, and on what?"."""

    def __init__(
        self,
        *,
        node_id: str,
        worker_running: Callable[..., bool | None] | None = None,
        interactive_actors: Callable[[], frozenset[str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = AGENT_HEARTBEAT_TTL_SECONDS,
    ) -> None:
        self.node_id = node_id
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        # The probe may be handed the caller's already-loaded desired state
        # (card 259: one snapshot reads the store once); a probe without one
        # is still called with the actor alone, so single-argument probes
        # keep working unchanged.
        self._worker_running = worker_running or (
            lambda _actor, _desired_state=None: None
        )
        self._interactive_actors = interactive_actors or (lambda: frozenset())
        self._bindings: dict[str, AgentBinding] = {}
        self._heartbeats: dict[str, float] = {}

    # -- signals ----------------------------------------------------------

    def touch(self, actor: str) -> None:
        """Record confirmed daemon contact from the connector speaking for ``actor``."""

        self._heartbeats[actor] = self._clock()

    def heartbeat_fresh(self, actor: str) -> bool:
        last_seen = self._heartbeats.get(actor)
        if last_seen is None:
            return False
        return (self._clock() - last_seen) <= self.ttl_seconds

    # -- bindings ---------------------------------------------------------

    def binding(self, actor: str) -> AgentBinding | None:
        return self._bindings.get(actor)

    def bindings(self) -> tuple[AgentBinding, ...]:
        return tuple(self._bindings[actor] for actor in sorted(self._bindings))

    def live_binding(self, actor: str) -> AgentBinding | None:
        """The connector currently speaking for ``actor``, if one is alive.

        A binding proven dead -- a supervised process that is not running, or
        a heartbeat past its TTL -- returns None, so a crashed connector never
        strands its agent's name. A binding whose liveness cannot be
        determined counts as alive: refusing with a message that names the
        remedy is recoverable, whereas letting a second connector onto the
        same URI reintroduces the silent message loss this change exists to
        stop. ``hyprial down`` and ``session.unregister`` both release the
        binding, so the remedy always works.
        """

        existing = self._bindings.get(actor)
        if existing is None:
            return None
        return None if self.verdict(actor) is False else existing

    def displaced_by(
        self, actor: str, *, harness: str, runtime: str
    ) -> AgentBinding | None:
        """The stale binding a rebind to ``(harness, runtime)`` supersedes.

        Uniqueness is **not** decided here.  An agent name is unique because
        only one :class:`~hyprial.agents.registry.Agent` record may hold it, and
        that is enforced at creation, independent of anything running.
        Whether a *live* connector blocks a start is :meth:`live_binding`'s
        question, answered by the caller before this one is asked.

        By the time this is consulted the previous connector is already known
        to be dead, so what it identifies is leftovers: the desired-state entry
        of a crashed harness that would otherwise be restored beside the new
        connector on the next daemon start, putting two of them back on one
        URI.  Same harness *and* same runtime kind supersedes nothing and stays
        idempotent.
        """

        existing = self._bindings.get(actor)
        if existing is None or existing.key == (harness, runtime):
            return None
        return existing

    def bind(
        self,
        actor: str,
        *,
        harness: str,
        runtime: str,
        session_id: str | None = None,
    ) -> AgentBinding:
        """Record which harness/session now speaks for ``actor``."""

        binding = AgentBinding(
            actor=actor,
            harness=harness,
            runtime=runtime,
            session_id=session_id,
            bound_at_ms=time.time_ns() // 1_000_000,
        )
        self._bindings[actor] = binding
        return binding

    def release(self, actor: str) -> AgentBinding | None:
        self._heartbeats.pop(actor, None)
        return self._bindings.pop(actor, None)

    # -- verdict ----------------------------------------------------------

    def verdict(self, actor: str, desired_state: object | None = None) -> bool | None:
        """Is ``actor`` alive?  ``None`` means "not this machine's to judge".

        ``None`` is what keeps remote peers on raw zenoh liveliness, which is
        the right answer for them: a peer's own daemon owns that judgement.
        ``desired_state`` is the snapshot caller's already-loaded copy of the
        desired state (card 259): when present it rides along to the worker
        probe so one snapshot parses the document once; without it the probe
        loads its own, exactly as before.
        """

        if desired_state is None:
            running = self._worker_running(actor)
        else:
            running = self._worker_running(actor, desired_state)
        if running is not None:
            return running
        binding = self._bindings.get(actor)
        if actor in self._interactive_actors() or (
            binding is not None and binding.runtime == RUNTIME_INTERACTIVE
        ):
            return self.heartbeat_fresh(actor)
        return None

    def status(
        self, actor: str, desired_state: object | None = None
    ) -> str | None:
        """``"online"`` / ``"offline"`` / ``None`` when this daemon cannot tell."""

        if actor == self.node_id:
            return "online"
        verdict = self.verdict(actor, desired_state)
        if verdict is None:
            return None
        return "online" if verdict else "offline"

    def registered_status(
        self, actor: str, desired_state: object | None = None
    ) -> str:
        """Status for an agent this machine has a registry record for.

        A registered agent with nothing running is offline — the case the old
        hard-coded ``"online"`` literal could never express.
        """

        return self.status(actor, desired_state) or "offline"

    def snapshot(
        self, actor: str, desired_state: object | None = None
    ) -> dict[str, Any]:
        binding = self._bindings.get(actor)
        last_seen = self._heartbeats.get(actor)
        # Deliberately no "actor" key: callers merge this into an Agent record
        # that already carries its own identity, and a second spelling of the
        # same thing is exactly how identities drift apart.
        return {
            "status": self.registered_status(actor, desired_state),
            "harness": binding.harness if binding is not None else None,
            "runtime": binding.runtime if binding is not None else None,
            "sessionId": binding.session_id if binding is not None else None,
            "heartbeatAgeSeconds": (
                None if last_seen is None else round(self._clock() - last_seen, 3)
            ),
        }
