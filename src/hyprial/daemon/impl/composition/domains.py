"""AgentSessionDomains: the agent/session actor domain facade composed at daemon startup."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from hyprial.kernel import DomainEffectClaim
from hyprial.identity import (
    AgentActor,
    AgentRegistry,
)
from hyprial.identity import (
    AgentEvent,
    AgentLifecycleReceiptCompleted,
    RetireAgentLifecycleReceiptCommand,
    ConfirmAgentLifecycleReceiptCommand,
)
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
    ConfirmLifecycleReceiptCommand,
    LifecycleReceiptCompleted,
    RetireLifecycleReceiptCommand,
)
from hyprial.daemon.impl.desired_state  import DesiredStateStore
from hyprial.daemon.impl.harnesses.claude.runtime import prepare_claude_runtime_context
from hyprial.daemon.impl.harnesses.codex.native_env import prepare_codex_runtime_context
from hyprial.daemon.impl.session_actor  import SessionActor

from .events import (
    CorrelatedDomainEvents,
    DomainCommandError,
    _AGENT_RECEIPT_NAMESPACE,
    _EventT,
    _check_domain_receipt,
)
from .views import (
    AgentDirectoryFacade,
    AgentLivenessProjectionFacade,
)


def _import_legacy_session_agents(
    registry: AgentRegistry, desired_state: DesiredStateStore
) -> tuple[str, ...]:
    """Materialize local Agent records missing from pre-Agent session state.

    Before the Agent entity existed, ``session.register`` persisted only an
    interactive session.  Big-bang SessionActor recovery correctly requires
    every session to name a real local Agent, so a direct upgrade must bridge
    that one historical shape before either actor starts.  Foreign canonical
    URIs remain foreign and fail closed; existing records are never rewritten.
    """

    imported: list[str] = []
    source_harness = {
        "claude-channel": "claude",
        "pi-extension": "pi",
        "codex-app-server": "codex",
    }
    for session in desired_state.load().interactive_sessions:
        actor = registry.local_actor(session.actor)
        if actor is None or registry.exists(actor):
            continue
        runtime = (session.runtime or "").split("_", 1)[0]
        preferred = (
            runtime
            if runtime in {"claude", "pi", "codex"}
            else source_harness.get(session.source)
        )
        registry.create(
            actor,
            cwd=session.cwd,
            preferred_harness=preferred,
        )
        imported.append(f"interactiveSession:{session.actor}")
    return tuple(imported)


class AgentSessionDomains:
    """Own the private Agent registry and the Agent/Session actor fan-out."""

    def __init__(
        self,
        *,
        database: Path,
        desired_state: DesiredStateStore,
        owner: str,
        node_id: str,
        daemon_epoch: str,
        hyprial_home: Path,
        worker_running: Callable[..., bool | None],
        clock: Callable[[], float],
        persistence_late_result: Callable[[str], object | None] | None = None,
        session_desired_state: object | None = None,
    ) -> None:
        registry = AgentRegistry(
            database,
            owner=owner,
            machine=node_id,
            hyprial_home=hyprial_home,
        )
        imported_sessions = _import_legacy_session_agents(registry, desired_state)
        self.imported_legacy = (*registry.imported_legacy, *imported_sessions)
        self._registry = registry
        self._domains_closed = False
        self._close_lock = threading.Lock()
        self._agent_events = CorrelatedDomainEvents()
        self._session_events = CorrelatedDomainEvents()
        self._session: SessionActor | None = None

        def agent_event(event: AgentEvent) -> None:
            self._agent_events.publish(event)
            session = self._session
            if session is not None:
                session.accept_agent_event(event)

        def session_originated(correlation_id: str) -> bool:
            session = self._session
            return session is not None and session.owns_agent_correlation(
                correlation_id
            )

        self.agent = AgentActor(
            registry,
            event_sink=agent_event,
            desired_state=desired_state,
            worker_running=worker_running,
            clock=clock,
            session_originated=session_originated,
            home_runtime_preparers={
                "claude": prepare_claude_runtime_context,
                "codex": prepare_codex_runtime_context,
            },
        )
        self.session = SessionActor(
            desired_state if session_desired_state is None else session_desired_state,
            daemon_epoch=daemon_epoch,
            event_sink=self._session_events.publish,
            agent_commands=self.agent,
            clock=clock,
            persistence_late_result=persistence_late_result,
        )
        self._session = self.session
        self.agents = AgentDirectoryFacade(self)
        self.liveness = AgentLivenessProjectionFacade(self)

    def call_agent(
        self,
        command: object,
        expected: type[_EventT],
        *,
        timeout: float = 5.0,
    ) -> _EventT:
        admission = self.agent.submit(command)  # type: ignore[arg-type]
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"agent command admission is {admission.value}",
            )
        return self._agent_events.wait(
            str(getattr(command, "correlation_id")), expected, timeout=timeout
        )

    def call_agent_settled(
        self,
        command: object,
        expected: type[_EventT],
    ) -> _EventT:
        """Submit once and join that exact Agent completion until settlement."""

        correlation_id = str(getattr(command, "correlation_id"))
        self._agent_events.reserve_settlement(correlation_id)
        try:
            admission = self.agent.submit(command)  # type: ignore[arg-type]
        except BaseException:
            self._agent_events.cancel_settlement(correlation_id)
            raise
        if admission is not PortAdmission.ACCEPTED:
            self._agent_events.cancel_settlement(correlation_id)
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"agent command admission is {admission.value}",
            )
        return self._agent_events.wait_settled(
            correlation_id, expected
        )

    def call_session(
        self,
        command: object,
        expected: type[_EventT],
        *,
        timeout: float = 5.0,
    ) -> _EventT:
        admission = self.session.submit(command)  # type: ignore[arg-type]
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"session command admission is {admission.value}",
            )
        return self._session_events.wait(
            str(getattr(command, "correlation_id")), expected, timeout=timeout
        )

    def wait_agent_lifecycle(
        self, correlation_id: str, expected: type[_EventT]
    ) -> _EventT:
        return self._agent_events.wait(correlation_id, expected, timeout=5.0)

    def wait_session_lifecycle(
        self, correlation_id: str, expected: type[_EventT]
    ) -> _EventT:
        return self._session_events.wait(correlation_id, expected, timeout=5.0)

    def retire_agent_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        generation = self.agent.generation
        event = self.call_agent(
            RetireAgentLifecycleReceiptCommand(
                f"{_AGENT_RECEIPT_NAMESPACE}:retire:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            AgentLifecycleReceiptCompleted,
        )
        self._check_agent_receipt(
            event, generation, attempt_token, resource_token, "retire"
        )
        return event.matched

    def expire_lifecycle_receipts(self) -> int:
        """U0c startup sweep passthrough (see AgentRegistry)."""

        return self._registry.expire_lifecycle_receipts()

    def lifecycle_effect_claims(self) -> list["DomainEffectClaim"]:
        """U0c backfill claims passthrough (see AgentRegistry)."""

        return self._registry.lifecycle_effect_claims()

    def confirm_agent_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> None:
        generation = self.agent.generation
        event = self.call_agent(
            ConfirmAgentLifecycleReceiptCommand(
                f"{_AGENT_RECEIPT_NAMESPACE}:confirm:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            AgentLifecycleReceiptCompleted,
        )
        self._check_agent_receipt(
            event, generation, attempt_token, resource_token, "confirm"
        )

    @staticmethod
    def _check_agent_receipt(
        event: AgentLifecycleReceiptCompleted,
        generation: int,
        attempt_token: str,
        resource_token: str,
        operation: str,
    ) -> None:
        if (
            event.generation != generation
            or event.attempt_token != attempt_token
            or event.resource_token != resource_token
            or event.operation != operation
        ):
            raise DomainCommandError(
                "STALE_AGENT_RECEIPT", "agent receipt completion fence mismatch"
            )

    def retire_session_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        generation = self.session.generation
        event = self.call_session(
            RetireLifecycleReceiptCommand(
                f"session:receipt:retire:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
        )
        _check_domain_receipt(
            event, "session", generation, attempt_token, resource_token, "retire"
        )
        return event.matched

    def confirm_session_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> None:
        generation = self.session.generation
        event = self.call_session(
            ConfirmLifecycleReceiptCommand(
                f"session:receipt:confirm:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
        )
        _check_domain_receipt(
            event, "session", generation, attempt_token, resource_token, "confirm"
        )

    def close(self, timeout: float = 5.0) -> bool:
        with self._close_lock:
            if self._domains_closed:
                return True
            started = time.monotonic()
            session = self.session.drain(timeout * 0.6)
            if not session.complete:
                return False
            remaining = max(0.0, timeout - (time.monotonic() - started))
            agent = self.agent.drain(remaining)
            if not agent.complete:
                return False
            self._session_events.close()
            self._agent_events.close()
            self._registry.close()
            self._domains_closed = True
            return True
