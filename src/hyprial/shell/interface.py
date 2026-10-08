"""Explicit public bindings for the shell domain."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.shell.impl.cli.commands.workflow.run import _identity as cli_identity
    import hyprial.shell.impl.desktop_network as desktop_network
    from hyprial.shell.impl.gui.runtime import gui_status as gui_status
    from hyprial.shell.impl.cli.runtime.entry import main as main
    from hyprial.shell.impl.gui.apps import perform as perform_gui_action
    from hyprial.shell.impl.gui.product import product_bundle as product_gui_bundle
    from hyprial.shell.impl.gui.apps import resolve_invocation as resolve_gui_invocation
    from hyprial.shell.impl.cli.commands.routine.commands import routine_app as routine_app
    from hyprial.shell.impl.gui.runtime import start_gui_background as start_gui_background
    from hyprial.shell.impl.gui.runtime import stop_gui as stop_gui
    from hyprial.shell.impl.gui.apps import upgrade as upgrade_gui
    from hyprial.shell.impl.cli.commands.workflow.run import workflow_app as workflow_app

_FACADE_EXPORTS = {
    'cli_identity': ('hyprial.shell.impl.cli.commands.workflow.run', '_identity'),
    'desktop_network': ('hyprial.shell.impl.desktop_network', None),
    'gui_status': ('hyprial.shell.impl.gui.runtime', 'gui_status'),
    'main': ('hyprial.shell.impl.cli.runtime.entry', 'main'),
    'perform_gui_action': ('hyprial.shell.impl.gui.apps', 'perform'),
    'product_gui_bundle': ('hyprial.shell.impl.gui.product', 'product_bundle'),
    'resolve_gui_invocation': ('hyprial.shell.impl.gui.apps', 'resolve_invocation'),
    'routine_app': ('hyprial.shell.impl.cli.commands.routine.commands', 'routine_app'),
    'start_gui_background': ('hyprial.shell.impl.gui.runtime', 'start_gui_background'),
    'stop_gui': ('hyprial.shell.impl.gui.runtime', 'stop_gui'),
    'upgrade_gui': ('hyprial.shell.impl.gui.apps', 'upgrade'),
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
    'cli_identity',
    'desktop_network',
    'gui_status',
    'main',
    'perform_gui_action',
    'product_gui_bundle',
    'resolve_gui_invocation',
    'routine_app',
    'start_gui_background',
    'stop_gui',
    'upgrade_gui',
    'workflow_app',
]
