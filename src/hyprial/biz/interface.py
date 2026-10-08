"""Explicit public bindings for the biz domain."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.biz.impl.routine.execution.coordinator.generation import AgentCreationCompensation as AgentCreationCompensation
    from hyprial.biz.impl.routine.templates import BUILTIN_TEMPLATES as BUILTIN_TEMPLATES
    from hyprial.biz.impl.routine.execution.pac_dispatch import PacRoutineDispatch as PacRoutineDispatch
    from hyprial.biz.impl.quota_watchdog import QuotaWatchdog as QuotaWatchdog
    from hyprial.biz.impl.routine.execution.coordinator import RoutineCoordinator as RoutineCoordinator
    from hyprial.biz.impl.routine.execution.coordinator.generation import RoutineCoordinatorError as RoutineCoordinatorError
    from hyprial.biz.impl.routine.execution.coordinator.generation import RoutineCoordinatorTimeout as RoutineCoordinatorTimeout
    from hyprial.biz.impl.routine.contracts.schema import RoutineSchemaError as RoutineSchemaError
    from hyprial.biz.impl.routine.execution.service import RoutineService as RoutineService
    from hyprial.biz.impl.routine.execution.service import RoutineServiceError as RoutineServiceError
    from hyprial.biz.impl.routine.contracts.schema import RoutineSpec as RoutineSpec
    from hyprial.biz.impl.routine.storage.store import RoutineStore as RoutineStore
    from hyprial.biz.impl.usage_actor import UsageAuthority as UsageAuthority
    from hyprial.biz.impl.workflow.identity import actor_owner as actor_owner
    from hyprial.biz.impl.routine.execution.coordinator import add_command as add_command
    from hyprial.biz.impl.workflow.identity import check_actor_claim as check_actor_claim
    from hyprial.biz.impl.routine.contracts.schema import load_routine as load_routine
    from hyprial.biz.impl.routine.contracts.schema import load_routine_text as load_routine_text
    from hyprial.biz.impl.routine.execution.coordinator import remove_command as remove_command
    from hyprial.biz.impl.routine.templates import render_template as render_template
    from hyprial.biz.impl.turn_pulse import signal_turn_ended as signal_turn_ended
    from hyprial.biz.impl.workflow.identity import state_dir as state_dir
    from hyprial.biz.impl.routine.templates import template_text as template_text
    from hyprial.biz.impl.usage import usage_collection_disabled as usage_collection_disabled
    from hyprial.biz.impl.workflow.identity import worker_binding as worker_binding

_FACADE_EXPORTS = {
    'AgentCreationCompensation': ('hyprial.biz.impl.routine.execution.coordinator.generation', 'AgentCreationCompensation'),
    'BUILTIN_TEMPLATES': ('hyprial.biz.impl.routine.templates', 'BUILTIN_TEMPLATES'),
    'PacRoutineDispatch': ('hyprial.biz.impl.routine.execution.pac_dispatch', 'PacRoutineDispatch'),
    'QuotaWatchdog': ('hyprial.biz.impl.quota_watchdog', 'QuotaWatchdog'),
    'RoutineCoordinator': ('hyprial.biz.impl.routine.execution.coordinator', 'RoutineCoordinator'),
    'RoutineCoordinatorError': ('hyprial.biz.impl.routine.execution.coordinator.generation', 'RoutineCoordinatorError'),
    'RoutineCoordinatorTimeout': ('hyprial.biz.impl.routine.execution.coordinator.generation', 'RoutineCoordinatorTimeout'),
    'RoutineSchemaError': ('hyprial.biz.impl.routine.contracts.schema', 'RoutineSchemaError'),
    'RoutineService': ('hyprial.biz.impl.routine.execution.service', 'RoutineService'),
    'RoutineServiceError': ('hyprial.biz.impl.routine.execution.service', 'RoutineServiceError'),
    'RoutineSpec': ('hyprial.biz.impl.routine.contracts.schema', 'RoutineSpec'),
    'RoutineStore': ('hyprial.biz.impl.routine.storage.store', 'RoutineStore'),
    'UsageAuthority': ('hyprial.biz.impl.usage_actor', 'UsageAuthority'),
    'actor_owner': ('hyprial.biz.impl.workflow.identity', 'actor_owner'),
    'add_command': ('hyprial.biz.impl.routine.execution.coordinator', 'add_command'),
    'check_actor_claim': ('hyprial.biz.impl.workflow.identity', 'check_actor_claim'),
    'load_routine': ('hyprial.biz.impl.routine.contracts.schema', 'load_routine'),
    'load_routine_text': ('hyprial.biz.impl.routine.contracts.schema', 'load_routine_text'),
    'remove_command': ('hyprial.biz.impl.routine.execution.coordinator', 'remove_command'),
    'render_template': ('hyprial.biz.impl.routine.templates', 'render_template'),
    'signal_turn_ended': ('hyprial.biz.impl.turn_pulse', 'signal_turn_ended'),
    'state_dir': ('hyprial.biz.impl.workflow.identity', 'state_dir'),
    'template_text': ('hyprial.biz.impl.routine.templates', 'template_text'),
    'usage_collection_disabled': ('hyprial.biz.impl.usage', 'usage_collection_disabled'),
    'worker_binding': ('hyprial.biz.impl.workflow.identity', 'worker_binding'),
}


def __getattr__(name: str):
    try:
        module_path, symbol_name = _FACADE_EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    from importlib import import_module

    resolved_module = import_module(module_path)
    export = resolved_module if symbol_name is None else getattr(resolved_module, symbol_name)
    globals()[name] = export
    return export


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    'AgentCreationCompensation',
    'BUILTIN_TEMPLATES',
    'PacRoutineDispatch',
    'QuotaWatchdog',
    'RoutineCoordinator',
    'RoutineCoordinatorError',
    'RoutineCoordinatorTimeout',
    'RoutineSchemaError',
    'RoutineService',
    'RoutineServiceError',
    'RoutineSpec',
    'RoutineStore',
    'UsageAuthority',
    'actor_owner',
    'add_command',
    'check_actor_claim',
    'load_routine',
    'load_routine_text',
    'remove_command',
    'render_template',
    'signal_turn_ended',
    'state_dir',
    'template_text',
    'usage_collection_disabled',
    'worker_binding',
]
