"""Per-invocation resolution of the CLI's monkeypatchable service bindings.

``hyprial.cli`` was a single module, and the test suite (plus operators'
tooling) rebinds names on it - ``monkeypatch.setattr(cli, "_daemon_request",
fake)`` and friends.  After the command-family extraction those names live in
their owning modules. The native Shell composition owner,
``hyprial.shell.impl.cli.runtime.entry``, is now the binding authority and
builds one :class:`CliServices` per lookup from its current
module globals, so a patched attribute is honored by every extracted call
site exactly as before.  No ``__getattr__``, no eval, no hidden state: the
resolver is registered once by the native entry at composition time via
:func:`set_service_resolver`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CliServices:
    """Current bindings of every externally rebindable CLI name."""

    CliError: type[Exception]
    time: Any
    print: Callable[..., None]
    uuid4: Callable[..., Any]
    _complete_initialization: Callable[..., Any]
    _create_agent_for_start: Callable[..., Any]
    _daemon_probe: Callable[..., Any]
    _daemon_ready_for_process: Callable[..., Any]
    _daemon_request: Callable[..., Any]
    _describe_survivor: Callable[..., Any]
    _dsh_host_describe: Callable[..., Any]
    _execute: Callable[..., Any]
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
    _start_interactive_pi: Callable[..., Any]
    _state_dir: Callable[..., Any]
    _stdin_isatty: Callable[..., Any]
    _stop_daemon_for_identity_switch: Callable[..., Any]
    _stop_daemon_gracefully: Callable[..., Any]
    _stopped_process: Callable[..., Any]
    _stored_user_proxy_route: Callable[..., Any]
    _squire_setup_operation: Callable[..., Any]
    _timer_config: Callable[..., Any]
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


_resolver: Callable[[], CliServices] | None = None


def set_service_resolver(resolver: Callable[[], CliServices]) -> None:
    """Register the composition root's binding reader (called once by cli)."""
    global _resolver
    _resolver = resolver


def get_services() -> CliServices:
    """Return the bindings live on the native Shell entry *right now*."""
    if _resolver is None:
        raise RuntimeError(
            "hyprial.shell.impl.cli.commands.common.services: no resolver registered; "
            "import hyprial.shell.impl.cli.runtime.entry before invoking commands"
        )
    return _resolver()
