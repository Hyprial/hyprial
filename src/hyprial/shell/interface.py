"""Explicit public bindings for the shell domain."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.shell.impl.gui.adapter.operations.client import daemon_request as daemon_request
    from hyprial.shell.impl.gui.adapter.operations.client import daemon_request_once as daemon_request_once
    from hyprial.shell.impl.cli.commands.workflow.run import _identity as cli_identity
    from hyprial.shell.impl.cli.runtime.entry import main as main
    from hyprial.shell.impl.cli.commands.routine.commands import routine_app as routine_app
    from hyprial.shell.impl.upgrade_bootstrap import prime_post_install_imports as prime_post_install_imports
    from hyprial.shell.impl.worker_context import worker_session_binding as worker_session_binding
    from hyprial.shell.impl.cli.commands.workflow.run import workflow_app as workflow_app

_FACADE_EXPORTS = {
    'daemon_request': ('hyprial.shell.impl.gui.adapter.operations.client', 'daemon_request'),
    'daemon_request_once': ('hyprial.shell.impl.gui.adapter.operations.client', 'daemon_request_once'),
    'cli_identity': ('hyprial.shell.impl.cli.commands.workflow.run', '_identity'),
    'main': ('hyprial.shell.impl.cli.runtime.entry', 'main'),
    'prime_post_install_imports': ('hyprial.shell.impl.upgrade_bootstrap', 'prime_post_install_imports'),
    'worker_session_binding': ('hyprial.shell.impl.worker_context', 'worker_session_binding'),
    'routine_app': ('hyprial.shell.impl.cli.commands.routine.commands', 'routine_app'),
    'workflow_app': ('hyprial.shell.impl.cli.commands.workflow.run', 'workflow_app'),
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
    'daemon_request',
    'daemon_request_once',
    'cli_identity',
    'main',
    'prime_post_install_imports',
    'worker_session_binding',
    'routine_app',
    'workflow_app',
]
