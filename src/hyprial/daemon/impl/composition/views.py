"""Agent projection views and the harness launch projection helpers."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import uuid
from dataclasses import replace
from hyprial.identity import (
    Agent,
    AgentBinding,
    AgentRegistry,
    HandoverNotice,
    PinConflictError,
)
from hyprial.identity import HomePayloadFile
from hyprial.identity import (
    AgentCapabilityGrantCompleted,
    GrantAgentCapabilityCommand,
    RevokeAgentCapabilityCommand,
    AgentDestroySettled,
    AgentMutationCompleted,
    AgentProjection,
    BindAgentCommand,
    BlockAgentCommand,
    ClearRestoreDispositionCommand,
    CreateAgentCommand,
    CreateHostInvitedAgentCommand,
    CreateTransferHostedAgentCommand,
    DestroyAgentCommand,
    SettleAgentDestroyCommand,
    PinAgentAdapterCommand,
    RecordAgentActivityCommand,
    RecordAgentSessionRefCommand,
    RetireAgentSessionRefsCommand,
    RollbackRetiredAgentSessionRefsCommand,
    SetRestoreDispositionCommand,
    ReleaseAgentCommand,
    UnpinAgentAdapterCommand,
    UnblockAgentCommand,
    UpdateAgentCommand,
)
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.harnesses.runtime.ports import (
    HarnessLaunchProjection,
    AgentIdentityProjectionPort,
)
from hyprial.kernel import HarnessLaunchSpec

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only: domains constructs these facades at runtime; the
    # back-reference here is typing, not a runtime cycle.
    from .domains import AgentSessionDomains
from .events import (
    DomainCommandError,
)


def harness_launch_projection(spec: HarnessLaunchSpec) -> HarnessLaunchProjection:
    return HarnessLaunchProjection(
        harness=spec.harness,
        name=spec.name,
        headless=spec.headless,
        args=spec.args,
        ownership=spec.ownership,
        nickname=spec.nickname,
        cwd=spec.cwd,
        endpoint=spec.endpoint,
        session_ref=spec.session_ref,
        command=spec.command,
        turn_timeout_seconds=spec.turn_timeout_seconds,
        idle_timeout_seconds=spec.idle_timeout_seconds,
        containerized=spec.containerized,
        pinned_owner=spec.pinned_owner,
        container_image=spec.container_image,
        execution_runtime=spec.execution_runtime,
        model_provider=spec.model_provider,
        model=spec.model,
    )


class AgentDirectoryFacade:
    """Compatibility-shaped system edge; every mutation is an Agent command."""

    def __init__(self, domains: AgentSessionDomains) -> None:
        self._domains = domains
        self.owner = domains._registry.owner
        self.machine = domains._registry.machine

    @property
    def imported_legacy(self) -> tuple[str, ...]:
        return self._domains.imported_legacy

    @staticmethod
    def normalize_actor(value: str) -> str:
        return AgentRegistry.normalize_actor(value)

    def native_actor(self, value: str) -> str:
        return self._domains._registry.native_actor(value)

    def local_actor(self, value: str) -> str | None:
        from hyprial.kernel import parse_agent_uri

        parsed = parse_agent_uri(value)
        if parsed is not None:
            if parsed[0] != self.owner or parsed[1] != self.machine:
                hosted = self._domains.agent.read_agent(parsed[2])
                return (
                    hosted.actor if hosted is not None
                    and hosted.uri == value and hosted.hosted_by is not None
                    else None
                )
            candidate = parsed[2]
        elif ":" in value:
            return None
        else:
            candidate = value
        try:
            return self.normalize_actor(candidate)
        except Exception:
            return None

    def uri_for(self, actor: str) -> str:
        from hyprial.kernel import canonical_agent_uri

        name = self.normalize_actor(actor)
        hosted = self._domains.agent.read_agent(name)
        if hosted is not None and hosted.hosted_by is not None:
            return hosted.uri
        return canonical_agent_uri(self.owner, self.machine, name)

    def get(self, actor: str) -> Agent | None:
        local = self.local_actor(actor)
        if local is None:
            return None
        projection = self._domains.agent.read_agent(local)
        return None if projection is None else _agent(projection)

    def projection(self, actor: str) -> AgentProjection | None:
        local = self.local_actor(actor)
        return None if local is None else self._domains.agent.read_agent(local)

    def is_blocked(self, actor: str) -> bool:
        projection = self.projection(actor)
        return bool(projection is not None and projection.block is not None)

    def require(self, actor: str) -> Agent:
        item = self.get(actor)
        if item is None:
            raise DomainCommandError(
                ipc_errors.AGENT_NOT_FOUND,
                f"no agent named {actor!r} on this machine",
            )
        return item

    def exists(self, actor: str) -> bool:
        return self.get(actor) is not None

    def list(self) -> tuple[Agent, ...]:
        return tuple(_agent(item) for item in self._domains.agent.read_agents())

    def projections(self) -> tuple[AgentProjection, ...]:
        return self._domains.agent.read_agents()

    def local_actors(self) -> tuple[str, ...]:
        return tuple(item.uri for item in self._domains.agent.read_agents())

    def pins(self) -> dict[str, str]:
        return {
            adapter: item.uri
            for item in self._domains.agent.read_agents()
            for adapter in item.pinned_adapters
        }

    def create(
        self,
        actor: str,
        *,
        cwd: str | None = None,
        config: object = None,
        provider: str | None = None,
        model: str | None = None,
        capabilities: object = None,
        harness_args: object = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        command = CreateAgentCommand(
                correlation_id=f"agent-command:create:{uuid.uuid4().hex}",
            name=actor,
            cwd=cwd,
            config=config,
            provider=provider,
            model=model,
            capabilities=_capability_pairs(capabilities),
            harness_args=_harness_pairs(harness_args),
            preferred_harness=preferred_harness,
        )
        event = self._domains.call_agent(command, AgentMutationCompleted)
        assert event.agent is not None
        return _agent(event.agent)

    def create_transfer_hosted(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: object = None, preferred_harness: str | None = None,
    ) -> Agent:
        event = self._domains.call_agent(
            CreateTransferHostedAgentCommand(
                correlation_id=f"agent-command:transfer-hosted:{uuid.uuid4().hex}",
                name=actor, pinned_owner=pinned_owner, cwd=cwd,
                harness_args=_harness_pairs(harness_args),
                preferred_harness=preferred_harness,
            ),
            AgentMutationCompleted,
        )
        assert event.agent is not None
        return _agent(event.agent)

    def create_host_invited(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: object = None, preferred_harness: str | None = None,
    ) -> Agent:
        correlation_id = f"agent-command:host-invite:{uuid.uuid4().hex}"
        entity_token = uuid.uuid4().hex
        try:
            event = self._domains.call_agent_settled(
                CreateHostInvitedAgentCommand(
                    correlation_id=correlation_id,
                    name=actor, pinned_owner=pinned_owner,
                    entity_token=entity_token, cwd=cwd,
                    harness_args=_harness_pairs(harness_args),
                    preferred_harness=preferred_harness,
                ),
                AgentMutationCompleted,
            )
        except DomainCommandError as error:
            if error.code not in {
                ipc_errors.AGENT_CREATE_COMMIT_FAILED,
                ipc_errors.AGENT_CREATE_ROLLBACK_PENDING,
            }:
                raise
            try:
                settlement = self.settle_destroy(
                    f"{correlation_id}:rollback", actor,
                    expected_entity_token=entity_token,
                )
                if settlement.disposition not in {"destroyed", "already-cleaned"}:
                    raise DomainCommandError(
                        ipc_errors.AGENT_CREATE_ROLLBACK_PENDING,
                        f"Agent invite {correlation_id} rollback is "
                        f"{settlement.disposition}",
                    )
            except BaseException as cleanup_error:
                raise DomainCommandError(
                    ipc_errors.AGENT_CREATE_ROLLBACK_PENDING,
                    f"Agent invite {correlation_id} failed ({error}); "
                    f"entity {entity_token} cleanup remains unsettled: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}",
                ) from cleanup_error
            if error.code == ipc_errors.AGENT_CREATE_ROLLBACK_PENDING:
                raise DomainCommandError(
                    ipc_errors.AGENT_CREATE_COMMIT_FAILED,
                    f"Agent invite {correlation_id} creation failed and its "
                    f"entity {entity_token} cleanup settled: {error.detail}",
                ) from error
            raise
        assert event.agent is not None
        if event.agent.entity_token != entity_token:
            raise DomainCommandError(
                "DOMAIN_CORRELATION_CONFLICT",
                "host invite completed for a different Agent incarnation",
            )
        return _agent(event.agent)

    def save(self, agent: Agent) -> Agent:
        command = UpdateAgentCommand(
                correlation_id=f"agent-command:update:{uuid.uuid4().hex}",
            name=agent.actor,
            cwd=agent.cwd,
            config=None if agent.config is None else agent.config.to_json(),
            provider=agent.provider,
            model=agent.model,
            capabilities=tuple(sorted(agent.capabilities.items())),
            harness_args=tuple(
                (name, tuple(args)) for name, args in sorted(agent.harness_args.items())
            ),
            preferred_harness=agent.preferred_harness,
            last_harness=agent.last_harness,
            last_session_id=agent.last_session_id,
        )
        event = self._domains.call_agent(command, AgentMutationCompleted)
        assert event.agent is not None
        return _agent(event.agent)

    def record_session(
        self, actor: str, *, harness: str, session_id: str | None
    ) -> HandoverNotice | None:
        agent = self.require(actor)
        notice = (
            HandoverNotice(
                actor=agent.actor,
                previous_harness=agent.last_harness,
                previous_session_id=agent.last_session_id,
                next_harness=harness,
            )
            if agent.last_harness is not None and agent.last_harness != harness
            else None
        )
        self.save(
            replace(
                agent,
                last_harness=harness,
                last_session_id=session_id,
                preferred_harness=agent.preferred_harness or harness,
            )
        )
        return notice

    def record_activity(self, actor: str) -> bool:
        event = self._domains.call_agent(
            RecordAgentActivityCommand(
                correlation_id=f"agent-command:activity:{uuid.uuid4().hex}",
                actor=actor,
            ),
            AgentMutationCompleted,
        )
        return event.changed

    def suppress_restore(
        self,
        actor: str,
        *,
        desired_generation: str,
        last_active_at_ms: int | None,
        idle_age_ms: int | None,
        restore_threshold_ms: int,
        restore_override: str = "none",
        activity_unknown: bool = False,
    ) -> Agent:
        event = self._domains.call_agent(
            SetRestoreDispositionCommand(
                f"agent-command:suppress-restore:{uuid.uuid4().hex}",
                actor,
                desired_generation,
                last_active_at_ms,
                idle_age_ms,
                restore_threshold_ms,
                restore_override,
                activity_unknown,
            ),
            AgentMutationCompleted,
        )
        assert event.agent is not None
        return _agent(event.agent)

    def clear_restore_disposition(
        self,
        actor: str,
        *,
        expected_entity_token: str | None = None,
        expected_desired_generation: str | None = None,
        expected_disposition_token: str | None = None,
    ) -> bool:
        return self._domains.call_agent(
            ClearRestoreDispositionCommand(
                f"agent-command:clear-restore:{uuid.uuid4().hex}",
                actor,
                expected_entity_token,
                expected_desired_generation,
                expected_disposition_token,
            ),
            AgentMutationCompleted,
        ).changed

    def block_agent(
        self,
        actor: str,
        *,
        reason: str,
        expected_entity_token: str | None = None,
    ) -> tuple[Agent, bool]:
        event = self._domains.call_agent(
            BlockAgentCommand(
                f"agent-command:block:{uuid.uuid4().hex}",
                actor,
                reason,
                expected_entity_token,
            ),
            AgentMutationCompleted,
        )
        assert event.agent is not None
        return _agent(event.agent), event.changed

    def unblock_agent(self, actor: str) -> bool:
        return self._domains.call_agent(
            UnblockAgentCommand(
                f"agent-command:unblock:{uuid.uuid4().hex}", actor
            ),
            AgentMutationCompleted,
        ).changed

    def destroy_landing_home(self, actor: str, *, expected_entity_token: str) -> bool:
        event = self.settle_destroy(
            f"landing-rollback:{uuid.uuid4().hex}", actor,
            expected_entity_token=expected_entity_token,
        )
        if event.disposition == "stale-incarnation":
            raise DomainCommandError("AGENT_VERSION_CONFLICT", "landing incarnation was replaced")
        return True

    def materialise_landing_home(
        self, actor: str, staged_root: str, *, expected_entity_token: str,
        expected_files: tuple[HomePayloadFile, ...],
    ) -> str:
        return self._domains.agent.materialise_landing_home(
            actor, staged_root, expected_entity_token=expected_entity_token,
            expected_files=expected_files,
        )

    def confirm_landing_home(self, actor: str, *, expected_entity_token: str) -> str:
        return self._domains.agent.confirm_landing_home(
            actor, expected_entity_token=expected_entity_token,
        )

    def grant_capability(
        self, actor: str, *, grant_id: str, capability: str, scope: str,
        granted_by: str, revision: int, expected_entity_token: str | None = None,
    ) -> object:
        event = self._domains.call_agent(
            GrantAgentCapabilityCommand(
                f"agent-command:grant:{uuid.uuid4().hex}", actor, grant_id,
                capability, scope, granted_by, revision, expected_entity_token,
            ),
            AgentCapabilityGrantCompleted,
        )
        assert event.grant is not None
        return event.grant

    def revoke_capability(
        self, actor: str, grant_id: str, *, revoked_by: str,
        expected_entity_token: str | None = None,
    ) -> bool:
        return self._domains.call_agent(
            RevokeAgentCapabilityCommand(
                f"agent-command:revoke:{uuid.uuid4().hex}", actor, grant_id,
                revoked_by, expected_entity_token,
            ),
            AgentCapabilityGrantCompleted,
        ).changed

    def destroy(
        self, actor: str, *, expected_entity_token: str | None = None
    ) -> bool:
        event = self._domains.call_agent(
            DestroyAgentCommand(
            f"agent-command:destroy:{uuid.uuid4().hex}",
            actor,
            expected_entity_token,
            ),
            AgentMutationCompleted,
        )
        return event.changed

    def record_session_ref(
        self, actor: str, entity_token: str, session_ref: str
    ) -> None:
        self._domains.call_agent(
            RecordAgentSessionRefCommand(
                f"agent-command:record-session-ref:{uuid.uuid4().hex}",
                actor,
                entity_token,
                session_ref,
            ),
            AgentMutationCompleted,
        )

    def retire_session_refs(
        self,
        actor: str,
        entity_token: str,
        session_refs: tuple[str, ...],
        *,
        reason: str,
        destroy_attempt: str,
    ) -> bool:
        return self._domains.call_agent(
            RetireAgentSessionRefsCommand(
                f"agent-command:retire-session-refs:{uuid.uuid4().hex}",
                actor,
                entity_token,
                session_refs,
                reason,
                destroy_attempt,
            ),
            AgentMutationCompleted,
        ).changed

    def rollback_retired_session_refs(self, destroy_attempt: str) -> bool:
        return self._domains.call_agent(
            RollbackRetiredAgentSessionRefsCommand(
                f"agent-command:rollback-retired-session-refs:{uuid.uuid4().hex}",
                destroy_attempt,
            ),
            AgentMutationCompleted,
        ).changed

    def is_session_ref_retired(self, actor: str, session_ref: str) -> bool:
        return self._domains.agent.read_session_ref_retired(actor, session_ref)

    def settle_destroy(
        self,
        correlation_id: str,
        actor: str,
        *,
        expected_entity_token: str,
    ) -> AgentDestroySettled:
        """Submit once and join exact Agent/home cleanup settlement."""

        event = self._domains.call_agent_settled(
            SettleAgentDestroyCommand(
                correlation_id,
                actor,
                expected_entity_token,
            ),
            AgentDestroySettled,
        )
        if (
            event.actor != actor
            or event.expected_entity_token != expected_entity_token
        ):
            raise DomainCommandError(
                "DOMAIN_CORRELATION_CONFLICT",
                "Agent destroy settlement does not match the requested entity",
            )
        return event

    def pin(self, adapter: str, actor: str) -> str | None:
        pins = self.pins()
        previous = pins.get(adapter)
        try:
            event = self._domains.call_agent(
                PinAgentAdapterCommand(
            f"agent-command:pin:{uuid.uuid4().hex}", actor, adapter
                ),
                AgentMutationCompleted,
            )
        except DomainCommandError as error:
            if error.code != PinConflictError.code:
                raise
            agent = self.require(actor)
            holder = next(
                (name for name, target in pins.items() if target == agent.uri),
                "(unknown)",
            )
            raise PinConflictError(agent.uri, adapter, holder) from error
        return previous if event.changed else previous

    def restore_pin_if_absent(self, adapter: str, actor: str) -> str | None:
        """Restore one snapshot without overwriting a concurrent replacement."""

        previous = self.pins().get(adapter)
        event = self._domains.call_agent(
            PinAgentAdapterCommand(
                f"agent-command:restore-pin:{uuid.uuid4().hex}",
                actor,
                adapter,
                None,
                True,
            ),
            AgentMutationCompleted,
        )
        return previous if event.changed else previous

    def unpin(self, adapter: str) -> str | None:
        previous = self.pins().get(adapter)
        self._domains.call_agent(
            UnpinAgentAdapterCommand(
            f"agent-command:unpin:{uuid.uuid4().hex}", adapter
            ),
            AgentMutationCompleted,
        )
        return previous

    def unpin_if(self, adapter: str, expected_actor: str | None) -> str | None:
        previous = self.pins().get(adapter)
        self._domains.call_agent(
            UnpinAgentAdapterCommand(
                f"agent-command:unpin-if:{uuid.uuid4().hex}",
                adapter,
                expected_actor,
                True,
            ),
            AgentMutationCompleted,
        )
        return previous

    def close(self) -> None:
        self._domains.close()


class AgentIdentityProjectionView(AgentIdentityProjectionPort):
    """Narrow cache-only Agent incarnation port for other domain owners."""

    def __init__(self, domains: AgentSessionDomains) -> None:
        self._agent = domains.agent

    def entity_token(self, actor: str) -> str | None:
        projection = self._agent.read_agent(actor)
        return None if projection is None else projection.entity_token


class AgentLivenessProjectionFacade:
    """Read projections plus typed bind/release mutations."""

    def __init__(self, domains: AgentSessionDomains) -> None:
        self._domains = domains

    def binding(self, actor: str) -> AgentBinding | None:
        projection = self._domains.agent.read_binding(actor)
        return None if projection is None else _binding(projection)

    def bindings(self) -> tuple[AgentBinding, ...]:
        values = []
        for agent in self._domains.agent.read_agents():
            binding = self._domains.agent.read_binding(agent.uri)
            if binding is not None:
                values.append(_binding(binding))
        return tuple(values)

    def live_binding(self, actor: str) -> AgentBinding | None:
        runtime = self._domains.agent.read_runtime(actor)
        if runtime.online is False or runtime.binding is None:
            return None
        return _binding(runtime.binding)

    def displaced_by(
        self, actor: str, *, harness: str, runtime: str
    ) -> AgentBinding | None:
        binding = self.binding(actor)
        if binding is None or binding.key == (harness, runtime):
            return None
        return binding

    def bind(
        self,
        actor: str,
        *,
        harness: str,
        runtime: str,
        session_id: str | None = None,
    ) -> AgentBinding:
        event = self._domains.call_agent(
            BindAgentCommand(
                f"agent-command:bind:{uuid.uuid4().hex}",
                actor,
                harness,
                runtime,
                session_id,
            ),
            AgentMutationCompleted,
        )
        assert event.binding is not None
        return _binding(event.binding)

    def release(self, actor: str) -> AgentBinding | None:
        previous = self.binding(actor)
        self._domains.call_agent(
            ReleaseAgentCommand(
                f"agent-command:release:{uuid.uuid4().hex}", actor
            ),
            AgentMutationCompleted,
        )
        return previous

    def touch(self, actor: str) -> None:
        binding = self.binding(actor)
        if binding is None:
            return
        self.bind(
            actor,
            harness=binding.harness,
            runtime=binding.runtime,
            session_id=binding.session_id,
        )

    def verdict(self, actor: str) -> bool | None:
        return self._domains.agent.read_runtime(actor).online

    def status(
        self, actor: str, desired_state: object | None = None
    ) -> str | None:
        """Same verdict as ``verdict``; ``desired_state`` is card 259's

        snapshot-scoped desired-state copy threaded down to the worker probe
        so one snapshot loads the store once.
        """

        runtime = self._domains.agent.read_runtime(actor, desired_state)
        return runtime.status if runtime.online is not None else None

    def registered_status(self, actor: str) -> str:
        return self._domains.agent.read_runtime(actor).status

    def snapshot(self, actor: str) -> dict[str, object]:
        return self._domains.agent.read_runtime(actor).to_payload()


def _agent(value: AgentProjection) -> Agent:
    return Agent(
        uri=value.uri,
        actor=value.actor,
        owner=value.owner,
        machine=value.machine,
        entity_token=value.entity_token,
        cwd=value.cwd,
        config=value.config_payload(),
        provider=value.provider,
        model=value.model,
        capabilities=value.capabilities_payload(),
        harness_args=dict(value.harness_args),
        preferred_harness=value.preferred_harness,
        last_harness=value.last_harness,
        last_session_id=value.last_session_id,
        last_active_at_ms=value.last_active_at_ms,
        pinned_adapters=value.pinned_adapters,
        created_at_ms=value.created_at_ms,
        hosted_by=value.hosted_by,
    )


def _binding(value: object) -> AgentBinding:
    return AgentBinding(
        actor=str(getattr(value, "actor")),
        harness=str(getattr(value, "harness")),
        runtime=str(getattr(value, "runtime")),
        session_id=getattr(value, "session_id"),
        bound_at_ms=int(getattr(value, "bound_at_ms")),
    )


def _capability_pairs(value: object) -> tuple[tuple[str, object], ...]:
    from hyprial.identity import normalize_capabilities

    return tuple(sorted(normalize_capabilities(value).items()))


def _harness_pairs(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(
        sorted((str(key), tuple(str(item) for item in items)) for key, items in value.items())
    )


__all__ = [
    "AgentDirectoryFacade",
    "AgentIdentityProjectionView",
    "AgentLivenessProjectionFacade",
    "harness_launch_projection",
]
