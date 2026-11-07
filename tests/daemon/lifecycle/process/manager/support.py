from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted, BindAgentCommand
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.correlation.correlation import CompletionReceipt
from hyprial.daemon.impl.harnesses.runtime.ports import (
    HarnessLaunchProjection,
    HarnessMutationCompleted,
)
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import (
    LifecycleMutationFailed,
    MutationProvenance,
)
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.lifecycle_manager import (
    LifecyclePorts,
    LifecycleProcessManager,
    LifecycleSpec,
    SessionLifecycleSpec,
)
from hyprial.daemon.impl.network.route_ports import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteMutationCompleted,
    RouteMutationFailed,
    RouteSpec,
)
from hyprial.daemon.impl.operations.session_ports import (
    SessionMutationCompleted,
    SessionMutationProjection,
)


class _CompletionPorts:
    def __init__(
        self,
        router: CorrelationEventRouter,
        *,
        fail_at: int | None = None,
        block_at: int | None = None,
        block_effect_token: str | None = None,
        overloads: int = 0,
        close_at: int | None = None,
        no_change_at: int | None = None,
        receipts: dict[str, MutationProvenance] | None = None,
        current_tokens: dict[str, str] | None = None,
        retired_receipts: dict[str, str] | None = None,
        failure_code: str = "INJECTED",
        harness_failure_settlement: str = "failed",
        refuse_live_bind: bool = False,
    ) -> None:
        self.router = router
        self.fail_at = fail_at
        self.block_at = block_at
        # Permanently block every submission whose attempt token contains this
        # substring (e.g. ":forward:0"): its completion is never published, so
        # the step stays unresolved across re-drives -- a truly stuck effect,
        # unlike block_at which only blocks the first attempt. Card 104164aa (c).
        self.block_effect_token = block_effect_token
        self.overloads = overloads
        self.close_at = close_at
        self.no_change_at = no_change_at
        self.attempts = 0
        self.release = threading.Event()
        self.calls: list[object] = []
        self.generation = 1
        self.version = 0
        self.receipts = receipts if receipts is not None else {}
        self.current_tokens = current_tokens if current_tokens is not None else {}
        self.requests: list[LifecycleMutationRequest] = []
        self.retired_receipts = retired_receipts if retired_receipts is not None else {}
        self.failure_code = failure_code
        self.harness_failure_settlement = harness_failure_settlement
        self.refuse_live_bind = refuse_live_bind
        self.failure_controls: list[LifecycleMutationRequest] = []

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        receipt = self.receipts.get(attempt_token)
        if receipt is None:
            retired = self.retired_receipts.get(attempt_token)
            if retired is None:
                return False
            if retired != resource_token:
                raise ValueError("test retired receipt token mismatch")
            return True
        if receipt is not None and receipt.resource_token != resource_token:
            raise ValueError("test receipt token mismatch")
        self.receipts.pop(attempt_token, None)
        self.retired_receipts[attempt_token] = resource_token
        return True

    def confirm_receipt_retired(self, attempt_token: str, resource_token: str) -> None:
        retired = self.retired_receipts.get(attempt_token)
        if retired is None:
            return
        if retired != resource_token:
            raise ValueError("test retirement confirmation token mismatch")
        del self.retired_receipts[attempt_token]

    def submit(self, command: object) -> PortAdmission:
        if not isinstance(command, LifecycleMutationRequest):
            raise TypeError("test domain requires LifecycleMutationRequest")
        self.attempts += 1
        if self.overloads:
            self.overloads -= 1
            return PortAdmission.OVERLOADED
        self.requests.append(command)
        payload = command.payload
        self.calls.append(payload)
        index = len(self.calls) - 1
        if (
            self.refuse_live_bind
            and isinstance(payload, BindAgentCommand)
            and _resource_key(payload) in self.current_tokens
        ):
            self.router.publish(
                PortCommandRejected(
                    command.correlation_id,
                    "agent",
                    1,
                    1,
                    "AGENT_ALREADY_RUNNING",
                    "binding already has a live connector",
                )
            )
            return PortAdmission.ACCEPTED
        if self.block_effect_token is not None and (
            self.block_effect_token in _attempt_token(command)
        ):
            threading.Thread(
                target=self._publish_after_release, args=(command,), daemon=True
            ).start()
            return PortAdmission.ACCEPTED
        if self.close_at == index:
            return PortAdmission.CLOSING
        if self.block_at == index:
            threading.Thread(
                target=self._publish_after_release, args=(command,), daemon=True
            ).start()
            return PortAdmission.ACCEPTED
        if self.fail_at == index:
            if isinstance(payload, (EnsureRouteCommand, DropRouteCommand)):
                self.router.publish(
                    RouteMutationFailed(
                        payload.correlation_id,
                        payload.attempt_token,
                        payload.generation,
                        payload.version,
                        "ensure" if isinstance(payload, EnsureRouteCommand) else "drop",
                        payload.spec.route_id
                        if isinstance(payload, EnsureRouteCommand)
                        else payload.route_id,
                        self.failure_code,
                        "failure-at-step",
                    )
                )
            else:
                self.router.publish(
                    PortCommandRejected(
                        command.correlation_id,
                        "test",
                        1,
                        1,
                        self.failure_code,
                        "failure-at-step",
                    )
                )
            return PortAdmission.ACCEPTED
        self.router.publish(
            self._completion(command, changed=index != self.no_change_at)
        )
        return PortAdmission.ACCEPTED

    def fail_lifecycle(
        self,
        request: LifecycleMutationRequest,
        *,
        code: str,
        detail: str,
        timeout: float,
    ) -> LifecycleMutationCompleted | LifecycleMutationFailed:
        del timeout
        self.failure_controls.append(request)
        if self.harness_failure_settlement == "timeout":
            raise TimeoutError("injected missing harness acknowledgement")
        if self.harness_failure_settlement == "completed":
            return self._completion(request)  # type: ignore[return-value]
        return LifecycleMutationFailed(
            request.correlation_id,
            request.attempt_token,
            self.generation,
            self.version,
            "harness",
            code,
            detail,
            True,
        )

    def reassociate(
        self,
        attempt_token: str,
        *,
        correlation_id: str,
        generation: int,
        version: int,
    ) -> bool:
        return False

    def _publish_after_release(self, command: LifecycleMutationRequest) -> None:
        self.release.wait()
        self.router.publish(self._completion(command))

    def _completion(
        self, command: LifecycleMutationRequest, *, changed: bool = True
    ) -> object:
        attempt = _attempt_token(command)
        resource_key = _resource_key(command.payload)
        provenance = self.receipts.get(attempt)
        expected = command.expected_resource_token
        if expected is not None:
            current = self.current_tokens.get(resource_key, expected)
            if current != expected:
                changed = False
                provenance = MutationProvenance(False, False, current)
            else:
                changed = True
                provenance = MutationProvenance(True, True, expected)
                self.current_tokens.pop(resource_key, None)
        if provenance is None:
            provenance = MutationProvenance(
                changed,
                changed,
                f"resource:{attempt}",
            )
            self.receipts[attempt] = provenance
        elif provenance.changed != changed:
            provenance = MutationProvenance(
                provenance.created_by_operation,
                changed,
                provenance.resource_token,
            )
        self.receipts[attempt] = provenance
        if expected is None and provenance.created_by_operation:
            self.current_tokens[resource_key] = provenance.resource_token
        return _completion(command, changed=changed, provenance=provenance)


