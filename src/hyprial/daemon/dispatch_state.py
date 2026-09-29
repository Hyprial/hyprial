"""Pure mailbox owner for DispatchRuntime attempts, results and holds."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar, cast

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult

_Value = TypeVar("_Value")


class DispatchTable(StrEnum):
    INFLIGHT = "inflight"
    ATTEMPT_GENERATION = "attempt_generation"
    QUEUED_HOLDS = "queued_holds"
    PENDING_WORKFLOW_RESULTS = "pending_workflow_results"
    PENDING_WORKFLOW_ATTEMPTS = "pending_workflow_attempts"
    PENDING_REPLY_RESULTS = "pending_reply_results"
    PENDING_FORWARD_OUTCOMES = "pending_forward_outcomes"
    PENDING_NOTICES = "pending_notices"
    NOTICE_FAILURES_LOGGED = "notice_failures_logged"
    PRUNED_ITEMS = "pruned_items"


class DispatchStateOperation(StrEnum):
    GET = "get"
    PUT = "put"
    POP = "pop"
    SETDEFAULT = "setdefault"
    CONTAINS = "contains"
    VALUES = "values"
    ITEMS = "items"
    CLEAR = "clear"
    LENGTH = "length"
    SNAPSHOT = "snapshot"
    APPEND_TUPLE = "append_tuple"
    COMPARE_PUT = "compare_put"
    COMPARE_POP = "compare_pop"


@dataclass(frozen=True, slots=True)
class DispatchStateCommand:
    operation_id: str
    table: DispatchTable
    operation: DispatchStateOperation
    key: str | None = None
    value: object | None = None
    expected: object | None = None


@dataclass(frozen=True, slots=True)
class DispatchStateProjection:
    version: int
    table_sizes: tuple[tuple[str, int], ...]


@dataclass(slots=True)
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: object | None = None
    error: BaseException | None = None


class DispatchStateAuthority:
    """Only pure, bounded in-memory transitions run in this actor mailbox."""

    def __init__(self, *, capacity: int = 256, runtime: ActorRuntime | None = None):
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._pending: dict[str, _Reply] = {}
        self._tables: dict[DispatchTable, dict[str, object]] = {
            table: {} for table in DispatchTable
        }
        self._version = 0
        self._closed = False
        self._capacity = capacity
        self._handle = self._runtime.start(
            ActorSpec(
                name="dispatch-state",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )

    def table(self, table: DispatchTable) -> DispatchOwnedMap[object]:
        return DispatchOwnedMap(self, table)

    def call(
        self,
        table: DispatchTable,
        operation: DispatchStateOperation,
        key: str | None = None,
        value: object | None = None,
        *,
        expected: object | None = None,
        timeout: float = 5.0,
    ) -> object | None:
        command = DispatchStateCommand(
            uuid.uuid4().hex, table, operation, key, value, expected
        )
        reply = _Reply()
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                raise TimeoutError("dispatch state authority closed or overloaded")
            self._pending[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                raise TimeoutError(f"dispatch state admission {admission.value}")
        if not reply.ready.wait(max(0.0, timeout)):
            raise TimeoutError(
                f"dispatch state operation {command.operation_id} remains accepted"
            )
        if reply.error is not None:
            raise reply.error
        return reply.result

    def _receive(self, command: object) -> None:
        if not isinstance(command, DispatchStateCommand):
            raise TypeError("unsupported dispatch state command")
        table = self._tables[command.table]
        key = command.key
        try:
            if command.operation is DispatchStateOperation.GET:
                result = table.get(key)
            elif command.operation is DispatchStateOperation.CONTAINS:
                result = key in table
            elif command.operation is DispatchStateOperation.VALUES:
                result = tuple(table.values())
            elif command.operation is DispatchStateOperation.ITEMS:
                result = tuple(table.items())
            elif command.operation is DispatchStateOperation.LENGTH:
                result = len(table)
            elif command.operation is DispatchStateOperation.SNAPSHOT:
                result = DispatchStateProjection(
                    self._version,
                    tuple(
                        (name.value, len(rows))
                        for name, rows in self._tables.items()
                    ),
                )
            elif command.operation is DispatchStateOperation.CLEAR:
                table.clear()
                self._version += 1
                result = None
            elif key is None:
                raise ValueError("dispatch state mutation requires a key")
            elif command.operation is DispatchStateOperation.PUT:
                table[key] = command.value
                self._version += 1
                result = None
            elif command.operation is DispatchStateOperation.POP:
                result = table.pop(key, None)
                self._version += 1
            elif command.operation is DispatchStateOperation.SETDEFAULT:
                result = table.setdefault(key, command.value)
                self._version += 1
            elif command.operation is DispatchStateOperation.APPEND_TUPLE:
                table[key] = tuple(table.get(key, ())) + tuple(command.value or ())
                self._version += 1
                result = None
            elif command.operation is DispatchStateOperation.COMPARE_PUT:
                result = table.get(key) == command.expected
                if result:
                    table[key] = command.value
                    self._version += 1
            elif command.operation is DispatchStateOperation.COMPARE_POP:
                result = table.get(key) == command.expected
                if result:
                    table.pop(key, None)
                    self._version += 1
            else:
                raise TypeError("unsupported dispatch state operation")
            error = None
        except BaseException as caught:
            result = None
            error = caught
        with self._guard:
            reply = self._pending.pop(command.operation_id, None)
            if reply is not None:
                reply.result = result
                reply.error = error
                reply.ready.set()

    def projection(self) -> DispatchStateProjection:
        return cast(DispatchStateProjection, self.call(
            DispatchTable.INFLIGHT, DispatchStateOperation.SNAPSHOT
        ))

    def close(self, timeout: float = 5.0) -> bool:
        with self._guard:
            self._closed = True
        return self._runtime.stop(self._handle, timeout)

    def append_pruned(self, items: tuple[object, ...]) -> None:
        self.call(
            DispatchTable.PRUNED_ITEMS,
            DispatchStateOperation.APPEND_TUPLE,
            "items",
            items,
        )

    def take_pruned(self) -> tuple[object, ...]:
        return cast(tuple[object, ...], self.call(
            DispatchTable.PRUNED_ITEMS,
            DispatchStateOperation.POP,
            "items",
        ) or ())

    def settle_hold(
        self, message_id: str, *, expected: object | None,
        replacement: object | None,
    ) -> bool:
        operation = (
            DispatchStateOperation.COMPARE_POP
            if replacement is None else DispatchStateOperation.COMPARE_PUT
        )
        return bool(self.call(
            DispatchTable.QUEUED_HOLDS, operation,
            message_id, replacement, expected=expected,
        ))


class DispatchOwnedMap(Generic[_Value]):
    """Domain table port; values are frozen and mutations are mailbox commands."""

    def __init__(self, authority: DispatchStateAuthority, table: DispatchTable):
        self._authority = authority
        self._table = table

    def get(self, key: str, default: _Value | None = None) -> _Value | None:
        result = self._authority.call(self._table, DispatchStateOperation.GET, key)
        return default if result is None else cast(_Value, result)

    def __contains__(self, key: str) -> bool:
        return bool(self._authority.call(
            self._table, DispatchStateOperation.CONTAINS, key
        ))

    def __getitem__(self, key: str) -> _Value:
        result = self.get(key)
        if result is None:
            raise KeyError(key)
        return result

    def __setitem__(self, key: str, value: _Value) -> None:
        self._authority.call(self._table, DispatchStateOperation.PUT, key, value)

    def pop(self, key: str, default: _Value | None = None) -> _Value | None:
        result = self._authority.call(self._table, DispatchStateOperation.POP, key)
        return default if result is None else cast(_Value, result)

    def setdefault(self, key: str, value: _Value) -> _Value:
        return cast(_Value, self._authority.call(
            self._table, DispatchStateOperation.SETDEFAULT, key, value
        ))

    def values(self) -> tuple[_Value, ...]:
        return cast(tuple[_Value, ...], self._authority.call(
            self._table, DispatchStateOperation.VALUES
        ))

    def items(self) -> tuple[tuple[str, _Value], ...]:
        return cast(tuple[tuple[str, _Value], ...], self._authority.call(
            self._table, DispatchStateOperation.ITEMS
        ))

    def clear(self) -> None:
        self._authority.call(self._table, DispatchStateOperation.CLEAR)

    def __len__(self) -> int:
        return int(self._authority.call(
            self._table, DispatchStateOperation.LENGTH
        ) or 0)

    def __bool__(self) -> bool:
        return len(self) > 0


class DispatchOwnedSet:
    def __init__(self, authority: DispatchStateAuthority, table: DispatchTable):
        self._map: DispatchOwnedMap[bool] = DispatchOwnedMap(authority, table)

    def __contains__(self, key: str) -> bool:
        return key in self._map

    def add(self, key: str) -> None:
        self._map[key] = True

    def discard(self, key: str) -> None:
        self._map.pop(key)
