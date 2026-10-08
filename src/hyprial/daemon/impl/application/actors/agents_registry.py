"""Agent registry projections: seeding, binding, canonical URIs, status/restore projections and worker verdicts."""

from __future__ import annotations

from __future__ import annotations
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, TYPE_CHECKING
from hyprial.identity import (
    RUNTIME_HEADLESS,
    RUNTIME_INTERACTIVE,
    Agent,
    AgentAlreadyRunning,
    AgentError,
    HandoverNotice,
)
from hyprial.identity import (
    desired_generation,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.status import build_actor_status_snapshot
from hyprial.daemon.impl.desired_state  import (
    DesiredState,
    InteractiveSession,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
from hyprial.kernel import (
    canonical_agent_uri,
    parse_agent_uri,
)
from hyprial.daemon.impl.alias_resolver import (
    AliasStatus,
    AliasSurface,
    AliasValidationError,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.actors.sessions.registry import (
    _session_harness,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
)


class _AgentsRegistryMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _canonical_harness_uri(
        self,
        name: str,
        spec: HarnessLaunchSpec | None = None,
        desired: DesiredState | None = None,
    ) -> str:
        """The network identity this daemon mints for a managed harness.

        A transferred or host-invited spec carries ``pinned_owner``: the
        URI keeps the admitted owner instead of this daemon's ambient one,
        so hosting never silently renames the worker. The machine
        segment stays this node's id -- the worker physically lives here.

        The spec=None fallback is a defensive single-row projection read for
        single-call paths (e.g. identity.whoami).  Batch paths still pass the
        spec or desired snapshot they already hold.  The URI is identical in
        every form; only the amount of projection data copied differs.
        """

        owner = self.owner
        if spec is None and desired is None:
            # A batch path that installed the worker-status snapshot already
            # holds one loaded copy; reuse it instead of another projection
            # lookup (the runtime timer resolves every streaming actor here).
            snapshot = self._current_worker_snapshot()
            if snapshot is not None and snapshot.desired is not None:
                desired = snapshot.desired
        if spec is None:
            try:
                if desired is None:
                    spec = self.desired_state.harness_spec(name)
                else:
                    spec = next(
                        (item for item in desired.harnesses if item.name == name),
                        None,
                    )
            except Exception:  # noqa: BLE001 - identity must never crash
                spec = None
        if spec is not None and spec.pinned_owner:
            owner = spec.pinned_owner
        return canonical_agent_uri(owner, self.node_id, name)

    def _normalize_persisted_interactive_sessions(self) -> None:
        """Upgrade legacy bare desired-state sessions before mesh restore.

        A daemon restart must not briefly re-publish the old bare route while
        its surviving Channel child reconnects.  If legacy state somehow
        contains both spellings, the already-canonical record wins: it is the
        only record whose identity satisfied the post-fix registration
        contract, and restoring both would recreate the defect.
        """

        state = self.desired_state.load()
        normalized: dict[str, InteractiveSession] = {}
        rewrites: list[tuple[str, str | None, str]] = []
        changed = False
        for session in state.interactive_sessions:
            actor = self._canonical_interactive_actor(session.actor)
            if actor != session.actor:
                rewrites.append((session.actor, session.session_ref, actor))
            candidate = (
                session
                if actor == session.actor
                else replace(session, actor=actor)
            )
            changed = changed or candidate is not session
            existing = normalized.get(actor)
            if existing is None or session.actor == actor:
                normalized[actor] = candidate
            else:
                changed = True
        sessions = tuple(normalized[actor] for actor in sorted(normalized))
        if changed or sessions != state.interactive_sessions:
            self.desired_state.normalize_interactive_sessions(
                rewrites=tuple(rewrites)
            )

    def _local_agent_candidates(self, target: str) -> set[str]:
        """Return the exact local transport identities claimed by an alias.

        Both message short-name resolution and P2 admission consume this one
        projection, so a desired-state-only streaming harness cannot resolve
        successfully and then be rejected by a narrower registry-only view.
        """

        candidates: set[str] = set()
        from hyprial.daemon.impl.harnesses import is_streaming_spec

        state = self.desired_state.load()
        for spec in state.harnesses:
            if is_streaming_spec(spec) and target in (spec.name, spec.nickname):
                candidates.add(self._canonical_harness_uri(spec.name, spec))
        agent = self.agents.get(target)
        if agent is not None:
            candidates.add(agent.uri)
        return candidates

    def _known_local_agent_alias(self, alias: str) -> bool:
        """Whether one local source currently claims ``alias``.

        Bare-name resolution deliberately remains limited to desired state and
        the registry, but canonical input admission must also honor the send
        path's existing definition of "claimed here": presence, interactive
        sessions, managed workers, and streaming desired state.  Reuse that
        predicate so PAC, routines, and message sends cannot drift apart.
        """

        try:
            canonical = canonical_agent_uri(self.owner, self.node_id, alias)
        except ValueError:
            return False
        return bool(self._local_agent_candidates(alias)) or not (
            self._is_unresolved_local_identity(canonical)
        )

    def _resolve_agent_alias(self, target: str) -> str:
        """Resolve a bare short name to one canonical agent URI **by lookup**.

        Two local tables answer, each by exact match, and nothing else is
        consulted:

        - the machine's agent registry (``actor`` is one row per machine), and
        - this daemon's managed streaming harnesses, matched on either the
          managed ``name`` or the operator-facing ``nickname``.  The minted
          candidate is always derived from ``spec.name`` regardless of which
          alias was typed — a connector registers its liveliness token and
          inbox endpoint under the name-minted URI (see
          ``_reconcile_harness_actors``), so minting from the typed nickname
          would recreate the queue-forever trap under a key nobody drains.

        ⛔ Presence and interactive sessions are deliberately NOT scanned.
        Scanning them meant matching on ``owner + actor`` while ignoring
        ``machine``, which was harmless only because the owner segment used to
        differ per host: it came from the local login.  Now that owner is a
        *user* identity — one value across all of a user's machines — a peer
        running the same actor name would have become a second candidate for
        every bare send, including ``hq-adjutant``.  Presence was already
        known to be unsafe in one direction (a sender resolved through it
        could mint a message impersonating a remote actor); a table lookup
        removes that hazard in both directions instead of guarding one.

        ⚠️ More than one candidate stays a loud error, and it is **not** dead
        code even with only two exact lookups: ``_canonical_harness_uri``
        honours ``spec.pinned_owner`` (decision D-D), so a transferred spec
        mints the SOURCE owner while a registry row for the same short name
        mints this daemon's.  That disagreement is real and must never be
        resolved by guessing.

        An unknown bare name is left untouched, so node actors and durable
        queueing keep their existing semantics — ``_destroy_agent_messages``
        drains both spellings precisely because of it.  Targets that already
        carry a scheme pass through untouched.
        """

        if ":" in target:
            return target
        candidates = self._local_agent_candidates(target)
        if len(candidates) > 1:
            raise DaemonRequestError(
                ipc_errors.AMBIGUOUS_TARGET,
                f"actor name {target!r} matches more than one agent; "
                "address the canonical URI directly",
                {"target": target, "candidates": sorted(candidates)},
            )
        return next(iter(candidates), target)

    def _validate_input_alias(
        self,
        target: str,
        *,
        surface: AliasSurface,
        preserve_unknown: bool = False,
    ) -> str:
        """Apply P2 validation without changing the caller's stored form."""

        try:
            return self._alias_resolver.validate_target(
                target,
                surface=surface,
                preserve_unknown=preserve_unknown,
            )
        except (AgentError, OSError) as error:
            # One warn per (error type, surface) per daemon generation: a
            # single broken registry row must not log on every send.
            seen = self.__dict__.setdefault("_alias_source_unavailable_seen", set())
            key = (type(error).__name__, surface.value)
            if key not in seen:
                seen.add(key)
                self._log(
                    "warn",
                    "daemon",
                    "alias.source-unavailable",
                    errorType=key[0],
                    surface=key[1],
                )
            return target
        except AliasValidationError as error:
            if preserve_unknown and error.status is AliasStatus.UNKNOWN:
                return target
            raise DaemonRequestError(
                error.code,
                str(error),
                error.data,
            ) from error

    def _resolve_and_validate_input_alias(
        self, target: str, *, surface: AliasSurface
    ) -> str:
        """Preserve existing short-name expansion, then validate its aliases."""

        resolved = self._resolve_agent_alias(target)
        # Before P2, an unresolved bare target reached the established
        # target-kind gate and was refused as TARGET_IS_NODE with its
        # historical payload. Keep that public contract; only new alias
        # decisions (including ambiguity) belong to this layer.
        # message.send already has stable handling for both unknown spellings:
        # bare names reach the target-kind gate, while canonical local names
        # queue with TARGET_UNRESOLVED plus send.target_unresolved telemetry.
        # P2 adds ambiguity/source decisions but must not replace either path
        # with a request-level TARGET_UNRESOLVED refusal.
        self._validate_input_alias(resolved, surface=surface, preserve_unknown=True)
        return resolved

    def _is_unresolved_local_identity(self, target: str) -> bool:
        """True when a queued target names THIS node but nothing claims it.

        This node is the authority for ``agent:<its owner>:<its node>:…``
        URIs: if no streaming harness, interactive session (either
        spelling), managed worker, or online actor carries the URI, the
        durable queue can never drain it — the silent-loss shape of the
        bare-name/canonical-pin mismatch.  Foreign-namespace URIs and bare
        node-actor names keep their quiet durable-queue semantics.
        """

        parsed_target = parse_agent_uri(target)
        if parsed_target is None:
            return False
        actor_segment = parsed_target[2]
        if parsed_target[0] != self.owner or parsed_target[1] != self.node_id:
            return False
        if self._presence is not None and self._presence.actor_online(target):
            return False
        # Deferred import mirrors _resolve_agent_alias.
        from hyprial.daemon.impl.harnesses import is_streaming_spec

        state = self.desired_state.load()
        for spec in state.harnesses:
            if (
                is_streaming_spec(spec)
                and self._canonical_harness_uri(spec.name, spec) == target
            ):
                return False
        for session in self._agent_session_domains.session.read_sessions():
            if session.actor in (target, actor_segment):
                return False
        return self._worker_session_ref(target) is None

    def _managed_worker_running(
        self, actor: str, desired: DesiredState | None = None
    ) -> bool | None:
        """Supervised-subprocess state for a daemon-managed worker, else None.

        This is the signal ``connectors[].running`` already reported on its
        own; feeding it into AgentLiveness is what collapses two of the three
        old models into one instead of adding a fourth.  ``desired`` is the
        snapshot's already-loaded desired state (card 259), threaded to
        ``_canonical_harness_uri`` so each row stops re-reading the store.
        """

        snapshot = self._current_worker_snapshot()
        if snapshot is not None:
            return snapshot.running_by_actor.get(actor)
        # Single-call paths (identity.whoami, session fencing) reach here
        # without a request snapshot and keep the live per-call query.
        # Batch paths install _worker_status_snapshot to share the supervisor
        # round trip. Unsnapshotted calls use a bounded single-harness lookup
        # only for the one status row whose name can match the actor URI.
        # Unit-level application.handle seams stand in a bare object for the
        # supervisor; absent a real one there is simply no worker signal.
        report = getattr(self._harnesses, "status", None)
        if not callable(report):
            return None
        statuses = report()
        if not isinstance(statuses, tuple | list):
            return None
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name:
                continue
            if status.get("runtime") == "lark":
                continue
            if name == actor:
                return bool(status.get("running"))
            # canonical_agent_uri(owner, node, name) always ends ":<name>", so
            # a row whose name is not actor's last segment can never match;
            # skip even the bounded harness-spec lookup for impossible rows.
            if not actor.endswith(f":{name}"):
                continue
            if (
                self._canonical_harness_uri(name, desired=desired) == actor
                or name == actor
            ):
                return bool(status.get("running"))
        return None

    def _worker_session_ref(self, actor: str) -> str | None:
        """Project a managed worker session ref from the Harness authority."""

        snapshot = self._current_worker_snapshot()
        if snapshot is not None:
            return snapshot.session_ref_by_actor.get(actor)
        harnesses = self._harnesses
        if harnesses is None:
            return None
        read_refs = getattr(harnesses, "projected_worker_session_refs", None)
        if not callable(read_refs):
            return None
        refs = read_refs()
        for spec in self.desired_state.load().harnesses:
            if spec.harness == "lark":
                continue
            if self._canonical_harness_uri(spec.name, spec) == actor:
                return refs.get((spec.harness, spec.name))
        return None

    def _seed_agent_bindings(self) -> None:
        """Re-derive runtime bindings after a restart, before serving requests.

        Bindings are per daemon generation, so without this a restarted daemon
        would forget that ``foo`` is already running headless and let a second
        connector claim the same URI -- the exact collision A1 forbids.
        """

        from hyprial.identity import option_value

        state = self.desired_state.load()
        self._agent_recovery_failures.clear()
        for spec in state.harnesses:
            if spec.harness == "lark":
                continue
            self._ensure_agent(
                self._canonical_harness_uri(spec.name, spec),
                harness=spec.harness, interactive=False, cwd=spec.cwd,
                args=spec.args, provider=spec.model_provider, model=spec.model,
            )
            self._agent_liveness.bind(
                self._canonical_harness_uri(spec.name, spec),
                harness=spec.harness,
                runtime=RUNTIME_HEADLESS,
                session_id=spec.session_ref,
            )
        for session in state.interactive_sessions:
            if self.agents.is_session_ref_retired(
                session.actor, session.session_ref
            ):
                binding = self._agent_liveness.binding(session.actor)
                if (
                    binding is not None
                    and binding.runtime == RUNTIME_INTERACTIVE
                    and binding.session_id == session.session_ref
                ):
                    self._agent_liveness.release(session.actor)
                self._log(
                    "warn",
                    "agents",
                    "agent.session_binding.retired_skipped",
                    actor=session.actor,
                    sessionId=session.session_ref,
                )
                continue
            agent = self.agents.get(session.actor)
            self._ensure_agent(
                session.actor, harness=_session_harness(session), interactive=True,
                cwd=session.cwd,
                provider=option_value(session.command, "--provider") or (agent.provider if agent else None),
                model=option_value(session.command, "--model") or (agent.model if agent else None),
            )
            harness = _session_harness(session)
            try:
                self._agent_liveness.bind(
                    session.actor,
                    harness=harness,
                    runtime=RUNTIME_INTERACTIVE,
                    session_id=session.session_ref,
                )
            except DomainCommandError as error:
                # This is the per-agent recovery boundary.  The preceding
                # headless bind is deliberately left in place, so an old
                # interactive-session row cannot evict or stop the running
                # connector.  Other domain failures still abort actor-runtime:
                # a closed/overloaded actor or corrupt global state is not one
                # agent's connector conflict and must remain fail-loud.
                if error.code != AgentAlreadyRunning.code:
                    raise
                failure: JsonObject = {
                    "code": error.code,
                    "errorType": type(error).__name__,
                    "error": str(error),
                    "source": session.source,
                    "harness": harness,
                    "runtime": session.runtime,
                    "sessionId": session.session_ref,
                }
                self._agent_recovery_failures[session.actor] = failure
                self._log(
                    "warn",
                    "agents",
                    "agent.recovery.failed",
                    actor=session.actor,
                    source=session.source,
                    harness=harness,
                    runtime=session.runtime,
                    sessionId=session.session_ref,
                    errorCode=error.code,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                    detail=(
                        "interactive session recovery was isolated to this "
                        "agent; its persistent record and the live connector "
                        "were left unchanged for operator resolution"
                    ),
                )

    def _agent_name_for(self, actor: str) -> str | None:
        """The machine-local agent name behind an actor, when there is one.

        A four-segment URI minted by another owner or machine is somebody
        else's agent: this daemon holds no record for it and must not invent
        one.
        """

        if actor == self.node_id:
            return None
        return self.agents.local_actor(actor)

    def _ensure_agent(
        self,
        actor: str,
        *,
        harness: str,
        interactive: bool,
        cwd: str | None = None,
        args: tuple[str, ...] = (),
        provider: str | None = None,
        model: str | None = None,
        session_ref: str | None = None,
    ) -> Agent | None:
        """Get-or-create this machine's record for ``actor`` (A5).

        ``hyprial start`` (and PAC worker launch) reaches agent creation through
        here, so a connector can never come up without an identity behind it.
        ``hyprial agent create`` is NOT this path: its IPC handler
        (agents_admin/registry_ops.py) also gives a new agent the default
        config C when none is passed, while this path creates without one, so
        start/PAC agents run the legacy (non-P2) runtime.  Card 3b21172f: the
        two are to converge only after #1213 is installed in production.
        """

        name = self._agent_name_for(actor)
        if name is None:
            return None
        from hyprial.identity import runtime_capabilities
        from hyprial.daemon.impl.harnesses.capabilities import declare

        facts = runtime_capabilities(
            harness, interactive=interactive, provider=provider, model=model, args=args,
            declare=declare,
        )
        agent = self.agents.get(name)
        if agent is None:
            if session_ref is not None:
                # A whole destroy can complete between a registration's first
                # retired check and here. Destroy retires the refs before it
                # deletes the agent, so an absent agent with a retired ref is
                # exactly that case: refuse instead of re-creating it.
                self._reject_retired_session_ref(actor, session_ref)
            agent = self.agents.create(
                name,
                cwd=cwd,
                capabilities=facts,
                provider=facts["provider"],
                model=facts["model"],
                harness_args={harness: args} if args else None,
                preferred_harness=harness,
            )
            if not agent.actor.startswith("wf-"):
                self._finish_resident_agent_creation(agent)
            self._log(
                "info", "agents", "agent.created", actor=agent.uri, harness=harness
            )
            self._declare_persona_route(agent.uri)
            # A legacy pin naming this agent becomes migratable the moment
            # the record exists (see _migrate_legacy_channel_pins).
            self._migrate_legacy_channel_pins()
            self._publish_agent_restore_allow(agent)
            return agent
        changes: dict[str, Any] = {
            "capabilities": {**agent.capabilities, **facts},
            "provider": facts["provider"],
            "model": facts["model"],
        }
        if cwd is not None and agent.cwd != cwd:
            changes["cwd"] = cwd
        if args and agent.args_for(harness) != args:
            changes["harness_args"] = {**agent.harness_args, harness: args}
        if changes:
            agent = self.agents.save(replace(agent, **changes))
        self._publish_agent_restore_allow(agent)
        return agent

    def _publish_agent_restore_allow(self, agent: Agent) -> None:
        spec = next(
            (
                item
                for item in self.desired_state.load().harnesses
                if item.name == agent.actor and item.harness != "lark"
            ),
            None,
        )
        if spec is not None:
            self._publish_restore_eligibility(
                spec=spec,
                entity_token=agent.entity_token,
                suppressed=False,
            )

    def _bind_agent(
        self,
        actor: str,
        *,
        harness: str,
        runtime: str,
        session_id: str | None,
        cwd: str | None = None,
        args: tuple[str, ...] = (),
        refuse_runtimes: tuple[str, ...] = (RUNTIME_HEADLESS, RUNTIME_INTERACTIVE),
    ) -> HandoverNotice | None:
        """Point an agent at a harness, refusing to attach to a running one.

        Starting a harness for an existing agent is **not** a name collision:
        an agent name is unique because only one Agent record may hold it, and
        that is enforced at creation. ``hyprial start pi --name foo`` on an
        existing ``foo`` is *that same agent* moving to pi -- one agent, one
        identity, a different runtime binding.

        But it may not move while something is still speaking for it. Two
        connectors on one actor is the original defect: both mint the identical
        four-segment URI, dispatch hands every message to whichever sorts
        first, and the other reports online forever while receiving nothing.
        Rather than stopping the incumbent automatically, this refuses and
        names the remedy -- stopping a headless worker out from under its
        in-flight work, or deregistering an interactive session whose window is
        still open, are both worse than an error the user can act on. The move
        is then ``hyprial down`` followed by ``hyprial start``, and A9 reports the
        previous harness and session id on the way back up.

        ``refuse_runtimes`` narrows which incumbents object. ``lifecycle.start``
        objects to any of them. ``session.register`` objects only to a headless
        worker, because interactive-to-interactive is the existing
        supersede-by-actor handoff (last writer wins, loser fenced off with
        SESSION_SUPERSEDED) -- still one connector, and not a path a user's
        ``hyprial start`` reaches without passing the check above first.
        """

        name = self._agent_name_for(actor)
        if name is None:
            # Not a local agent identity (a foreign URI, or this node itself):
            # no record to own, nothing to bind.
            return None
        self._refuse_if_running(
            actor, harness=harness, runtime=runtime, refuse_runtimes=refuse_runtimes
        )
        self._ensure_agent(
            actor, harness=harness, interactive=runtime == RUNTIME_INTERACTIVE,
            cwd=cwd, args=args,
        )
        superseded = self._agent_liveness.displaced_by(
            actor, harness=harness, runtime=runtime
        )
        if superseded is not None:
            # The incumbent is already known dead (the check above passed), so
            # this only clears its leftovers -- a desired-state entry that
            # would otherwise be restored beside the new connector.
            stopped = self._stop_agent_runtime(actor, keep=(harness, runtime))
            self._log(
                "info",
                "agents",
                "agent.binding.superseded",
                actor=actor,
                previous=superseded.to_json(),
                stopped=stopped,
                harness=harness,
                runtime=runtime,
            )
            # The Agent actor independently rechecks live ownership at bind.
            # Retire the proven-dead incumbent after its connector resources
            # are removed so the new typed bind cannot observe an ambiguous
            # unknown process state and conservatively reject the handoff.
            self._agent_liveness.release(actor)
        prior_agent = self.agents.require(name)
        self._agent_liveness.bind(
            actor, harness=harness, runtime=runtime, session_id=session_id
        )
        notice = (
            HandoverNotice(
                actor=prior_agent.actor,
                previous_harness=prior_agent.last_harness,
                previous_session_id=prior_agent.last_session_id,
                next_harness=harness,
            )
            if prior_agent.last_harness is not None
            and prior_agent.last_harness != harness
            else None
        )
        if notice is not None:
            self._log(
                "info",
                "agents",
                "agent.harness.handover",
                actor=actor,
                previousHarness=notice.previous_harness,
                previousSessionId=notice.previous_session_id,
                nextHarness=harness,
            )
        return notice

    def _release_agent_binding(self, actor: str) -> None:
        self._agent_liveness.release(actor)

    def _actor_status_snapshot(self) -> list[JsonObject]:
        """One canonical actor table for top, targets, and future consumers."""

        assert self._harnesses is not None
        assert self._presence is not None
        # One load per request: the ps/agent handlers install a worker
        # snapshot that already loaded the state (intg U2); reuse it, and load
        # only when this is called bare (card 259 P1 on dev did the load here).
        snapshot = self._current_worker_snapshot()
        desired = (
            snapshot.desired
            if snapshot is not None and snapshot.desired is not None
            else self.desired_state.load()
        )
        sessions = {
            session.actor: session for session in desired.interactive_sessions
        }
        if snapshot is not None:
            # The request snapshot already holds this supervisor read; a
            # second live status() per request is what made targets N x.
            connectors = snapshot.statuses
        else:
            report_connectors = getattr(self._harnesses, "status", None)
            connectors = report_connectors() if callable(report_connectors) else ()

        def status_for_actor(actor: str, online: frozenset[str]) -> str:
            session = sessions.get(actor)
            if (
                session is not None
                and session.source == "claude-channel"
                and session.channel_lease_digest is not None
                and not self._channel_alive(session, self._clock())
            ):
                return "offline"
            agent = self.agents.get(actor)
            if agent is not None:
                return self._registered_agent_status(agent, desired)
            return self._agent_target_status(actor, online, desired)

        return build_actor_status_snapshot(
            owner=self.owner,
            node_id=self.node_id,
            connectors=connectors,
            interactive_sessions=(
                {
                    "actor": session.actor,
                    "runtime": session.runtime,
                    "source": session.source,
                }
                for session in desired.interactive_sessions
            ),
            registered_actors=(agent.uri for agent in self.agents.list()),
            presence_actors=self._presence.online_actors(),
            status_for_actor=status_for_actor,
        )

    @staticmethod
    def _agent_is_inactive(
        agent: Agent,
        hints: Mapping[str, int | bool | None],
        *,
        cutoff_ms: int,
        kept: frozenset[str],
    ) -> bool:
        if agent.actor in kept or hints.get("hasLastSessionId") is True:
            return False
        timestamps = (
            agent.last_active_at_ms,
            hints.get("createdAtMs"),
            hints.get("agentHomeMtimeMs"),
        )
        return not any(
            isinstance(value, int) and not isinstance(value, bool) and value >= cutoff_ms
            for value in timestamps
        )

    def _agent_status_json(
        self,
        agent: Agent,
        *,
        activity_hints: Mapping[str, int | bool | None] | None = None,
    ) -> JsonObject:
        spelling = (
            agent.actor
            if self._agent_liveness.binding(agent.uri) is None
            and self._agent_liveness.binding(agent.actor) is not None
            else agent.uri
        )
        recovery_failure = self._agent_recovery_failures.get(
            agent.uri
        ) or self._agent_recovery_failures.get(agent.actor)
        recovery_cleanup = self._agent_recovery_cleanups.get(
            agent.uri
        ) or self._agent_recovery_cleanups.get(agent.actor)
        from hyprial.identity import shared_credential_status

        credential_status = shared_credential_status(
            registry=self._agent_registry, agent=agent
        )
        return {
            # ``Agent.to_json`` is also the durable/lifecycle round-trip form,
            # so it carries the internal ``entityToken`` incarnation fence;
            # that is authority state, not a public ``ps`` field, and the frozen
            # snapshot contract omits it.
            **{
                key: value
                for key, value in agent.to_json().items()
                if key != "entityToken"
                and (activity_hints is not None or key != "lastActiveAtMs")
            },
            **(
                {"activityHints": dict(activity_hints)}
                if activity_hints is not None
                else {}
            ),
            **self._agent_liveness.snapshot(spelling),
            "status": self._registered_agent_status(agent),
            **self._restore_agent_projection(
                agent, now_ms=self._restore_now_ms()
            ),
            **(
                {"sharedCredentials": credential_status}
                if credential_status
                else {}
            ),
            **(
                {
                    "recoveryStatus": "failed",
                    "recoveryError": dict(recovery_failure),
                }
                if recovery_failure is not None
                else {}
            ),
            **(
                {
                    "recoveryStatus": "cleaned",
                    "recoveryCleanup": dict(recovery_cleanup),
                }
                if recovery_failure is None and recovery_cleanup is not None
                else {}
            ),
        }

    def _restore_agent_projection(
        self, agent: Agent, *, now_ms: int | None = None,
        spec: HarnessLaunchSpec | None = None,
    ) -> JsonObject:
        now_ms = self._restore_now_ms() if now_ms is None else now_ms
        projection = self.agents.projection(agent.actor)
        if projection is None:
            return {}
        if projection.block is not None:
            block = projection.block
            age_ms = max(0, now_ms - block.blocked_at_ms)
            return {
                "workerState": "blocked",
                "restoreStatus": "blocked",
                "blockReason": block.reason,
                "blockedAtMs": block.blocked_at_ms,
                "blockedAgeMs": age_ms,
                "lastActiveAtMs": agent.last_active_at_ms,
                "idleAgeMs": None,
                "restoreThresholdMs": (
                    self._restore_policy.projection().policy.threshold_ms
                ),
                "restoreOverride": "none",
                "activityUnknown": agent.actor in self._restore_activity_unknown,
                "status": (
                    f"blocked ({block.reason}; {self._restore_age_text(age_ms)})"
                ),
            }
        disposition = projection.restore_disposition
        if disposition is not None and spec is not None and disposition.desired_generation != desired_generation(spec):
            disposition = None
        if disposition is None:
            if agent.actor in self._restore_activity_unknown:
                return {
                    "lastActiveAtMs": None, "idleAgeMs": None,
                    "restoreThresholdMs": self._restore_threshold_for_status(),
                    "restoreOverride": "none", "activityUnknown": True,
                }
            return {}
        idle_age_ms = (
            max(0, now_ms - disposition.last_active_at_ms)
            if disposition.last_active_at_ms is not None
            else disposition.idle_age_ms
        )
        return {
            "workerState": "dormant",
            "restoreStatus": disposition.status,
            "lastActiveAtMs": disposition.last_active_at_ms,
            "idleAgeMs": idle_age_ms,
            "restoreThresholdMs": disposition.restore_threshold_ms,
            "restoreOverride": disposition.restore_override,
            "activityUnknown": disposition.activity_unknown,
            "status": (
                "dormant (idle "
                f"{self._restore_age_text(idle_age_ms)}; auto-restore threshold "
                f"{self._restore_age_text(disposition.restore_threshold_ms)})"
            ),
        }

    def _restore_status_summary(self) -> JsonObject:
        projections = self.agents.projections()
        dispositions = tuple(
            item.restore_disposition
            for item in projections
            if item.restore_disposition is not None
        )
        blocks = tuple(
            item.block for item in projections if item.block is not None
        )
        policy = self._restore_policy.projection()
        now_ms = self._restore_now_ms()
        idle_ages = tuple(
            (
                max(0, now_ms - item.last_active_at_ms)
                if item.last_active_at_ms is not None
                else item.idle_age_ms
            )
            for item in dispositions
        )
        return {
            "restoreThresholdMs": policy.policy.threshold_ms,
            "suppressedCount": len(dispositions),
            "oldestIdleAgeMs": max(
                (value for value in idle_ages if value is not None),
                default=None,
            ),
            "unknownActivityCount": len(self._restore_activity_unknown),
            "blockedCount": len(blocks),
            "oldestBlockedAgeMs": max(
                (
                    max(0, now_ms - item.blocked_at_ms)
                    for item in blocks
                ),
                default=None,
            ),
            "degraded": policy.degraded is not None,
            **(
                {"degradedReason": policy.degraded}
                if policy.degraded is not None
                else {}
            ),
        }

    @staticmethod
    def _restore_now_ms() -> int:
        return time.time_ns() // 1_000_000

    @staticmethod
    def _restore_age_text(value_ms: int | None) -> str:
        if value_ms is None:
            return "unknown"
        hours = max(0, value_ms) // (60 * 60 * 1_000)
        if hours:
            return f"{hours}h"
        return f"{max(0, value_ms) // (60 * 1_000)}m"

    def _agent_target_status(
        self, actor: str, online: frozenset[str], desired: DesiredState | None = None
    ) -> str:
        """The real online/offline verdict behind a targets entry.

        Replaces a hard-coded ``"online"`` literal that was built from a list
        of online actors and therefore could not report offline even in
        principle. Presence membership is still what answers for a remote peer
        -- its own daemon owns that judgement -- but a locally registered agent
        now gets this machine's real verdict, which can and does say offline.
        """

        status = self._agent_liveness.status(actor, desired_state=desired)
        if status is not None:
            return status
        return "online" if actor in online else "offline"

    def _registered_agent_status(
        self, agent: Agent, desired: DesiredState | None = None
    ) -> str:
        """Verdict for a registry agent, tolerating either identity spelling.

        A connector normally registers under the canonical four-segment URI,
        but the daemon has always accepted a bare actor name too, and the
        binding is recorded under whichever spelling arrived. Checking both
        keeps a bare-name session from being reported offline while it is
        demonstrably alive.
        """

        for spelling in (agent.uri, agent.actor):
            status = self._agent_liveness.status(spelling, desired_state=desired)
            if status is not None:
                return status
        return "offline"
