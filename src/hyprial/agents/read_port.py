"""Bounded Agent registry and filesystem read lane.

Security-sensitive grant queries must read the current SQLite incarnation
fence.  Cached Agent projections are useful for status but cannot authorize
grants after an external/offline writer changes the database.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .registry import AgentRegistry
from .grants import CapabilityGrant, GrantJournalEntry
from .home import HomeReceipt, WorkspaceSummary
from .secrets import SecretGrant


@dataclass(frozen=True, slots=True)
class ValidateHome:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class ReadCapabilityGrants:
    correlation_id: str
    actor: str | None


@dataclass(frozen=True, slots=True)
class ReadSecretInventory:
    correlation_id: str
    actor: str | None


@dataclass(frozen=True, slots=True)
class ReadGrantJournal:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class ReadWorkspaceSummary:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class ResolveWorkspacePath:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class ReadAgentActivityHints:
    correlation_id: str
    actor: str


@dataclass(frozen=True, slots=True)
class AgentActivityHints:
    created_at_ms: int
    has_last_session_id: bool
    agent_home_mtime_ms: int | None

    def to_payload(self) -> dict[str, int | bool | None]:
        return {
            "createdAtMs": self.created_at_ms,
            "hasLastSessionId": self.has_last_session_id,
            "agentHomeMtimeMs": self.agent_home_mtime_ms,
        }


ReadCommand = (
    ValidateHome | ReadCapabilityGrants | ReadSecretInventory
    | ReadGrantJournal | ReadWorkspaceSummary | ResolveWorkspacePath
    | ReadAgentActivityHints
)


@dataclass(frozen=True, slots=True)
class ReadCompleted:
    correlation_id: str
    error_code: str | None = None


class AgentReadBusy(RuntimeError):
    pass


class AgentReadTimeout(TimeoutError):
    def __init__(self, correlation_id: str) -> None:
        super().__init__(f"Agent read remains unsettled: {correlation_id}")
        self.correlation_id = correlation_id


class _Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.completion: ReadCompleted | None = None
        self.value: object = None
        self.error: Exception | None = None


class AgentReadEffectPort:
    """Fixed reads only; no caller SQL, callbacks, credentials or FS writes."""

    def __init__(
        self, registry: AgentRegistry, *, capacity: int = 32,
        workers: int = 2, call_timeout: float = 5.0,
    ) -> None:
        if capacity < 1 or workers < 1:
            raise ValueError("Agent read effect capacity and workers must be positive")
        self.registry = registry
        self._queue: queue.Queue[ReadCommand | None] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._closed = False
        self._late_completions = 0
        self._call_timeout = call_timeout
        self._workers = tuple(
            threading.Thread(
                target=self._run, name=f"agent-read-effect-{index}", daemon=True
            )
            for index in range(workers)
        )
        for worker in self._workers:
            worker.start()

    def validate_home(self, actor: str) -> HomeReceipt:
        return self._call(ValidateHome(f"agent-home-{uuid4().hex}", actor))

    def capability_grants(
        self, actor: str | None = None
    ) -> tuple[CapabilityGrant, ...]:
        return self._call(ReadCapabilityGrants(
            f"agent-capabilities-{uuid4().hex}", actor,
        ))

    def secret_inventory(self, actor: str | None = None) -> tuple[SecretGrant, ...]:
        return self._call(ReadSecretInventory(
            f"agent-secrets-{uuid4().hex}", actor,
        ))

    def grant_journal(self, actor: str) -> tuple[GrantJournalEntry, ...]:
        return self._call(ReadGrantJournal(
            f"agent-grant-journal-{uuid4().hex}", actor,
        ))

    def workspace_summary(self, actor: str) -> WorkspaceSummary:
        return self._call(ReadWorkspaceSummary(
            f"agent-workspace-{uuid4().hex}", actor,
        ))

    def workspace_path(self, actor: str) -> str:
        return self._call(ResolveWorkspacePath(
            f"agent-workspace-path-{uuid4().hex}", actor,
        ))

    def activity_hints(self, actor: str) -> AgentActivityHints:
        return self._call(ReadAgentActivityHints(
            f"agent-activity-hints-{uuid4().hex}", actor,
        ))

    def _call(self, command: ReadCommand) -> Any:
        pending = _Pending()
        with self._lock:
            if self._closed or len(self._pending) >= self._queue.maxsize * 2:
                raise AgentReadBusy("Agent read effect capacity is full or closed")
            self._pending[command.correlation_id] = pending
            try:
                self._queue.put_nowait(command)
            except queue.Full as error:
                del self._pending[command.correlation_id]
                raise AgentReadBusy("Agent read effect queue is full") from error
        if not pending.event.wait(self._call_timeout):
            # Timeout ends only this caller's read wait.
            with self._lock:
                self._pending.pop(command.correlation_id, None)
            raise AgentReadTimeout(command.correlation_id)
        with self._lock:
            completion = self._pending.pop(command.correlation_id).completion
        assert completion is not None
        if completion.error_code is not None:
            assert pending.error is not None
            raise pending.error
        return pending.value

    def _run(self) -> None:
        while True:
            command = self._queue.get()
            if command is None:
                return
            captured_error: Exception | None = None
            try:
                if isinstance(command, ValidateHome):
                    value = self.registry.home_receipt(command.actor)
                elif isinstance(command, ReadCapabilityGrants):
                    value = self.registry.capability_grants(command.actor)
                elif isinstance(command, ReadSecretInventory):
                    value = self.registry.secret_inventory(command.actor)
                elif isinstance(command, ReadGrantJournal):
                    value = self.registry.grant_journal(command.actor)
                elif isinstance(command, ReadWorkspaceSummary):
                    value = self.registry.workspace_summary(command.actor)
                elif isinstance(command, ResolveWorkspacePath):
                    value = str(self.registry.workspace_path(command.actor))
                elif isinstance(command, ReadAgentActivityHints):
                    hints = self.registry.activity_hints(command.actor)
                    value = AgentActivityHints(
                        created_at_ms=int(hints["createdAtMs"]),
                        has_last_session_id=bool(hints["hasLastSessionId"]),
                        agent_home_mtime_ms=(
                            None if hints["agentHomeMtimeMs"] is None
                            else int(hints["agentHomeMtimeMs"])
                        ),
                    )
                else:
                    raise TypeError("unsupported Agent read effect")
                completion = ReadCompleted(command.correlation_id)
            except Exception as error:
                captured_error = error
                completion = ReadCompleted(
                    command.correlation_id, error_code=type(error).__name__
                )
            with self._lock:
                pending = self._pending.get(command.correlation_id)
                if pending is not None:
                    if completion.error_code is not None:
                        pending.error = captured_error
                    else:
                        pending.value = value
                    pending.completion = completion
                    pending.event.set()
                else:
                    self._late_completions += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "queued": self._queue.qsize(),
                "pending": len(self._pending),
                "lateCompletions": self._late_completions,
            }

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
        for _ in self._workers:
            try:
                self._queue.put(None, timeout=max(0.0, deadline - time.monotonic()))
            except queue.Full:
                return False
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in self._workers)
