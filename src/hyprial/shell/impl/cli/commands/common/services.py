"""Explicitly assembled service instance for unmigrated command families.

The composition root (:mod:`hyprial.shell.impl.cli.runtime.services`) imports
the owning helpers and builds one :class:`CliServices`.  Command modules read
that instance through :func:`get_services`; there is no per-invocation
``globals()``/``getattr`` sweep and no entry-module lookup authority.

Tests patch the owning module for request/execute (``support``) or the
explicit ``get_services()`` instance for command-specific helpers.  New B/C
code must use :class:`~hyprial.shell.impl.cli.runtime.composition.CliRuntime`
rather than growing this snapshot.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hyprial.shell.impl.cli.runtime.composition import CliRuntime


@dataclass
class CliServices:
    """Current bindings of every externally rebindable CLI name.

    ``runtime`` owns the two cross-cutting ports.  Commands call
    ``services.runtime.request(...)`` / ``services.runtime.execute(...)``
    rather than carrying independent request/execute snapshot fields, so a
    request/output change has one source of truth during the A/C transition.
    """

    runtime: CliRuntime
    CliError: type[Exception]
    time: Any
    print: Callable[..., None]
    uuid4: Callable[..., Any]
    _complete_initialization: Callable[..., Any]
    _create_agent_for_start: Callable[..., Any]
    _daemon_probe: Callable[..., Any]
    _daemon_ready_for_process: Callable[..., Any]
    _describe_survivor: Callable[..., Any]
    _dsh_host_describe: Callable[..., Any]
    _hyprial_home: Callable[..., Any]
    _initialize_org_context: Callable[..., Any]
    _lark_auth_timer_check: Callable[..., Any]
    _launch_daemon_process: Callable[..., Any]
    _launch_daemon_process_locked: Callable[..., Any]
    _onboarding_apply_actions: Callable[..., Any]
    _onboarding_snapshot: Callable[..., Any]
    _perform_upgrade: Callable[..., Any]
    _perform_upgrade_and_report: Callable[..., Any]
    _prime_post_install_restart_imports: Callable[..., Any]
    _restart_is_interactive: Callable[..., Any]
    _route_operation: Callable[..., Any]
    _route_candidates: Callable[..., Any]
    _routine_identity: Callable[..., Any]
    _run_login_cli_flow: Callable[..., Any]
    _run_worktree_git: Callable[..., Any]
    _running_daemon_before_upgrade: Callable[..., Any]
    _runtime_context_environment: Callable[..., Any]
    _sample_process_table: Callable[..., Any]
    _socket_path: Callable[..., Any]
    _start_interactive_claude: Callable[..., Any]
    _start_interactive_codex: Callable[..., Any]
    _start_interactive_dsh: Callable[..., Any]
    _start_interactive_pi: Callable[..., Any]
    _state_dir: Callable[..., Any]
    _stdin_isatty: Callable[..., Any]
    _stop_daemon_for_identity_switch: Callable[..., Any]
    _stop_daemon_gracefully: Callable[..., Any]
    _stopped_process: Callable[..., Any]
    _stored_user_proxy_route: Callable[..., Any]
    _squire_setup_operation: Callable[..., Any]
    _wait_for_daemon: Callable[..., Any]
    initialize_hyprial_home: Callable[..., Any]
    notify_upgrade_failure: Callable[..., Any]
    process_cpu_seconds: Callable[..., Any]
    record_alert_outcome: Callable[..., Any]
    require_initialized_hyprial_home: Callable[..., Any]
    configured_hyprial_home: Callable[..., Any]
    default_hyprial_home: Callable[..., Any]
    resolve_profile: Callable[..., Any]
    start_gui_background: Callable[..., Any]
    tcp_probe: Callable[..., Any]
    write_failure_marker: Callable[..., Any]


_services: CliServices | None = None


def install_services(services: CliServices) -> None:
    """Install the composition root's explicitly assembled instance once."""

    global _services
    if _services is not None:
        raise RuntimeError("CLI services are already installed")
    _services = services


def get_services() -> CliServices:
    """Return the explicitly assembled instance for unmigrated families."""

    if _services is None:
        raise RuntimeError(
            "hyprial.shell.impl.cli.commands.common.services: CLI services are not "
            "installed; run the composition root or install a test double"
        )
    return _services
