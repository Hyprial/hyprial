"""Interactive session registry: registration fences, turn stats, channel leases and the session IPC families."""

from __future__ import annotations

from __future__ import annotations
import hashlib
import hmac
import time
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.daemon.impl.network.routing.session_route_coordinator  import (
    SessionRouteKind,
)
from hyprial.identity import (
    RUNTIME_HEADLESS,
    RUNTIME_INTERACTIVE,
    HandoverNotice,
    PinConflictError,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import (
    CHANNEL_LIVENESS_TTL_SECONDS,
    CHANNEL_PROTOCOL_VERSION,
    SESSION_CARRIER_SOURCES,
    channel_generation,
    safe_channel_build_version,
)
from hyprial.daemon.impl.desired_state  import (
    InteractiveSession,
)
from hyprial.daemon.impl.session_actor  import owner_only_relocation
from hyprial.daemon.impl.mcp.channel.ownership import (
    _process_identities_match,
    _read_process_identity as process_birth_identity,
    _read_process_parent,
)
from hyprial.daemon.impl.operations.session_ports  import (
    HeartbeatSessionCommand,
    RefreshSessionCommand,
    RegisterSessionCommand,
    UnregisterSessionCommand,
)
from hyprial.daemon.impl.operations.top  import InteractiveTurnStats
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_boolean,
    _optional_positive_integer,
    _optional_string_param,
    _required_string,
)


def _channel_lease_token(value: object) -> str:
    if not isinstance(value, str) or not (32 <= len(value) <= 256):
        raise DaemonRequestError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            "channelLeaseToken must be an opaque 32-256 character token",
        )
    return value

def _channel_lease_digest_for_registration(
    *, source: str, protocol_version: int | None, token: object
) -> str | None:
    if source != "claude-channel":
        if token is not None:
            raise DaemonRequestError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "channelLeaseToken is only valid for claude-channel sessions",
            )
        return None
    if protocol_version != CHANNEL_PROTOCOL_VERSION:
        # Protocol-v1 and older registrations remain readable and routable, but
        # cannot claim lease-backed operational liveness.
        return None
    lease = _channel_lease_token(token)
    return hashlib.sha256(lease.encode()).hexdigest()

def _verify_channel_lease(session: InteractiveSession, token: object) -> None:
    expected = session.channel_lease_digest
    if expected is None:
        raise DaemonRequestError(
            ipc_errors.CHANNEL_LIVENESS_UNAVAILABLE,
            "channel registration has no operational liveness lease",
        )
    supplied = hashlib.sha256(_channel_lease_token(token).encode()).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise DaemonRequestError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            "channel liveness lease did not match the registered session",
        )

def _interactive_session_status(
    session: Any,
    *,
    current_epoch: str | None,
    online: bool,
    last_observed: tuple[str, str, float] | None = None,
    now: float | None = None,
) -> JsonObject:
    """Separate durable registration, generation, observation, and liveness."""

    serialize = getattr(session, "to_json", None)
    status = serialize() if callable(serialize) else session.to_payload()
    status["online"] = online
    # The one-way lease verifier is daemon recovery state, not operator
    # telemetry. The digest is a same-OS-user misuse check, not a cryptographic
    # authentication boundary or an anti-replay promise.
    status.pop("channelLeaseDigest", None)
    if session.source == "claude-channel":
        observed_age: float | None = None
        if (
            last_observed is not None
            and current_epoch is not None
            and last_observed[:2] == (session.session_ref, current_epoch)
        ):
            observed_age = max(
                0.0,
                (time.monotonic() if now is None else now) - last_observed[2],
            )
        recently_observed = (
            observed_age is not None
            and observed_age <= CHANNEL_LIVENESS_TTL_SECONDS
        )
        lease_capable = bool(
            getattr(session, "channel_lease_backed", False)
            or getattr(session, "channel_lease_digest", None) is not None
        )
        status["channelRegistered"] = True
        status["channelCurrentThisGeneration"] = current_epoch is not None
        status["channelCurrentEpoch"] = current_epoch
        status["channelRecentlyObserved"] = recently_observed
        status["channelAlive"] = bool(
            lease_capable and current_epoch is not None and recently_observed
        )
        if status["channelAlive"]:
            status["channelLiveness"] = "alive"
        elif lease_capable:
            status["channelLiveness"] = "stale"
        else:
            status["channelLiveness"] = "unverified"
        if observed_age is not None:
            status["channelLastObservedAgeMs"] = int(observed_age * 1000)
    if session.source == "claude-channel" or session.runtime == "claude_interactive":
        status["channelGeneration"] = channel_generation(
            build_version=session.channel_build_version,
            protocol_version=session.channel_protocol_version,
        )
    return status

