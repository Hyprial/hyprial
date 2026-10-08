from __future__ import annotations

from hyprial.identity.impl.agents.registry._base import Agent
from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.identity.impl.agents.runtime.context import AgentRuntimeContext
from hyprial.identity.impl.agents.runtime.context import AgentToolProfile
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from hyprial.identity.impl.agents.home.environment import apply_runtime_environment_profile
from hyprial.identity.impl.agents.runtime.context import resolve_agent_runtime_context

from ._base import (
    AgentHomeMigrationError,
    AgentMigrationPlan,
    _is_within,
)


class AgentMigrationBindings(Protocol):
    """P22/P23-owned context switch consumed by the P24 transaction."""

    def preflight(self, plan: AgentMigrationPlan) -> None: ...

    def activate(self, plan: AgentMigrationPlan) -> None: ...

    def rollback(self, plan: AgentMigrationPlan) -> None: ...

    def legacy_sources_in_use(self, plan: AgentMigrationPlan) -> tuple[str, ...]: ...
class AgentMigrationLivenessProbe(Protocol):
    """Return True only after observing this exact incarnation is stopped."""

    def __call__(self, agent_uri: str, entity_token: str) -> bool | None: ...
class AgentRuntimeMigrationBindings:
    """Bind a published migration through P22's one resolved context seam.

    This class deliberately owns no root construction.  P22 resolves every
    root, projection, and environment selector; P24 only checks that the
    returned incarnation is the one authorized by the plan and that no legacy
    source remains in any resolved root or applied launch environment.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        harnesses: Iterable[str],
        tool_profile: AgentToolProfile,
        base_environment: Mapping[str, str],
        cwd: str | None = None,
        containerized: bool = False,
    ) -> None:
        selected = tuple(harnesses)
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("migration runtime harnesses must be non-empty and unique")
        if any(harness not in {"claude", "codex", "pi"} for harness in selected):
            raise ValueError("migration runtime harness must be claude, codex, or pi")
        self.registry = registry
        self.harnesses = selected
        self.tool_profile = tool_profile
        self.base_environment = dict(base_environment)
        self.cwd = cwd
        self.containerized = containerized
        self._contexts: tuple[AgentRuntimeContext, ...] = ()
        self._environments: dict[str, dict[str, str]] = {}

    @property
    def contexts(self) -> tuple[AgentRuntimeContext, ...]:
        return self._contexts

    @property
    def environments(self) -> dict[str, dict[str, str]]:
        return {
            harness: dict(environment)
            for harness, environment in self._environments.items()
        }

    def preflight(self, plan: AgentMigrationPlan) -> None:
        agent = self.registry.require(plan.authorization.actor)
        if agent.uri != plan.agent_uri or agent.entity_token != (
            plan.authorization.entity_token
        ):
            raise AgentHomeMigrationError(
                "binding-identity-mismatch", "preflight-bindings", actor=agent.actor
            )
        declared = {key.harness for key in plan.support}
        if set(self.harnesses) - declared:
            raise AgentHomeMigrationError(
                "binding-support-missing", "preflight-bindings", actor=agent.actor
            )

    def activate(self, plan: AgentMigrationPlan) -> None:
        agent = self.registry.require(plan.authorization.actor)
        receipt = self.registry.home_receipt(agent.actor)
        if any(entry.entry.destination == "config" for entry in plan.entries):
            agent = self.registry.update(
                agent.actor,
                config=AgentConfig(str(Path(receipt.path) / "config")),
            )
        cwd = self.cwd if self.cwd is not None else agent.cwd
        contexts: list[AgentRuntimeContext] = []
        environments: dict[str, dict[str, str]] = {}
        for harness in self.harnesses:
            context = resolve_agent_runtime_context(
                registry=self.registry,
                agent_name=agent.actor,
                harness=harness,
                cwd=cwd,
                tool_profile=self.tool_profile,
                containerized=self.containerized,
            )
            if context is None:
                raise AgentHomeMigrationError(
                    "runtime-context-unavailable",
                    "activate-bindings",
                    actor=agent.actor,
                )
            self._validate_context(context, agent, Path(receipt.path))
            environment = apply_runtime_environment_profile(
                self.base_environment,
                context.environment(),
            )
            if any(environment.get(name) != value for name, value in context.environment().items()):
                raise AgentHomeMigrationError(
                    "runtime-environment-mismatch",
                    "activate-bindings",
                    actor=agent.actor,
                )
            contexts.append(context)
            environments[harness] = environment
        self._contexts = tuple(contexts)
        self._environments = environments

    def rollback(self, plan: AgentMigrationPlan) -> None:
        agent = self.registry.require(plan.authorization.actor)
        self.registry.update(agent.actor, config=plan.prior_config)
        self._contexts = ()
        self._environments = {}

    def legacy_sources_in_use(self, plan: AgentMigrationPlan) -> tuple[str, ...]:
        if len(self._contexts) != len(self.harnesses):
            return tuple(snapshot.entry.source for snapshot in plan.entries)
        candidates: list[Path] = []
        for context in self._contexts:
            candidates.extend(
                (
                    context.roots.agent_home,
                    context.roots.config_source,
                    context.roots.projection_root,
                    context.roots.native_root,
                    context.roots.session_root,
                    context.roots.tool_home,
                    context.roots.xdg_config_home,
                    context.roots.xdg_cache_home,
                    context.roots.xdg_data_home,
                    context.roots.xdg_state_home,
                    context.roots.tool_profile_root,
                )
            )
            candidates.extend(
                Path(value)
                for value in self._environments[context.harness].values()
                if Path(value).is_absolute()
            )
        return tuple(
            snapshot.entry.source
            for snapshot in plan.entries
            if any(
                candidate == Path(snapshot.entry.source)
                or _is_within(candidate, Path(snapshot.entry.source))
                for candidate in candidates
            )
        )

    @staticmethod
    def _validate_context(
        context: AgentRuntimeContext, agent: Agent, agent_home: Path
    ) -> None:
        if (
            context.actor != agent.uri
            or context.entity_token != agent.entity_token
            or context.roots.agent_home != agent_home
            or agent.config is None
            or context.roots.config_source != Path(agent.config.source)
        ):
            raise AgentHomeMigrationError(
                "runtime-context-mismatch", "activate-bindings", actor=agent.actor
            )
        for root in (
            context.roots.projection_root,
            context.roots.native_root,
            context.roots.session_root,
            context.roots.tool_home,
            context.roots.xdg_config_home,
            context.roots.xdg_cache_home,
            context.roots.xdg_data_home,
            context.roots.xdg_state_home,
            context.roots.tool_profile_root,
        ):
            if not _is_within(root, agent_home):
                raise AgentHomeMigrationError(
                    "runtime-root-escape", "activate-bindings", actor=agent.actor
                )