class _Registration:
    def close(self) -> None:
        return None


class _RouteTransport:
    def __init__(self) -> None:
        self.liveliness_calls = 0
        self.queryable_calls = 0

    def declare_liveliness(self, key: str) -> _Registration:
        self.liveliness_calls += 1
        return _Registration()

    def declare_queryable(self, key: str, handler: object) -> _Registration:
        self.queryable_calls += 1
        return _Registration()


class _StaleThenCurrentPorts(_CompletionPorts):
    def __init__(self, router: CorrelationEventRouter) -> None:
        super().__init__(router)
        self.generation = 2
        self.sent_stale = False

    def submit(self, command: object) -> PortAdmission:
        if not isinstance(command, LifecycleMutationRequest):
            raise TypeError("test domain requires LifecycleMutationRequest")
        self.requests.append(command)
        self.calls.append(command.payload)
        event = self._completion(command)
        if isinstance(command.payload, (EnsureRouteCommand, DropRouteCommand)):
            self.router.publish(event)  # type: ignore[arg-type]
            return PortAdmission.ACCEPTED
        if not self.sent_stale:
            self.sent_stale = True
            assert (
                self.router.publish(
                    replace(event, generation=1),  # type: ignore[arg-type]
                )
                is CompletionReceipt.STALE
            )
        self.router.publish(replace(event, generation=2))  # type: ignore[arg-type]
        return PortAdmission.ACCEPTED