def _session_harness(session: InteractiveSession) -> str:
    """Which harness an interactive session is running on.

    An InteractiveSession never stored a harness (it did not need to when
    nothing compared connectors across harnesses); its ``runtime`` is spelled
    ``<harness>_interactive`` and its ``source`` ``<harness>-channel``, so the
    harness is recoverable without changing that record's schema.
    """

    runtime = (session.runtime or "").strip()
    if runtime:
        candidate = runtime.split("_", 1)[0]
        if candidate:
            return candidate
    source = (session.source or "").strip()
    if source:
        candidate = source.split("-", 1)[0]
        if candidate:
            return candidate
    return "claude"

def _optional_channel_build_version(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not safe_channel_build_version(value):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a safe package-version token"
        )
    return value


class _SessionRegistryMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _reject_retired_session_ref(self, actor: str, session_ref: str) -> None:
        if self.agents.is_session_ref_retired(actor, session_ref):
            raise DaemonRequestError(
                ipc_errors.SESSION_SUPERSEDED,
                f"agent {actor} was destroyed; this session is permanently retired",
            )

    @staticmethod
    def _verified_ipc_process_fence(
        process_pid: int | None,
        process_identity: str | None,
        peer_pid: object,
    ) -> tuple[int | None, str | None]:
        if process_pid is None or process_identity is None:
            return process_pid, process_identity
        if not isinstance(peer_pid, int) or peer_pid <= 0:
            return None, None
        observed = process_birth_identity(process_pid)
        if observed is None or not _process_identities_match(
            process_identity, observed
        ):
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "session process identity does not match the claimed process",
            )

        def ancestry_reaches(start: int, target: int) -> bool:
            """True only when a bounded, fully readable walk meets ``target``.

            An unreadable link (root-owned processes such as /usr/bin/login
            are unreadable to a non-root daemon), a cycle, or an over-long
            chain is "not proven", never an error: the other check may still
            prove the relation.
            """
            current = start
            seen: set[int] = set()
            while current > 1 and current not in seen and len(seen) < 128:
                if current == target:
                    return True
                seen.add(current)
                parent = _read_process_parent(current)
                if parent is None or parent < 0:
                    return False
                current = parent
            return False

        # Interactive carriers claim an ancestor harness process (claude, pi):
        # walk from the peer up to the claim. Codex is the inverse: the IPC CLI
        # Popen()s the claimed app-server, so a DIRECT child of the peer is
        # also accepted -- depth one only, not any descendant.
        if _read_process_parent(process_pid) == peer_pid or ancestry_reaches(
            peer_pid, process_pid
        ):
            return process_pid, process_identity
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "session process claim is not proven to be the IPC peer's "
            "ancestor or direct child",
        )

    def _ipc_session_register(
        self, params, *, verify_process_claim: bool = False
    ) -> Any:
        actor = self._mcp_actor(params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        self._reject_retired_session_ref(actor, session_ref)
        source = _required_string(params.get("source"), "source")
        process_pid = _optional_positive_integer(
            params.get("processPid"), "processPid"
        )
        process_identity = _optional_string_param(
            params.get("processIdentity"), "processIdentity"
        )
        if (process_pid is None) != (process_identity is None):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "processPid and processIdentity must be provided together",
            )
        if process_pid is not None and source not in SESSION_CARRIER_SOURCES:
            raise DaemonRequestError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "process identity is valid only for interactive carrier sessions",
            )
        if verify_process_claim:
            process_pid, process_identity = self._verified_ipc_process_fence(
                process_pid,
                process_identity,
                getattr(self._ipc_peer, "pid", None),
            )
        command = params.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "command must be a non-empty array of strings"
            )
        session = InteractiveSession(
            actor=actor,
            cwd=_required_string(params.get("cwd"), "cwd"),
            command=tuple(command),
            source=source,
            session_ref=session_ref,
            runtime=str(params.get("runtime") or "claude_interactive"),
            channel_confirmed=params.get("channelConfirmed") is True,
            channel_build_version=_optional_channel_build_version(
                params.get("channelBuildVersion"), "channelBuildVersion"
            ),
            channel_protocol_version=_optional_positive_integer(
                params.get("channelProtocolVersion"), "channelProtocolVersion"
            ),
            owner_fence=_optional_boolean(params.get("ownerFence"), "ownerFence"),
            channel_lease_digest=_channel_lease_digest_for_registration(
                source=_required_string(params.get("source"), "source"),
                protocol_version=_optional_positive_integer(
                    params.get("channelProtocolVersion"),
                    "channelProtocolVersion",
                ),
                token=params.get("channelLeaseToken"),
            ),
            tmux_session=_optional_string_param(
                params.get("tmuxSession"), "tmuxSession"
            ),
            process_pid=process_pid,
            process_identity=process_identity,
        )
        harness = _session_harness(session)
        self._refuse_if_running(
            actor,
            harness=harness,
            runtime=RUNTIME_INTERACTIVE,
            refuse_runtimes=(RUNTIME_HEADLESS,),
        )
        from hyprial.identity import option_value

        existing_agent = self.agents.get(actor)
        same_harness = existing_agent is not None and (
            existing_agent.capabilities.get("harness", existing_agent.preferred_harness) == harness
        )
        agent = self._ensure_agent(
            actor,
            harness=harness,
            interactive=True,
            cwd=session.cwd,
            session_ref=session_ref,
            provider=option_value(session.command, "--provider") or (
                existing_agent.provider if same_harness else None
            ),
            model=option_value(session.command, "--model") or (
                existing_agent.model if same_harness else None
            ),
        )
        handover = (
            HandoverNotice(
                actor=agent.actor,
                previous_harness=agent.last_harness,
                previous_session_id=agent.last_session_id,
                next_harness=harness,
            )
            if agent is not None
            and agent.last_harness is not None
            and agent.last_harness != harness
            else None
        )
        completed = self._register_interactive_session_with_turn_stats(
            actor=actor,
            session_ref=session_ref,
            command=RegisterSessionCommand(
                        correlation_id=f"session:register:{uuid4().hex}",
                        actor=actor,
                        cwd=session.cwd,
                        command=session.command,
                        source=session.source,
                        session_ref=session_ref,
                        runtime=session.runtime or "claude_interactive",
                        channel_confirmed=session.channel_confirmed,
                        channel_build_version=session.channel_build_version,
                        channel_protocol_version=session.channel_protocol_version,
                        owner_fence=session.owner_fence,
                        channel_lease_token=params.get("channelLeaseToken")
                        if isinstance(params.get("channelLeaseToken"), str)
                        else None,
                        tmux_session=session.tmux_session,
                        process_pid=session.process_pid,
                        process_identity=session.process_identity,
                        manage_agent=agent is not None,
            ),
            reporting=(
                params.get("turnReporting") is True
                and session.runtime == "claude_interactive"
            ),
        )
        result = completed.result.to_payload()
        return {
            **result,
            **(
                {"harnessHandover": handover.to_json()}
                if handover is not None
                else {}
            ),
        }

    def _ipc_session_turn_ended(self, params) -> Any:
        actor = self._mcp_actor(params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        report_ids = params.get("reportIds")
        if (
            not isinstance(report_ids, list)
            or not report_ids
            or len(report_ids) > 1000
            or any(
                not isinstance(report_id, str)
                or not report_id
                or len(report_id) > 128
                for report_id in report_ids
            )
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "reportIds must be an array of 1-1000 non-empty strings up to 128 characters",
            )
        stats = self._accept_interactive_turn_report(
            actor, session_ref, params, report_ids
        )
        return {
            "ok": True,
            "turnCount": stats.turn_count,
            "turnCountSinceMs": stats.counting_since_ms,
            "lastTurnEndedAtMs": stats.last_turn_ended_at_ms,
        }

    def _ipc_session_refresh(self, params) -> Any:
        actor = self._mcp_actor(params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        completed = self._call_session_route(
            SessionRouteKind.REFRESH,
            actor=actor,
            session_ref=session_ref,
            command=RefreshSessionCommand(
                    correlation_id=f"session:refresh:{uuid4().hex}",
                    actor=actor,
                    session_ref=session_ref,
                    channel_lease_token=(
                        params.get("channelLeaseToken")
                        if isinstance(params.get("channelLeaseToken"), str)
                        else None
                    ),
                    manage_agent=self.agents.get(actor) is not None,
            ),
        )
        return completed.result.to_payload()

    def _ipc_session_heartbeat(self, params) -> Any:
        actor = self._mcp_actor(params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        completed = self._call_session_route(
            SessionRouteKind.HEARTBEAT,
            actor=actor,
            session_ref=session_ref,
            command=HeartbeatSessionCommand(
                    correlation_id=f"session:heartbeat:{uuid4().hex}",
                    actor=actor,
                    session_ref=session_ref,
                    channel_lease_token=(
                        params.get("channelLeaseToken")
                        if isinstance(params.get("channelLeaseToken"), str)
                        else None
                    ),
                    manage_agent=self.agents.get(actor) is not None,
            ),
        )
        return completed.result.to_payload()

    def _ipc_session_unregister(self, params) -> Any:
        actor = self._mcp_actor(params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        completed = self._unregister_interactive_session_with_turn_stats(
            actor=actor,
            session_ref=session_ref,
            command=UnregisterSessionCommand(
                    correlation_id=f"session:unregister:{uuid4().hex}",
                    actor=actor,
                    session_ref=session_ref,
                    manage_agent=self.agents.get(actor) is not None,
            ),
        )
        return completed.result.to_payload()

    def _migrate_legacy_channel_pins(self) -> None:
        """Move desired-state ``channelPins`` into the agents database, loudly.

        Runs at daemon startup and again whenever an agent is created, and is
        idempotent: an entry either migrates (value normalized to the agent's
        canonical URI, binding written to the ``pins`` table) or stays in
        ``channelPins`` with an error-level log naming the exact remedy.  A
        pin is never silently dropped -- with one decided exception: IRC
        ``#channel`` pins, whose feature is removed outright (nothing ever
        read them for delivery; production carried zero).
        """

        state = self.desired_state.load()
        if not state.channel_pins:
            return
        gateways = set(self._lark_gateway_names())
        remaining: dict[str, str] = {}
        for adapter, value in state.channel_pins:
            if adapter.startswith("#"):
                self._log(
                    "warn",
                    "daemon",
                    "pin.migration.irc-dropped",
                    channel=adapter,
                    actor=value,
                    detail="IRC channel pins are removed; nothing read them",
                )
                continue
            if gateways is not None and adapter not in gateways:
                remaining[adapter] = value
                self._log(
                    "error",
                    "daemon",
                    "pin.migration.unknown-adapter",
                    adapter=adapter,
                    actor=value,
                    detail=(
                        f"{adapter!r} is not a configured adapter; the entry "
                        f"is kept unmigrated. Configure the adapter and "
                        f"restart, or drop the entry from desired-state"
                    ),
                )
                continue
            agent = self.agents.get(value)
            if agent is None:
                remaining[adapter] = value
                self._log(
                    "error",
                    "daemon",
                    "pin.migration.unresolved",
                    adapter=adapter,
                    actor=value,
                    detail=(
                        f"pin target {value!r} does not resolve to an agent "
                        f"on this machine; the pin is kept unmigrated and is "
                        f"NOT routing. Create the agent ('hyprial agent create "
                        f"--name {value}' or 'hyprial start') and it migrates "
                        f"immediately, or drop it with 'hyprial adapter unpin "
                        f"{adapter}'"
                    ),
                )
                continue
            try:
                self.agents.pin(adapter, agent.actor)
            except PinConflictError as error:
                # The pins table's UNIQUE(agent) spoke; keep the entry
                # unmigrated and say exactly how to resolve it.
                remaining[adapter] = value
                self._log(
                    "error",
                    "daemon",
                    "pin.migration.conflict",
                    adapter=adapter,
                    actor=agent.uri,
                    pinnedBy=error.holder,
                    detail=(
                        f"agent {agent.uri} is already pinned by "
                        f"{error.holder!r} and adapter/agent bind one-to-one; "
                        f"the entry is kept unmigrated. Keep one: 'hyprial "
                        f"adapter unpin {error.holder}' then 'hyprial adapter "
                        f"pin {adapter} {agent.actor}', or 'hyprial adapter "
                        f"unpin {adapter}'"
                    ),
                )
                continue
            self._log(
                "info",
                "daemon",
                "pin.migrated",
                adapter=adapter,
                actor=agent.uri,
                legacyValue=value,
            )
        if remaining != dict(state.channel_pins):
            self.desired_state.remove_matching_channel_pins(
                tuple(
                    (adapter, value)
                    for adapter, value in state.channel_pins
                    if adapter not in remaining
                )
            )

    def _register_interactive_session_with_turn_stats(
        self,
        *,
        actor: str,
        session_ref: str,
        command: RegisterSessionCommand,
        reporting: bool,
    ) -> Any:
        """Serialize ownership replacement with its turn-counter reset."""

        completed = self._call_session_route(
            SessionRouteKind.REGISTER,
            actor=actor,
            session_ref=session_ref,
            command=command,
        )
        self._reset_interactive_turn_stats(
            actor, session_ref, reporting=reporting
        )
        return completed

    def _unregister_interactive_session_with_turn_stats(
        self,
        *,
        actor: str,
        session_ref: str,
        command: UnregisterSessionCommand,
    ) -> Any:
        """Serialize removal with any final turn report from that session."""

        completed = self._call_session_route(
            SessionRouteKind.UNREGISTER,
            actor=actor,
            session_ref=session_ref,
            command=command,
        )
        with self._interactive_turn_stats_lock:
            stats = self._interactive_turn_stats.get(actor)
            if stats is not None and stats.session_ref == session_ref:
                self._interactive_turn_stats.pop(actor, None)
        return completed

    def _accept_interactive_turn_report(
        self,
        actor: str,
        session_ref: str,
        params: JsonObject,
        report_ids: list[str],
    ) -> InteractiveTurnStats:
        """Fence and count one batch atomically with session replacement."""

        actor = self._fence_interactive_session(actor, params)
        session = next(
            (
                item
                for item in self._agent_session_domains.session.read_sessions()
                if item.actor == actor and item.session_ref == session_ref
            ),
            None,
        )
        if session is None or session.runtime != "claude_interactive":
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "turn reports require the current claude_interactive session",
            )
        stats = self._record_interactive_turn_reports(
            actor, session_ref, report_ids
        )
        try:
            self._fence_interactive_session(actor, params)
        except DaemonRequestError:
            with self._interactive_turn_stats_lock:
                current = self._interactive_turn_stats.get(actor)
                if current is not None and current.session_ref == session_ref:
                    self._interactive_turn_stats.pop(actor, None)
            raise
        return stats

    def _reset_interactive_turn_stats(
        self, actor: str, session_ref: str, *, reporting: bool
    ) -> None:
        """Start a fresh daemon-local counter for a newly registered session."""

        current = self._agent_session_domains.session.read_session(actor)
        if current is None or current.session_ref != session_ref:
            return
        with self._interactive_turn_stats_lock:
            current = self._agent_session_domains.session.read_session(actor)
            if current is None or current.session_ref != session_ref:
                return
            if not reporting:
                self._interactive_turn_stats.pop(actor, None)
            else:
                self._interactive_turn_stats[actor] = InteractiveTurnStats(
                    session_ref=session_ref,
                    counting_since_ms=int(time.time() * 1000),
                    turn_count=0,
                    last_turn_ended_at_ms=None,
                )
        current = self._agent_session_domains.session.read_session(actor)
        if current is None or current.session_ref != session_ref:
            with self._interactive_turn_stats_lock:
                stats = self._interactive_turn_stats.get(actor)
                if stats is not None and stats.session_ref == session_ref:
                    self._interactive_turn_stats.pop(actor, None)

    def _record_interactive_turn_reports(
        self, actor: str, session_ref: str, report_ids: list[str]
    ) -> InteractiveTurnStats:
        """Count idempotent Stop pulses at daemon receipt time."""

        received_at_ms = int(time.time() * 1000)
        with self._interactive_turn_stats_lock:
            current = self._interactive_turn_stats.get(actor)
            if current is None or current.session_ref != session_ref:
                current = InteractiveTurnStats(
                    session_ref=session_ref,
                    counting_since_ms=received_at_ms,
                    turn_count=0,
                    last_turn_ended_at_ms=None,
                )
            new_ids = frozenset(report_ids) - current.observed_report_ids
            updated = InteractiveTurnStats(
                session_ref=session_ref,
                counting_since_ms=current.counting_since_ms,
                turn_count=current.turn_count + len(new_ids),
                last_turn_ended_at_ms=(
                    received_at_ms
                    if new_ids
                    else current.last_turn_ended_at_ms
                ),
                observed_report_ids=current.observed_report_ids | new_ids,
            )
            self._interactive_turn_stats[actor] = updated
            return updated

    def _fence_interactive_session(
        self, actor: str, params: JsonObject
    ) -> str:
        """Reject an MCP child after a newer TUI owns the actor registration.

        Returns the canonical actor the request must continue under. An
        owner-segment-only relocation -- the owner migration rewrote this same
        session's stored address -- is NOT a supersede: the surviving carrier
        keeps serving under the migrated (canonical) spelling, so its fetches,
        replies, and acks key on the rows the migration left behind (#352).
        """

        if "sessionRef" not in params:
            return actor
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        self._reject_retired_session_ref(actor, session_ref)
        current = next(
            (
                item
                for item in self._agent_session_domains.session.read_sessions()
                if item.actor == actor
            ),
            None,
        )
        if current is None or current.session_ref != session_ref:
            # A daemon-managed worker owns no interactive session; accept its
            # pre-registered worker session so its own harness_send/read pass.
            if self._worker_session_ref(actor) == session_ref:
                return actor
            if current is not None:
                # A *different* interactive session now owns this actor: a newer
                # register superseded this one (last-writer-wins, keyed by actor).
                # This is a terminal ownership verdict -- the losing stdio Channel
                # child must go quiet, never re-register. A distinct code lets it
                # tell this apart from "no session registered" below, which stays
                # a transient re-register signal so a daemon restart still self
                # heals. Keep in sync with hyprial.mcp.api.SESSION_SUPERSEDED_CODE.
                raise DaemonRequestError(
                    ipc_errors.SESSION_SUPERSEDED,
                    # Never name the current owner's session_ref: it is the
                    # credential that authenticates that actor's sends.
                    f"actor {actor} is now owned by another interactive session",
                )
            relocated = next(
                (
                    item
                    for item in self._agent_session_domains.session.read_sessions()
                    if item.session_ref == session_ref
                ),
                None,
            )
            if relocated is not None:
                if not owner_only_relocation(actor, relocated.actor):
                    raise DaemonRequestError(
                        ipc_errors.SESSION_SUPERSEDED,
                        f"interactive session {session_ref} moved from actor {actor} "
                        f"to {relocated.actor}",
                    )
                # Same session, owner migration only: continue under the
                # canonical spelling the state now carries.
                return relocated.actor
            raise DaemonRequestError(
                ipc_errors.STALE_SESSION,
                f"interactive session {session_ref} no longer owns actor {actor}",
            )
        # This is an ownership fence, not a liveness signal.  Only the explicit
        # session.heartbeat operation possessing the opaque lease token may
        # renew a carrier; ordinary identity/inbox traffic cannot keep a dead
        # channel online accidentally.
        return actor

    def _interactive_actor_liveness(self, actor: str) -> bool | None:
        """Liveness verdict for an actor this daemon speaks for, or None.

        A7 convergence: this used to be one of three unrelated judgements and
        answered only for interactive-session actors. It now delegates to the
        single AgentLiveness model, which folds in the harness supervisor's
        real subprocess state as well. None still means "not this daemon's to
        judge" -- _LocalPresence then falls back to the raw zenoh token, which
        is the right answer for a genuinely remote peer whose own daemon owns
        that verdict.
        """

        return self._agent_liveness.verdict(actor)

    def _interactive_session_status_projection(
        self, session: Any, now: float
    ) -> JsonObject:
        runtime = self._agent_session_domains.session.read_runtime(session.actor)
        last_observed = None
        if (
            runtime is not None
            and runtime.current_epoch is not None
            and runtime.last_heartbeat_ms is not None
        ):
            last_observed = (
                session.session_ref,
                runtime.current_epoch,
                runtime.last_heartbeat_ms / 1000,
            )
        return _interactive_session_status(
            session,
            current_epoch=None if runtime is None else runtime.current_epoch,
            online=(self._presence.actor_online(session.actor) if self._presence else False),
            last_observed=last_observed,
            now=now,
        )

    def _registered_interactive_actors(self) -> frozenset[str]:
        """Actors with a persisted interactive session, for the liveness model."""

        return frozenset(
            session.actor
            for session in self._agent_session_domains.session.read_sessions()
        )

    def _channel_current_epoch(
        self, actor: str, session_ref: str
    ) -> str | None:
        runtime = self._agent_session_domains.session.read_runtime(actor)
        if runtime is None or runtime.session_ref != session_ref:
            return None
        return runtime.current_epoch
