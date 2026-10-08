"""Actor-owned routine registry and durable effect coordinator.

Only :class:`RoutineRegistry` mutates routine state. Taskwarrior, PAC and
alarm calls are represented as outbox effects and run outside the actor.

The implementation is split by responsibility: ``_effects`` holds the effect
records and output union, ``_registry_core`` the command/timer lane and
``_registry_events`` the completion/cycle bookkeeping.  Single state owner is
still ``RoutineRegistry``.
"""

from __future__ import annotations

from hyprial.biz.impl.routine.execution.registry.effects import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    RegistryOutput,
    RoutineAlarmEffect,
    RoutineEffect,
    RoutinePacEffect,
    RoutineSourceQueryEffect,
)
from hyprial.biz.impl.routine.execution.registry.core import _RegistryCore
from hyprial.biz.impl.routine.execution.registry.events import _RegistryEvents

__all__ = [
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "RegistryOutput",
    "RoutineAlarmEffect",
    "RoutineEffect",
    "RoutineRegistry",
    "RoutineSourceQueryEffect",
    "RoutinePacEffect",
]


class RoutineRegistry(_RegistryCore, _RegistryEvents):
    """Actor-owned writer for routine state; see the module docstring."""