class _FailIndicesPorts(_CompletionPorts):
    def __init__(
        self,
        router: CorrelationEventRouter,
        indices: set[int],
        *,
        failure_code: str,
    ) -> None:
        super().__init__(router, failure_code=failure_code)
        self._indices = indices

    def submit(self, command: object) -> PortAdmission:
        self.fail_at = len(self.calls) if len(self.calls) in self._indices else None
        return super().submit(command)


class _CrashBeforeFirstCompletion(_CompletionPorts):
    def submit(self, command: object) -> PortAdmission:
        if not isinstance(command, LifecycleMutationRequest):
            raise TypeError("test domain requires LifecycleMutationRequest")
        self.requests.append(command)
        self.calls.append(command.payload)
        attempt = _attempt_token(command)
        self.receipts[attempt] = MutationProvenance(True, True, f"resource:{attempt}")
        raise RuntimeError("effect applied, process manager crashed before completion")


class _ReplaceBeforeFailurePorts(_CompletionPorts):
    def __init__(
        self,
        router: CorrelationEventRouter,
        *,
        fail_at: int,
        resource_key: str,
    ) -> None:
        super().__init__(router, fail_at=fail_at)
        self._replacement_index = fail_at
        self._resource_key = resource_key

    def submit(self, command: object) -> PortAdmission:
        if len(self.calls) == self._replacement_index:
            self.current_tokens[self._resource_key] = "external-replacement-token"
        return super().submit(command)


class _CompletionGate:
    def __init__(self, router: CorrelationEventRouter) -> None:
        self.router = router
        self.allow = False
        self.calls = 0

    def publish(self, event: object) -> CompletionReceipt:
        self.calls += 1
        if not self.allow:
            return CompletionReceipt.OVERLOADED
        return self.router.publish(event)  # type: ignore[arg-type]


def _attempt_token(command: object) -> str:
    if isinstance(command, LifecycleMutationRequest):
        return command.attempt_token
    if isinstance(command, (EnsureRouteCommand, DropRouteCommand)):
        return command.attempt_token
    return str(command.correlation_id).removeprefix("lifecycle:")  # type: ignore[attr-defined]


def _resource_key(command: object) -> str:
    if isinstance(command, EnsureRouteCommand):
        return f"route:{command.spec.route_id}"
    if isinstance(command, DropRouteCommand):
        return f"route:{command.route_id}"
    name = type(command).__name__
    if name in {"CreateAgentCommand", "DestroyAgentCommand"}:
        return f"agent:{getattr(command, 'name')}"
    if name in {"BindAgentCommand", "ReleaseAgentCommand"}:
        return f"binding:{getattr(command, 'actor')}"
    if name == "EnsureHarnessCommand":
        spec = getattr(command, "spec")
        return f"harness:{spec.harness}:{spec.name}"
    if name == "RemoveHarnessCommand":
        return f"harness:{getattr(command, 'harness')}:{getattr(command, 'name')}"
    if name in {"RegisterSessionCommand", "UnregisterSessionCommand"}:
        return f"session:{getattr(command, 'actor')}"
    raise TypeError(f"unsupported lifecycle test resource: {name}")


def _completion(
    command: object,
    *,
    changed: bool = True,
    provenance: MutationProvenance | None = None,
) -> object:
    request = command if isinstance(command, LifecycleMutationRequest) else None
    payload_command = request.payload if request is not None else command
    correlation = (
        request.correlation_id if request is not None else command.correlation_id  # type: ignore[attr-defined]
    )
    attempt = _attempt_token(command)
    receipt = provenance or MutationProvenance(changed, changed, f"resource:{attempt}")
    if isinstance(payload_command, (EnsureRouteCommand, DropRouteCommand)):
        return RouteMutationCompleted(
            correlation,
            payload_command.attempt_token,
            payload_command.generation,
            payload_command.version,
            "ensure" if isinstance(payload_command, EnsureRouteCommand) else "drop",
            payload_command.spec.route_id
            if isinstance(payload_command, EnsureRouteCommand)
            else payload_command.route_id,
            changed,
            receipt,
        )
    module = type(payload_command).__module__
    if module == BindAgentCommand.__module__:
        payload = AgentMutationCompleted(
            correlation, 1, 1, type(payload_command).__name__, changed
        )
        domain = "agent"
    elif module.endswith("session_ports"):
        registered = type(payload_command).__name__ == "RegisterSessionCommand"
        payload = SessionMutationCompleted(
            correlation,
            1,
            1,
            SessionMutationProjection(
                "agent:o:n:a",
                "s1",
                "epoch",
                registered=registered and changed,
                unregistered=not registered and changed,
            ),
        )
        domain = "session"
    else:
        payload = HarnessMutationCompleted(correlation, 1, 1, "codex:worker", changed)
        domain = "harness"
    return LifecycleMutationCompleted(
        correlation,
        attempt,
        1,
        1,
        domain,
        receipt,
        payload,
    )


