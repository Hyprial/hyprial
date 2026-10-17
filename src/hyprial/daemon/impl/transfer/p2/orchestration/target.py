"""AT07 I2: the target side of a P2 transfer, driven through the P2 ledger.

The identity half already exists: ``transfer.land`` mints a fresh incarnation,
moves the staged payload into the home that incarnation owns, materialises the
manifest's grants, and undoes whatever it did if any step fails
(``daemon/impl/transfer/landing/registry.py``).  What did not exist is the
driver that *records* that landing and then rebinds the target's runtime
context, so that a crash between "the row exists" and "the agent can run" is
readable afterwards instead of being guessed.

Two durable steps, the vocabulary the C2 journal already reserved for the
target role (``transfer_p2_steps``, same table, same primary key, same writer):

``[0] sandbox smoke`` (not journalled -- it runs before any row exists)
    the harness's own sandbox entry point is proved to enforce on this target;
    ``FAIL`` refuses here, ``NOT_RUN`` proceeds and is carried in the outcome,
    exactly as the P0 receive path does it.

``land``
    the bundle becomes a local agent.  Fingerprint = the plan digest the
    admission was opened with, so a resumed operation can tell "this landing"
    from "another landing that happened to reach the same name".

``activate``
    the landed config is published to the agent row and one runtime context per
    harness is resolved (native projection, session root, tool roots) under the
    fresh entity token.  This is the P22 seam (``resolve_agent_runtime_context``
    / ``AgentRuntimeMigrationBindings``), not a second copy of it.

Ordering rules this module exists to enforce:

* nothing here reads or writes the **source** -- undoing a failed target
  landing must never destroy, retire, or touch a source agent;
* a step is recorded *after* its owner accepted the work, with the owner's own
  receipt, and a failed step leaves a bounded ``needs_attention`` row rather
  than a silently re-runnable pending one;
* replay of an accepted operation returns the recorded outcome without
  re-executing it (the C2 "one request, one execution, one receipt" rule).

The callers that lift the P0 refusals are a *later* increment: this slice lands
the driver and its evidence while every not-yet-proven path keeps refusing with
its original code.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from hyprial.daemon.impl.transfer.execution.sandbox_smoke import (
    SMOKE_FAIL,
    SmokeResult,
)
from hyprial.daemon.impl.transfer.landing.owner import LandingResult
from hyprial.daemon.impl.transfer.p2.journal import MAX_ATTENTION_REASON
from hyprial.daemon.impl.transfer.landing.registry import land_bundle_into_registry
from hyprial.daemon.impl.transfer.landing.signature_gate import SignaturePolicy
from hyprial.daemon.impl.transfer.p2.model import Role
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore
from .lift import ENTRYPOINT_HEADLESS, ENTRYPOINT_TUI


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


class SandboxSmoke(Protocol):
    """The card's ``[0]`` mapping-time smoke, run on this target.

    It owns its own verdict shape (``SmokeResult``); this driver only refuses
    on ``FAIL`` and carries the status into the recorded outcome.
    """

    def __call__(self) -> SmokeResult: ...


class ActivationPort(Protocol):
    """The context switch a target landing needs, owned by P22/P23.

    ``activate`` receives the incarnation the landing minted, never a token the
    caller picked, and must refuse when the row's token is no longer that one.
    ``deactivate`` takes back only what ``activate`` did.
    """

    def activate(self, actor: str, entity_token: str) -> ActivationReceipt: ...

    def deactivate(self, actor: str, entity_token: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TargetLanding:
    """The readable outcome of one target-side landing."""

    operation_id: str
    actor: str
    entity_token: str
    phases: tuple[str, ...]
    grants: tuple[str, ...]
    files: int
    bytes: int
    harnesses: tuple[str, ...]
    replayed: bool
    sandbox_smoke: str
    #: The chain's entrypoint.  Activation is position-only either way; the
    #: value is recorded so a TUI's landed identity is never read as headless.
    entrypoint: str = ENTRYPOINT_HEADLESS

    @property
    def active(self) -> bool:
        return self.phases == ("landed", "active")

    def to_json(self) -> dict[str, object]:
        return {
            "operationId": self.operation_id,
            "actor": self.actor,
            "entityToken": self.entity_token,
            "phases": list(self.phases),
            "grants": list(self.grants),
            "files": self.files,
            "bytes": self.bytes,
            "harnesses": list(self.harnesses),
            "replayed": self.replayed,
            "sandboxSmoke": self.sandbox_smoke,
            "entrypoint": self.entrypoint,
        }


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
        tool_profile: AgentToolProfile,
        base_environment: Mapping[str, str],
        cwd: str | None = None,
        containerized: bool = False,
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
            # Fenced, single-column write: a read/merge/save of the whole row
            # would silently roll back whatever another writer changed
            # meanwhile (model, capabilities, cwd, last session...).
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
            # Undoing a row that is gone or replaced is refused loudly: the
            # caller's undo step reports it instead of writing over somebody
            # else's incarnation.
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


def _land_receipt(result: LandingResult) -> str:
    return f"landed:{result.actor}:{result.files}:{result.bytes}:{len(result.grants)}"


def _active_receipt(receipt: ActivationReceipt, files: int, byte_count: int) -> str:
    """The recorded outcome, including what the landing reported.

    The receipt is the only durable witness a replay can read back, so it
    carries the counts; a resumed operation must not have to invent zeros.
    """

    return f"active:{','.join(receipt.harnesses)}:{files}:{byte_count}"


def _parse_active_receipt(receipt: str) -> tuple[tuple[str, ...], int, int] | None:
    parts = receipt.split(":")
    if len(parts) != 4 or parts[0] != "active":
        return None
    harnesses = tuple(part for part in parts[1].split(",") if part)
    try:
        return harnesses, int(parts[2]), int(parts[3])
    except ValueError:
        return None


def land_and_activate(
    operation_id: str,
    *,
    store: AdmissionStore,
    bundle_dir: str | Path,
    registry: object,
    home_root: str | Path,
    machine_home: str | Path | None = None,
    staging: str | Path,
    granted_by: str,
    activation: ActivationPort,
    sandbox_smoke: SandboxSmoke,
    expected_actor: str | None = None,
    cwd: str | None = None,
    harness: str | None = None,
    signature: SignaturePolicy,
    entrypoint: str = ENTRYPOINT_HEADLESS,
) -> TargetLanding:
    """Land one received bundle on this target, then rebind its runtime context.

    The admission must already exist and be admissible: admitting an operator is
    an authority decision this module does not make.  Every step is recorded in
    the C2 ledger and a failure leaves the operation visibly unresolved instead
    of quietly retryable.

    ``entrypoint`` is recorded, never acted on by a launch: activation only
    positions the landed identity/home for either entrypoint.  A TUI is started
    afterwards by the target's own CLI (``target_tui.drive_target_start``), so
    the daemon still never launches an interactive TUI.
    """

    if entrypoint not in (ENTRYPOINT_HEADLESS, ENTRYPOINT_TUI):
        raise TargetLandingError(
            "invalid-entrypoint", f"unknown transfer entrypoint {entrypoint!r}"
        )

    # Card [0]: the harness's own sandbox entry point is smoked on this target
    # *before* an identity row exists, so a sandbox that cannot be shown to
    # enforce refuses with nothing to undo -- the same order and the same
    # non-veto rule for NOT_RUN as the P0 receive path.
    smoke = sandbox_smoke()
    if smoke.status == SMOKE_FAIL:
        raise TargetLandingError(
            "sandbox-smoke-failed",
            f"{smoke.harness}: the target refused the sandbox smoke: {smoke.detail}",
        )

    admission = store.get(operation_id)
    if admission is None:
        raise TargetLandingError("unknown-operation", f"no admitted operation {operation_id!r}")
    binding = admission.binding
    if binding.role is not Role.TARGET:
        raise TargetLandingError(
            "not-a-target-operation", f"{operation_id!r} is a {binding.role.value} operation"
        )
    actor = binding.target_actor
    journal = store.journal

    existing = journal.get(operation_id)
    if existing is not None and existing.needs_attention:
        # A failed step stays visible; it is never silently retried as if the
        # pending row were merely an interrupted attempt.
        raise TargetLandingError(
            "needs-attention",
            f"{operation_id!r} is waiting on an operator decision "
            f"({existing.phase}/{existing.step})",
        )
    if existing is not None and existing.step == "activate" and existing.phase == "activate-pending":
        # The landing was recorded and the activation began, but no result was
        # recorded: whether the context switch reached the row is unknown, so
        # this is neither a landing to redo nor an activation to replay.  The
        # window is named here instead of dying inside ``journal.begin`` as an
        # unmapped phase conflict, which is what a killed process leaves.
        raise TargetLandingError(
            "activate-unresolved",
            f"{operation_id!r} has an activation intent with no recorded result "
            f"(incarnation {existing.entity_token!r})",
        )
    if existing is not None and existing.phase == "active":
        recorded = _parse_active_receipt(existing.receipt)
        harnesses, files, byte_count = recorded if recorded is not None else ((), 0, 0)
        return TargetLanding(
            operation_id=operation_id,
            actor=actor,
            entity_token=existing.entity_token,
            phases=("landed", "active"),
            # Grant ids are only known to the attempt that materialised them;
            # the counts are read back from the receipt it recorded.
            grants=(),
            files=files,
            bytes=byte_count,
            harnesses=harnesses,
            replayed=True,
            sandbox_smoke=smoke.status,
            entrypoint=entrypoint,
        )

    land_start = journal.begin(
        operation_id, step="land", fingerprint=binding.plan_digest, actor=actor
    )
    replayed = not land_start.execute
    if land_start.execute:
        try:
            result = land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=home_root,
                machine_home=machine_home,
                staging=staging,
                granted_by=granted_by,
                expected_actor=expected_actor if expected_actor is not None else actor,
                cwd=cwd,
                harness=harness,
                signature=signature,
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            _record_attention(
                journal, operation_id, land_start.state, action="land-failed", error=error
            )
            raise
        landed = registry.require(actor)
        token = landed.entity_token
        land_state = journal.finish(
            operation_id,
            expected=land_start.state,
            receipt=_land_receipt(result),
            entity_token=token,
        )
        grants = tuple(result.grants)
        files = result.files
        byte_count = result.bytes
    else:
        land_state = land_start.state
        token = land_state.entity_token
        grants = ()
        files = 0
        byte_count = 0

    unresolved = journal.get(operation_id)
    if unresolved is not None and unresolved.step == "land" and unresolved.phase == "land-pending":
        # The landing intent survived without a result: whether the target home
        # exists is unknown, so this is not an activation problem to report as
        # an incarnation mismatch.
        raise TargetLandingError(
            "land-unresolved",
            f"{operation_id!r} has a landing intent with no recorded result",
        )

    activate_start = journal.begin(
        operation_id,
        step="activate",
        fingerprint=f"landed:{binding.plan_digest}",
        actor=actor,
    )
    if not activate_start.execute:
        recorded = _parse_active_receipt(activate_start.state.receipt)
        harnesses, recorded_files, recorded_bytes = (
            recorded if recorded is not None else ((), files, byte_count)
        )
        return TargetLanding(
            operation_id=operation_id,
            actor=actor,
            entity_token=token,
            phases=("landed", "active"),
            grants=grants,
            files=recorded_files,
            bytes=recorded_bytes,
            harnesses=harnesses,
            replayed=True,
            sandbox_smoke=smoke.status,
            entrypoint=entrypoint,
        )

    try:
        receipt = activation.activate(actor, token)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        problems = _undo_landing(activation, registry, actor, token, grants, granted_by)
        _record_attention(
            journal,
            operation_id,
            activate_start.state,
            action="activate-failed",
            error=error,
            problems=problems,
        )
        detail = f"{error}; undo problems: {'; '.join(problems)}" if problems else str(error)
        raise TargetLandingError("activation-failed", detail) from error
    journal.finish(
        operation_id,
        expected=activate_start.state,
        receipt=_active_receipt(receipt, files, byte_count),
        entity_token=token,
    )
    return TargetLanding(
        operation_id=operation_id,
        actor=actor,
        entity_token=token,
        phases=("landed", "active"),
        grants=grants,
        files=files,
        bytes=byte_count,
        harnesses=receipt.harnesses,
        replayed=replayed,
        sandbox_smoke=smoke.status,
        entrypoint=entrypoint,
    )


def _undo_landing(
    activation: ActivationPort,
    registry: object,
    actor: str,
    token: str,
    grants: tuple[str, ...],
    granted_by: str,
) -> list[str]:
    """Take back, in reverse, whatever this attempt did on the target."""

    problems: list[str] = []
    try:
        activation.deactivate(actor, token)
    except Exception as error:  # noqa: BLE001 - reported, never masked
        problems.append(f"deactivate {actor!r}: {error}")
    for grant_id in grants:
        try:
            registry.revoke_capability(
                actor, grant_id, revoked_by=granted_by, expected_entity_token=token
            )
        except Exception as error:  # noqa: BLE001 - reported, never masked
            problems.append(f"revoke grant {grant_id}: {error}")
    try:
        registry.destroy_landing_home(actor, expected_entity_token=token)
    except Exception as error:  # noqa: BLE001 - reported, never masked
        problems.append(f"destroy landed home {actor!r}: {error}")
    return problems


def _record_attention(
    journal: object,
    operation_id: str,
    state: object,
    *,
    action: str,
    error: BaseException,
    problems: tuple[str, ...] | list[str] = (),
) -> None:
    reason = f"{type(error).__name__}: {error}"
    if problems:
        reason = f"{reason}; undo problems: {'; '.join(problems)}"
    # The audit column is bounded; the operator sentence is truncated, never
    # rejected, so a failure can always be recorded.
    reason = reason[:MAX_ATTENTION_REASON]
    journal.attention(
        operation_id,
        expected=state,
        event_id=f"{operation_id}:{action}",
        principal="target-landing",
        action=action,
        reason=reason,
    )
