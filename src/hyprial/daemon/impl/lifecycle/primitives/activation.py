# Decision record (file header; no inline comments by repository rule):
# - Agent teleport slice 3: the runtime-context activation, its error types and
#   its port moved here from ``transfer/p2/orchestration/target.py`` so a
#   teleport target can bind a landed home without importing the P2 journal,
#   admission store, SSH runner or orchestrator.  ``target.py`` re-exports the
#   same objects, so the P2 driver and its tests are unchanged.
# - ``TargetLandingError`` moved with ``ActivationError`` because the latter
#   subclasses it; ``target.py`` still raises it for its own fences.
# - ``activate`` publishes the landed config through a fenced, single-column
#   write (``bind_agent_config`` with the expected digest): a read/merge/save of
#   the whole row would silently roll back whatever another writer changed
#   meanwhile (model, capabilities, cwd, last session).
# - ``deactivate`` refuses loudly when the row is gone or replaced, so the
#   caller's undo step reports it instead of writing over somebody else's
#   incarnation.
"""Runtime-context activation of a landed agent home (P22 seam)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields as _dc_fields
from pathlib import Path
from typing import Protocol

from hyprial.identity import (
    AgentConfig,
    AgentConfigCasMismatch,
    AgentIncarnationMismatch,
    AgentRuntimeContext,
    AgentToolProfile,
    agent_config_digest,
    apply_runtime_environment_profile,
    resolve_agent_runtime_context,
)

class TargetLandingError(Exception):
    """The target side refused or failed; ``code`` names which fence spoke."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


class ActivationError(TargetLandingError):
    """The runtime context could not be rebound for the landed incarnation."""


@dataclass(frozen=True, slots=True)
class ActivationReceipt:
    """What the activation actually bound, as the owner reports it."""

    harnesses: tuple[str, ...]
    root_names: tuple[str, ...]


class ActivationPort(Protocol):
    """The context switch a target landing needs, owned by P22/P23.

    ``activate`` receives the incarnation the landing minted, never a token the
    caller picked, and must refuse when the row's token is no longer that one.
    ``deactivate`` takes back only what ``activate`` did.
    """

    def activate(self, actor: str, entity_token: str) -> ActivationReceipt: ...

    def deactivate(self, actor: str, entity_token: str) -> None: ...


class RuntimeContextActivation:
    """Bind a landed home through P22's one resolved context seam.

    This owns no root construction: ``resolve_agent_runtime_context`` decides
    every root and projection from the agent row plus the landed config, and
    the only thing this class adds is the check that what came back belongs to
    the incarnation the landing minted.
    """

    def __init__(
        self,
        registry: object,
        *,
        harnesses: tuple[str, ...],
        tool_profile: AgentToolProfile | None,
        base_environment: Mapping[str, str],
        cwd: str | None = None,
        containerized: bool = False,
        notice_reporter: Callable[[AgentRuntimeContext], None] | None = None,
    ) -> None:
        selected = tuple(harnesses)
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("activation harnesses must be non-empty and unique")
        if any(harness not in {"claude", "codex", "pi"} for harness in selected):
            raise ValueError("activation harness must be claude, codex, or pi")
        self.registry = registry
        self.harnesses = selected
        self.tool_profile = tool_profile
        self.base_environment = dict(base_environment)
        self.cwd = cwd
        self.containerized = containerized
        self._notice_reporter = notice_reporter
        self._published: tuple[str, AgentConfig | None] | None = None

    def activate(self, actor: str, entity_token: str) -> ActivationReceipt:
        agent = self.registry.require(actor)
        if agent.entity_token != entity_token:
            raise ActivationError(
                "incarnation-drift",
                f"{actor!r} is no longer the incarnation the landing minted",
            )
        home = Path(self.registry.home_receipt(actor).path)
        config_source = home / "config"
        if not config_source.is_dir():
            raise ActivationError(
                "no-landed-config",
                f"{actor!r} landed without a config directory to bind",
            )
        prior = agent.config
        self._published = (actor, prior)
        target_config = AgentConfig(str(config_source))
        if agent_config_digest(prior) != agent_config_digest(target_config):
            self._bind_config(actor, entity_token, agent_config_digest(prior), target_config)
        contexts: list[AgentRuntimeContext] = []
        for harness in self.harnesses:
            cwd = self.cwd if self.cwd is not None else agent.cwd
            context = resolve_agent_runtime_context(
                registry=self.registry,
                agent_name=actor,
                harness=harness,
                cwd=cwd,
                tool_profile=self.tool_profile,
                containerized=self.containerized,
            )
            if context is None:
                raise ActivationError(
                    "runtime-context-unavailable", f"{harness}: no P2 context"
                )
            self._validate_context(context, agent, home, config_source)
            if self._notice_reporter is not None:
                self._notice_reporter(context)
            environment = apply_runtime_environment_profile(
                self.base_environment, context.environment()
            )
            expected = context.environment()
            if any(environment.get(key) != value for key, value in expected.items()):
                raise ActivationError(
                    "runtime-environment-mismatch", f"{harness}: environment did not apply"
                )
            contexts.append(context)
        return ActivationReceipt(
            harnesses=tuple(context.harness for context in contexts),
            root_names=tuple(
                sorted({name for context in contexts for name, _root in _root_pairs(context)})
            ),
        )

    def deactivate(self, actor: str, entity_token: str) -> None:
        published, self._published = self._published, None
        agent = self.registry.get(actor)
        if agent is None or agent.entity_token != entity_token:
            raise ActivationError(
                "incarnation-drift",
                f"{actor!r} is no longer the incarnation this activation bound",
            )
        if published is not None and published[0] == actor:
            self._bind_config(
                actor, entity_token, agent_config_digest(agent.config), published[1]
            )

    def _bind_config(
        self, actor: str, entity_token: str, expected_digest: str, config: object
    ) -> None:
        try:
            self.registry.bind_agent_config(
                actor,
                expected_entity_token=entity_token,
                config=config,
                expected_current_config_digest=expected_digest,
            )
        except AgentIncarnationMismatch as error:
            raise ActivationError("incarnation-drift", str(error)) from error
        except AgentConfigCasMismatch as error:
            raise ActivationError("config-drift", str(error)) from error

    @staticmethod
    def _validate_context(
        context: AgentRuntimeContext, agent: object, home: Path, config_source: Path
    ) -> None:
        if (
            context.actor != agent.uri
            or context.entity_token != agent.entity_token
            or context.roots.agent_home != home
            or context.roots.config_source != config_source
        ):
            raise ActivationError(
                "runtime-context-mismatch",
                f"{context.harness}: resolved context is not this incarnation's",
            )
        for name, root in _root_pairs(context):
            if name == "agent_home":
                continue
            if root != home and home not in root.parents:
                raise ActivationError(
                    "runtime-root-escape", f"{context.harness}: {name} escapes the home"
                )


def _root_pairs(context: AgentRuntimeContext) -> tuple[tuple[str, Path], ...]:
    """Every root the resolved context carries, by name, for the two fences.

    Enumerated from the dataclass rather than a hand-written list so a root
    added to P22 cannot silently escape the "all roots live in the home" check.
    """

    roots = context.roots
    return tuple((field.name, getattr(roots, field.name)) for field in _dc_fields(roots))