def _spec(name: str = "worker") -> LifecycleSpec:
    actor = f"agent:owner:node:{name}"
    return LifecycleSpec(
        agent_name=name,
        actor=actor,
        harness=HarnessLaunchProjection("codex", name, True),
        route=RouteSpec(actor, f"live/{actor}", f"inbox/{actor}"),
        session=SessionLifecycleSpec(
            "/tmp", ("/bin/worker",), "test", f"session-{name}"
        ),
    )


_TEST_MANAGERS: list[LifecycleProcessManager] = []


def _drain_test_managers() -> None:
    while _TEST_MANAGERS:
        _TEST_MANAGERS.pop().drain(5.0)


@pytest.fixture(autouse=True)
def _close_test_managers():  # type: ignore[no-untyped-def]
    """The factory owns every manager it creates, on pass and failure alike."""

    yield
    _drain_test_managers()


#: Budget for waiting on an operation's TERMINAL state.  ``wait`` only polls
#: the durable result -- it never changes what the saga does -- so this bound
#: is not a latency claim: a green run returns as soon as the state lands.  The
#: old literal 2s went red under CI I/O load (tasks 15697, 15727: "lifecycle
#: operation still running"); with every state-db transaction slowed by 0.1s
#: locally, 13 of 33 tests in this file failed only on that budget.  Waits that
#: must time out (0.0 / 0.1 / 0.2 inside ``pytest.raises``) keep their literals.
#: One ruler with the product: the application waits for a lifecycle result
#: for the operation deadline plus its wait margin, and so do these tests.
TERMINAL_WAIT_SECONDS = LIFECYCLE_OPERATION_DEADLINE_SECONDS + LIFECYCLE_WAIT_MARGIN_SECONDS

#: The caller-side timeout for a wait that is EXPECTED to expire
#: (``pytest.raises(TimeoutError)``): with the gate closed no operation can
#: complete, so any positive bound must elapse with TimeoutError.  That makes
#: this a ``must_time_out`` -- the bound is part of what is being proven (an
#: absence of completion), so it stays small and literal.  No product
#: constant declares how long a caller may wait before giving up, so this is
#: not ``from_product``; it is named so the claim is visible in review.
EXPECTED_TIMEOUT_SECONDS = 0.2


def _manager(
    path: Path,
    ports: _CompletionPorts,
    router: CorrelationEventRouter,
    *,
    completion_timeout: float = 0.2,
    **kwargs: object,
) -> LifecycleProcessManager:
    manager = LifecycleProcessManager(
        path,
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=completion_timeout,
        **kwargs,  # type: ignore[arg-type]
    )
    _TEST_MANAGERS.append(manager)
    return manager






















def _settles(predicate, timeout: float = 5.0, step: float = 0.01) -> bool:  # type: ignore[no-untyped-def]
    """Bounded poll on the assertion's own object (card 2389ace2, family A).

    The fault hooks below raise mid-saga to simulate a daemon death; the tests
    used to give that crash 100-200 ms of wall clock (``first.wait(key, 0.2)``)
    before asserting the post-crash state, and under a full unit run the saga
    had not reached the crash yet.  Wait for the asserted state itself.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()










































































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'EXPECTED_TIMEOUT_SECONDS',
    'TERMINAL_WAIT_SECONDS',
    '_CompletionGate',
    '_CompletionPorts',
    '_CrashBeforeFirstCompletion',
    '_FailIndicesPorts',
    '_Registration',
    '_ReplaceBeforeFailurePorts',
    '_RouteTransport',
    '_StaleThenCurrentPorts',
    '_TEST_MANAGERS',
    '_attempt_token',
    '_close_test_managers',
    '_completion',
    '_drain_test_managers',
    '_manager',
    '_resource_key',
    '_settles',
    '_spec',
]
