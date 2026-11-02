"""Explicit assembly of the legacy ``CliServices`` instance.

The composition root calls :func:`install_services` once after every command
family has been imported.  The helper functions are imported from their owning
modules here, not re-exported through the process entry and not re-read from
module globals on every lookup.  Request/execute are the sole exceptions:
they stay on :class:`CliRuntime` and are read from the explicit owners in
``commands.common.support`` by the runtime resolvers.

New B/C code must use :class:`CliRuntime`; this module exists only to keep the
unmigrated command families working while C/DR-30 retires them by group.
"""
from __future__ import annotations

from pathlib import Path
import time as _time
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.shell.impl.cli.commands.common.services import (
    CliServices,
    install_services as _install_services,
)

if TYPE_CHECKING:
    from hyprial.shell.impl.cli.runtime.composition import CliRuntime


def initialize_hyprial_home() -> Path:
    """Shell composition of the kernel-owned home initializer.

    Kernel owns selection/mkdir semantics; the dispatch-policy callback is
    daemon-owned and injected here so kernel never imports daemon
    (kernel signature decision, PY-BATCH-2026-10-01 integration).
    """

    from hyprial.daemon import ensure_dispatch_policy
    from hyprial.kernel import initialize_hyprial_home as _kernel_initialize

    return _kernel_initialize(
        ensure_dispatch_policy=ensure_dispatch_policy,
    )


# Late-bound daemon/gui capabilities: importing the composition root must not
# import hyprial.daemon or the GUI runtime until a command actually uses them.
def notify_upgrade_failure(*args, **kwargs):
    from hyprial.daemon import notify_upgrade_failure as _real

    return _real(*args, **kwargs)


def process_cpu_seconds(*args, **kwargs):
    from hyprial.daemon import process_cpu_seconds as _real

    return _real(*args, **kwargs)


def record_alert_outcome(*args, **kwargs):
    from hyprial.daemon import record_alert_outcome as _real

    return _real(*args, **kwargs)


def resolve_profile(*args, **kwargs):
    from hyprial.daemon import resolve_profile as _real

    return _real(*args, **kwargs)


def tcp_probe(*args, **kwargs):
    from hyprial.daemon import tcp_probe as _real

    return _real(*args, **kwargs)


def write_failure_marker(*args, **kwargs):
    from hyprial.daemon import write_failure_marker as _real

    return _real(*args, **kwargs)


def start_gui_background(*args, **kwargs):
    from hyprial.shell.impl.gui.runtime import start_gui_background as _real

    return _real(*args, **kwargs)


