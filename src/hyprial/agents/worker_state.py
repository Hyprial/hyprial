"""Actor-owned restart policy and immutable restore eligibility facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from hyprial.actor_runtime import AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from hyprial.daemon.desired_state import HarnessLaunchSpec
from hyprial.persistent_config import atomic_json_write


RESTORE_THRESHOLD_MS = 12 * 60 * 60 * 1_000
RESTORE_POLICIES = frozenset({"active", "always", "never"})


class RestorePolicyError(ValueError):
    """The restore policy could not be read or written without guessing."""


@dataclass(frozen=True, slots=True)
class RestorePolicy:
    threshold_ms: int = RESTORE_THRESHOLD_MS
    agents: tuple[tuple[str, str], ...] = ()

    def policy_for(self, actor: str) -> str:
        return dict(self.agents).get(actor, "active")

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "restoreThresholdMs": self.threshold_ms,
            "agents": dict(self.agents),
        }


@dataclass(frozen=True, slots=True)
class RestorePolicyProjection:
    version: int
    policy: RestorePolicy
    degraded: str | None = None


@dataclass(frozen=True, slots=True)
class SetRestorePolicy:
    actor: str
    policy: str


@dataclass(frozen=True, slots=True)
class SetRestoreThreshold:
    threshold_ms: int


@dataclass(frozen=True, slots=True)
class RefreshRestorePolicy:
    pass


@dataclass(frozen=True, slots=True)
class _PolicyResult:
    policy: RestorePolicy
    changed: bool
    error: str | None = None


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: _PolicyResult | None = None


class _RestorePolicyStore:
    def __init__(self, path: Path, *, normalize: Callable[[str], str]) -> None:
        self.path = Path(path)
        self._normalize = normalize

    def load(self) -> RestorePolicy:
        if not self.path.exists():
            return RestorePolicy()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RestorePolicyError(
                f"cannot read agent restore policy {self.path}: {error}"
            ) from error
        if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
            raise RestorePolicyError("agent restore policy must use schemaVersion 1")
        threshold = raw.get("restoreThresholdMs")
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or threshold < 0
        ):
            raise RestorePolicyError(
                "agent restore policy restoreThresholdMs must be a non-negative integer"
            )
        agents = raw.get("agents", {})
        if not isinstance(agents, dict):
            raise RestorePolicyError("agent restore policy agents must be an object")
        normalized: dict[str, str] = {}
        for actor, policy in agents.items():
            if not isinstance(actor, str) or policy not in RESTORE_POLICIES:
                raise RestorePolicyError(
                    "agent restore policy entries must map names to active, always, or never"
                )
            try:
                name = self._normalize(actor)
            except (ValueError, RuntimeError) as error:
                raise RestorePolicyError(
                    f"invalid agent restore policy entry {actor!r}: {error}"
                ) from error
            normalized[name] = str(policy)
        return RestorePolicy(threshold, tuple(sorted(normalized.items())))

    def set_agent(self, actor: str, policy: str) -> RestorePolicy:
        if policy not in RESTORE_POLICIES:
            raise RestorePolicyError("policy must be active, always, or never")
        name = self._normalize(actor)
        current = self.load()
        agents = dict(current.agents)
        if policy == "active":
            agents.pop(name, None)
        else:
            agents[name] = policy
        updated = RestorePolicy(current.threshold_ms, tuple(sorted(agents.items())))
        atomic_json_write(self.path, updated.to_json())
        return updated

    def set_threshold(self, threshold_ms: int) -> RestorePolicy:
        if threshold_ms < 0:
            raise RestorePolicyError("restore threshold must not be negative")
        current = self.load()
        updated = RestorePolicy(threshold_ms, current.agents)
        atomic_json_write(self.path, updated.to_json())
        return updated


class RestorePolicyAuthority:
    """The sole policy-file owner; callers read one immutable cached snapshot."""

    def __init__(self, path: Path, *, normalize, capacity: int = 32, timeout=5.0):
        if capacity < 1:
            raise ValueError("restore policy capacity must be positive")
        self.path = Path(path)
        self._store = _RestorePolicyStore(self.path, normalize=normalize)
        try:
            initial = RestorePolicyProjection(0, self._store.load())
        except RestorePolicyError as error:
            initial = RestorePolicyProjection(0, RestorePolicy(), str(error))
        self._projection = initial
        self._version = 0
        self._guard = threading.Lock()
        self._pending: dict[str, _Reply] = {}
        self._closed = False
        self._timeout = timeout
        self._capacity = capacity
        self._effects: EffectLane | None = None

    def projection(self) -> RestorePolicyProjection:
        with self._guard:
            return self._projection

    def _execute(self, payload) -> _PolicyResult:
        try:
            before = self._store.load()
            if isinstance(payload, SetRestorePolicy):
                after = self._store.set_agent(payload.actor, payload.policy)
            elif isinstance(payload, SetRestoreThreshold):
                after = self._store.set_threshold(payload.threshold_ms)
            elif isinstance(payload, RefreshRestorePolicy):
                after = before
            else:
                raise TypeError("unsupported restore policy mutation")
            return _PolicyResult(after, after != before)
        except (OSError, RestorePolicyError) as error:
            return _PolicyResult(RestorePolicy(), False, str(error))

    def _receive(self, event) -> None:
        if not isinstance(event, EffectCompleted):
            raise TypeError("unsupported restore policy authority message")
        result = event.result or _PolicyResult(
            RestorePolicy(), False, event.error or "restore policy effect failed"
        )
        with self._guard:
            self._version += 1
            self._projection = RestorePolicyProjection(
                self._version,
                result.policy,
                result.error,
            )
            reply = self._pending.pop(event.operation_id, None)
            if reply is not None:
                reply.result = result
        effects = self._effects
        if effects is not None:
            effects.acknowledge(event.operation_id, event.generation)
        if reply is not None:
            reply.ready.set()

    def _call(self, payload, *, wait: bool = True) -> _PolicyResult | None:
        operation_id = uuid4().hex
        reply = _Reply()
        with self._guard:
            if self._closed:
                raise RestorePolicyError("restore policy authority is closed")
            if self._effects is None:
                self._effects = EffectLane(
                    name="agent-restore-policy-storage",
                    execute=self._execute,
                    complete=self._receive,
                    capacity=self._capacity,
                    workers=1,
                    retry_seconds=0.5,
                )
            effects = self._effects
            self._pending[operation_id] = reply
            admission = effects.submit(EffectRequest(operation_id, 1, payload))
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(operation_id, None)
                raise RestorePolicyError(
                    f"restore policy admission {admission.value}"
                )
        if not wait:
            return None
        if not reply.ready.wait(self._timeout):
            raise TimeoutError(
                f"restore policy operation {operation_id} remains accepted"
            )
        assert reply.result is not None
        if reply.result.error is not None:
            raise RestorePolicyError(reply.result.error)
        return reply.result

    def set_agent(self, actor: str, policy: str) -> RestorePolicy:
        result = self._call(SetRestorePolicy(actor, policy))
        assert result is not None
        return result.policy

    def set_threshold(self, threshold_ms: int) -> RestorePolicy:
        result = self._call(SetRestoreThreshold(threshold_ms))
        assert result is not None
        return result.policy

    def refresh(self, *, wait: bool = False) -> None:
        self._call(RefreshRestorePolicy(), wait=wait)

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                pending = bool(self._pending)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        effects = self._effects
        return bool(
            effects is None
            or effects.close(max(0.0, deadline - time.monotonic()))
        )


@dataclass(frozen=True, slots=True)
class _BlockingFailure:
    recipient: str
    code: str
    expected_entity_token: str


@dataclass(frozen=True, slots=True)
class _OwnerBlockNotice:
    text: str
    idempotency_key: str


class BlockingFailureAuthority:
    """Bounded retained bridge from settled delivery failure to Agent state."""

    def __init__(self, *, block, notify, capacity: int = 64):
        self._block = block
        self._notify = notify
        self._guard = threading.Lock()
        self._pending: dict[
            tuple[str, str, str],
            tuple[str, _BlockingFailure | _OwnerBlockNotice],
        ] = {}
        self._closed = False
        self._closing = False
        self._notified: OrderedDict[str, None] = OrderedDict()
        self._notice_history_capacity = capacity
        self._capacity = capacity
        self._effects: EffectLane | None = None

    def _effects_locked(self) -> EffectLane:
        if self._effects is None:
            self._effects = EffectLane(
                name="agent-blocking-failure",
                execute=self._execute,
                complete=self._complete,
                capacity=self._capacity,
                workers=1,
                retry_seconds=0.5,
            )
        return self._effects

    @staticmethod
    def _reason(code: str) -> str | None:
        return {
            "PROVIDER_USAGE_LIMIT": "provider-quota",
            "PROVIDER_AUTHENTICATION_FAILED": "credential-invalid",
        }.get(code)

    def submit(
        self, recipient: str, code: str, expected_entity_token: str
    ) -> AdmissionResult:
        if self._reason(code) is None:
            return AdmissionResult.ACCEPTED
        key = (recipient, code, expected_entity_token)
        with self._guard:
            if self._closed or self._closing:
                return AdmissionResult.CLOSED
            if key in self._pending:
                return AdmissionResult.ACCEPTED
            operation = f"agent-block:{uuid4().hex}"
            request = _BlockingFailure(recipient, code, expected_entity_token)
            result = self._effects_locked().submit(
                EffectRequest(operation, 1, request)
            )
            if result is AdmissionResult.ACCEPTED:
                self._pending[key] = (operation, request)
            return result

    def _execute(self, request):
        if isinstance(request, _OwnerBlockNotice):
            outcome = self._notify(
                request.text, idempotency_key=request.idempotency_key
            )
            return getattr(outcome, "delivered", None) is True
        reason = self._reason(request.code)
        if reason is None:
            return None
        committed = self._block(
            request.recipient, reason, request.expected_entity_token
        )
        if committed is None:
            return None
        if len(committed) == 3:
            agent, changed, committed_reason = committed
        else:
            agent, changed = committed
            committed_reason = reason
        return agent, changed, committed_reason

    def _complete(self, event: EffectCompleted[object]) -> None:
        with self._guard:
            owned_item = next(
                (
                    (key, value)
                    for key, value in self._pending.items()
                    if value[0] == event.operation_id
                ),
                None,
            )
        if owned_item is None:
            effects = self._effects
            if effects is not None:
                effects.acknowledge(event.operation_id, event.generation)
            return
        key, owned = owned_item
        _operation, request = owned
        effects = self._effects
        assert effects is not None
        effects.acknowledge(event.operation_id, event.generation)
        if event.error is not None:
            self._schedule_retry(key)
            return
        if isinstance(request, _OwnerBlockNotice):
            if event.result is not True:
                self._schedule_retry(key)
                return
            with self._guard:
                self._pending.pop(key, None)
                self._notified[request.idempotency_key] = None
                self._notified.move_to_end(request.idempotency_key)
                while len(self._notified) > self._notice_history_capacity:
                    self._notified.popitem(last=False)
            return
        result = event.result
        if not isinstance(result, tuple) or len(result) != 3:
            with self._guard:
                self._pending.pop(key, None)
            return
        agent, _changed, reason = result
        idempotency_key = f"agent-blocked:{agent.entity_token}:{reason}"
        with self._guard:
            already_notified = idempotency_key in self._notified
        if already_notified:
            with self._guard:
                self._pending.pop(key, None)
            return
        notice = _OwnerBlockNotice(
            f"Agent {agent.actor} is blocked and needs human action: {reason}.",
            idempotency_key,
        )
        self._submit_owned(key, notice)

    def _submit_owned(
        self,
        key: tuple[str, str, str],
        request: _BlockingFailure | _OwnerBlockNotice,
    ) -> None:
        with self._guard:
            if self._closed or key not in self._pending:
                return
            operation = f"agent-block:{uuid4().hex}"
            admitted = self._effects_locked().submit(
                EffectRequest(operation, 1, request)
            )
            if admitted is AdmissionResult.ACCEPTED:
                self._pending[key] = (operation, request)
                return
            self._pending[key] = ("", request)
        if admitted is not AdmissionResult.CLOSED:
            self._schedule_retry(key)

    def _schedule_retry(self, key: tuple[str, str, str]) -> None:
        timer = threading.Timer(0.1, self._retry, args=(key,))
        timer.daemon = True
        timer.start()

    def _retry(self, key: tuple[str, str, str]) -> None:
        with self._guard:
            current = self._pending.get(key)
        if current is not None:
            self._submit_owned(key, current[1])

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closing = True
        while True:
            with self._guard:
                pending = bool(self._pending)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        with self._guard:
            self._closed = True
        effects = self._effects
        return bool(
            effects is None
            or effects.close(max(0.0, deadline - time.monotonic()))
        )


def desired_generation(spec: HarnessLaunchSpec) -> str:
    encoded = json.dumps(
        spec.to_json(), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


# Existing dev standalone interfaces remain available for offline callers.
# DaemonApplication composes RestorePolicyAuthority and typed PAC queries; it
# does not instantiate or call these compatibility readers.
class RestorePolicyStore:
    """Atomic policy document; missing is the accepted 12-hour default."""

    def __init__(self, path: Path, *, normalize: Callable[[str], str]) -> None:
        self.path = Path(path)
        self._normalize = normalize
        self._lock = threading.RLock()

    def load(self) -> RestorePolicy:
        with self._lock:
            if not self.path.exists():
                return RestorePolicy()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RestorePolicyError(
                    f"cannot read agent restore policy {self.path}: {error}"
                ) from error
            if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
                raise RestorePolicyError("agent restore policy must use schemaVersion 1")
            threshold = raw.get("restoreThresholdMs")
            if (
                not isinstance(threshold, int)
                or isinstance(threshold, bool)
                or threshold < 0
            ):
                raise RestorePolicyError(
                    "agent restore policy restoreThresholdMs must be a non-negative integer"
                )
            agents = raw.get("agents", {})
            if not isinstance(agents, dict):
                raise RestorePolicyError("agent restore policy agents must be an object")
            normalized: dict[str, str] = {}
            for actor, policy in agents.items():
                if not isinstance(actor, str) or policy not in RESTORE_POLICIES:
                    raise RestorePolicyError(
                        "agent restore policy entries must map names to active, always, or never"
                    )
                try:
                    name = self._normalize(actor)
                except (ValueError, RuntimeError) as error:
                    raise RestorePolicyError(
                        f"invalid agent restore policy entry {actor!r}: {error}"
                    ) from error
                normalized[name] = str(policy)
            return RestorePolicy(threshold, tuple(sorted(normalized.items())))

    def set_agent(self, actor: str, policy: str) -> RestorePolicy:
        if policy not in RESTORE_POLICIES:
            raise RestorePolicyError("policy must be active, always, or never")
        name = self._normalize(actor)
        with self._lock:
            current = self.load()
            agents = dict(current.agents)
            if policy == "active":
                agents.pop(name, None)
            else:
                agents[name] = policy
            updated = RestorePolicy(current.threshold_ms, tuple(sorted(agents.items())))
            atomic_json_write(self.path, updated.to_json())
            return updated

    def set_threshold(self, threshold_ms: int) -> RestorePolicy:
        if threshold_ms < 0:
            raise RestorePolicyError("restore threshold must not be negative")
        with self._lock:
            current = self.load()
            updated = RestorePolicy(threshold_ms, current.agents)
            atomic_json_write(self.path, updated.to_json())
            return updated


@dataclass(frozen=True, slots=True)
class PacRestoreFacts:
    terminal: bool = False
    requested: bool = False
    remote_return: bool = False

    @property
    def pending_work(self) -> bool:
        return self.requested or self.remote_return


def pac_restore_facts(database: Path, actor: str) -> PacRestoreFacts:
    """Read PAC-owned restore facts without mutating workflow state."""

    path = Path(database)
    if not path.exists():
        return PacRestoreFacts()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT n.graph_id,n.node_id,w.state "
            "FROM nodes n JOIN workflow_graphs w ON w.graph_id=n.graph_id "
            "WHERE n.kind='actor' AND n.actor_name=?",
            (actor,),
        ).fetchall()
        if not rows:
            return PacRestoreFacts()
        terminal = all(
            str(row["state"]) in {"completed", "failed", "cancelled"}
            for row in rows
        )
        requested = any(
            connection.execute(
                "SELECT 1 FROM workflow_nodes WHERE graph_id=? AND actor_node=? "
                "AND state='requested' LIMIT 1",
                (row["graph_id"], row["node_id"]),
            ).fetchone()
            is not None
            for row in rows
        )
        remote_return = any(
            connection.execute(
                "SELECT 1 FROM remote_workflow_outbox o "
                "JOIN remote_workflow_requests r ON r.request_id=o.request_id "
                "JOIN workflow_nodes wn ON wn.graph_id=r.graph_id AND wn.node_id=r.node_id "
                "WHERE wn.graph_id=? AND wn.actor_node=? LIMIT 1",
                (row["graph_id"], row["node_id"]),
            ).fetchone()
            is not None
            for row in rows
        )
        return PacRestoreFacts(
            terminal=terminal, requested=requested, remote_return=remote_return
        )
    finally:
        connection.close()



__all__ = [
    "RestorePolicyStore",
    "PacRestoreFacts",
    "pac_restore_facts",
    "RESTORE_POLICIES",
    "RESTORE_THRESHOLD_MS",
    "RestorePolicy",
    "BlockingFailureAuthority",
    "RestorePolicyAuthority",
    "RestorePolicyError",
    "RestorePolicyProjection",
    "desired_generation",
]