def assemble_services(runtime: CliRuntime) -> CliServices:
    """Import owning helpers and build the one explicit service instance."""

    from hyprial.kernel import (
        configured_hyprial_home,
        default_hyprial_home,
        require_initialized_hyprial_home,
    )
    from hyprial.shell.impl.cli.commands.adapter.lark_auth import _lark_auth_timer_check
    from hyprial.shell.impl.cli.commands.adapter.routes import (
        _route_candidates,
        _route_operation,
    )
    from hyprial.shell.impl.cli.commands.agent.create import _create_agent_for_start
    from hyprial.shell.impl.cli.commands.agent.runtime.claude import (
        _start_interactive_claude,
    )
    from hyprial.shell.impl.cli.commands.agent.runtime.dsh import (
        _start_interactive_dsh,
    )
    from hyprial.shell.impl.cli.commands.agent.runtime.interactive import (
        _runtime_context_environment,
    )
    from hyprial.shell.impl.cli.commands.agent.runtime.sessions import (
        _start_interactive_codex,
        _start_interactive_pi,
    )
    from hyprial.shell.impl.cli.commands.common.daemon_start import (
        _launch_daemon_process,
        _launch_daemon_process_locked,
    )
    from hyprial.shell.impl.cli.commands.common.daemon_stop import (
        _describe_survivor,
        _stop_daemon_for_identity_switch,
        _stop_daemon_gracefully,
        _stopped_process,
    )
    from hyprial.shell.impl.cli.commands.common.support import (
        CliError,
        _daemon_probe,
        _daemon_ready_for_process,
        _hyprial_home,
        _socket_path,
        _state_dir,
        _stdin_isatty,
        _wait_for_daemon,
    )
    from hyprial.shell.impl.cli.commands.daemon.restart import _restart_is_interactive
    from hyprial.shell.impl.cli.commands.daemon.top import _sample_process_table
    from hyprial.shell.impl.cli.commands.doctor.commands import _dsh_host_describe
    from hyprial.shell.impl.cli.commands.init.init import (
        _complete_initialization,
        _initialize_org_context,
    )
    from hyprial.shell.impl.cli.commands.init.onboarding import (
        _onboarding_apply_actions,
        _onboarding_snapshot,
    )
    from hyprial.shell.impl.cli.commands.login.flow import _run_login_cli_flow
    from hyprial.shell.impl.cli.commands.routine.commands import _routine_identity
    from hyprial.shell.impl.cli.commands.socialware.commands import (
        _prime_post_install_restart_imports,
        _running_daemon_before_upgrade,
    )
    from hyprial.shell.impl.cli.commands.squire.squire import _squire_setup_operation
    from hyprial.shell.impl.cli.commands.squire.user_proxy import _stored_user_proxy_route
    from hyprial.shell.impl.cli.commands.upgrade.commands import (
        _perform_upgrade,
        _perform_upgrade_and_report,
    )
    from hyprial.shell.impl.cli.commands.workflow.internal.worktrees import (
        _run_worktree_git,
    )

    return CliServices(
        runtime=runtime,
        CliError=CliError,
        time=_time,
        print=print,
        uuid4=uuid4,
        _complete_initialization=_complete_initialization,
        _create_agent_for_start=_create_agent_for_start,
        _daemon_probe=_daemon_probe,
        _daemon_ready_for_process=_daemon_ready_for_process,
        _describe_survivor=_describe_survivor,
        _dsh_host_describe=_dsh_host_describe,
        _hyprial_home=_hyprial_home,
        _initialize_org_context=_initialize_org_context,
        _lark_auth_timer_check=_lark_auth_timer_check,
        _launch_daemon_process=_launch_daemon_process,
        _launch_daemon_process_locked=_launch_daemon_process_locked,
        _onboarding_apply_actions=_onboarding_apply_actions,
        _onboarding_snapshot=_onboarding_snapshot,
        _perform_upgrade=_perform_upgrade,
        _perform_upgrade_and_report=_perform_upgrade_and_report,
        _prime_post_install_restart_imports=_prime_post_install_restart_imports,
        _restart_is_interactive=_restart_is_interactive,
        _route_operation=_route_operation,
        _route_candidates=_route_candidates,
        _routine_identity=_routine_identity,
        _run_login_cli_flow=_run_login_cli_flow,
        _run_worktree_git=_run_worktree_git,
        _running_daemon_before_upgrade=_running_daemon_before_upgrade,
        _runtime_context_environment=_runtime_context_environment,
        _sample_process_table=_sample_process_table,
        _socket_path=_socket_path,
        _start_interactive_claude=_start_interactive_claude,
        _start_interactive_codex=_start_interactive_codex,
        _start_interactive_dsh=_start_interactive_dsh,
        _start_interactive_pi=_start_interactive_pi,
        _state_dir=_state_dir,
        _stdin_isatty=_stdin_isatty,
        _stop_daemon_for_identity_switch=_stop_daemon_for_identity_switch,
        _stop_daemon_gracefully=_stop_daemon_gracefully,
        _stopped_process=_stopped_process,
        _stored_user_proxy_route=_stored_user_proxy_route,
        _squire_setup_operation=_squire_setup_operation,
        _wait_for_daemon=_wait_for_daemon,
        configured_hyprial_home=configured_hyprial_home,
        default_hyprial_home=default_hyprial_home,
        initialize_hyprial_home=initialize_hyprial_home,
        notify_upgrade_failure=notify_upgrade_failure,
        process_cpu_seconds=process_cpu_seconds,
        record_alert_outcome=record_alert_outcome,
        require_initialized_hyprial_home=require_initialized_hyprial_home,
        resolve_profile=resolve_profile,
        start_gui_background=start_gui_background,
        tcp_probe=tcp_probe,
        write_failure_marker=write_failure_marker,
    )


def install_services(runtime: CliRuntime) -> None:
    """Build and install the single legacy service instance."""

    _install_services(assemble_services(runtime))
