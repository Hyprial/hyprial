"""Native Shell command composition for Harness Bridge.

The CLI owns argument parsing and presentation only.  Daemon-backed commands
cross the versioned IPC boundary; they do not import daemon implementation
details.  Command families live under ``hyprial.shell.impl.cli.commands``;
this module owns composition: it imports the families in the
original registration order, re-exports their names (tests, installed-app
hooks and operator tooling bind here), and resolves monkeypatchable
bindings per invocation through :class:`CliServices`. The installed
``hyprial.cli`` module delegates only to the public Shell ``main``.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

# ``shutil``, ``socket`` and ``subprocess`` stay importable here on purpose:
# the test suite patches process/socket boundaries as ``cli.subprocess`` and
# friends (attribute patches on the shared stdlib module objects).

# Typer's Rich help renderer treats CI as a terminal and its bundled
# ``NO_COLOR`` handling only removes color styles, not all ANSI styling.  Set
# Typer's own escape hatch before importing Typer so help and errors honor the
# standard no-color contract even in CI capture.
if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
    os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

import typer

from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.output import report_error

try:  # Typer 0.27+ vendors Click; older Typer exposes the external class.
    from typer._click.exceptions import ClickException as TyperClickException
except ImportError:  # pragma: no cover - compatibility with older Typer
    from click import ClickException as TyperClickException

try:  # Abort moved out of Typer's vendored exceptions in 0.27.2.
    from typer._click.exceptions import Abort as TyperAbort
except ImportError:  # pragma: no cover - compatibility across Typer layouts
    from click import Abort as TyperAbort


# --- command families (import order == original registration order) --------
from hyprial.shell.impl.cli.commands.common.services import CliServices, set_service_resolver
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import CliError, DAEMON_START_HARD_CAP_SECONDS, DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _CONNECT_BACKLOG_ERRNOS, _CONNECT_BACKLOG_RETRY_INTERVAL, _DAEMON_IPC_ROUNDTRIP_SECONDS, _DEVICE_SIGN_IN_HINT, _INIT_READY_TIMEOUT_ENV, _PEER_GONE_ERRNOS, _RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS, _RESTORE_WAIT_DEFAULT_SECONDS, _RESTORE_WAIT_MAX_SECONDS, _RESTORE_WAIT_POLL_SECONDS, _SQUIRE_SETUP_PARTS, _agent_workspace, _announce_setup_guidance, _append_warning, _connect_daemon_socket, _daemon_probe, _daemon_ready_for_process, _daemon_request, _daemon_request_once, _emit, _endpoint_args, _execute, _fail, _hyprial_home, _json_failure, _local_operator_identity, _message_claim_params, _overview_age, _overview_duration, _overview_last_segment, _overview_next, _overview_sender, _overview_table, _peer_gone, _probe_reports_running, _resolved_agent_cwd, _restore_wait_budget, _socket_path, _squire_setup_warning, _state_dir, _stdin_isatty, _user_proxy_launch_params, _user_proxy_setup_warning, _utc_now, _wait_for_daemon
from hyprial.shell.impl.cli.commands.common.daemon_start import _CUSTODY_STARTUP_ERROR_SHAPES, _DAEMON_LOG_READ_BYTES, _SAFE_DAEMON_STARTUP_EVENTS, _daemon_launch_capture, _daemon_launch_log_summary, _daemon_startup_failure_evidence, _daemon_startup_phase_summary, _launch_daemon_process, _launch_daemon_process_locked, _launch_process_error
from hyprial.shell.impl.cli.commands.common.daemon_stop import _describe_survivor, _observe_restart_process, _stop_daemon_for_identity_switch, _stop_daemon_for_operator, _stop_daemon_gracefully, _stop_daemon_with_proof, _stopped_process, _wait_daemon_teardown_receipt, old_pid_from
from hyprial.shell.impl.cli.commands.login.commands import _login_route_is_first_time_setup, login
from hyprial.shell.impl.cli.commands.meta.commands import _semantic_version, _version_result, help_command, version
from hyprial.shell.impl.cli.commands.socialware.commands import _prime_post_install_restart_imports, _require_installed_commit, _running_daemon_before_upgrade, install
from hyprial.shell.impl.cli.commands.common.pending_restart import _install_locked_requirement
from hyprial.shell.impl.cli.commands.daemon.status import _TOP_SEVERITY, _format_quota_window, _format_top_age, _format_top_reset, _format_top_rss, _format_top_seconds, _format_top_window_seconds, _parse_ps_duration, _render_top, _render_top_quota, _sample_process_table, _subtree_totals, _top_state_label, process_status, top_status
from hyprial.shell.impl.cli.commands.doctor.commands import _device_key_doctor_check, _doctor_result, _dsh_doctor_check, _dsh_endpoint_label, _dsh_host_describe, _duplicate_instance_doctor_check, _historical_inbox_doctor_check, _lark_inbound_doctor_check, _maintenance_doctor_check, _mcp_channel_doctor_check, _pac_gc_doctor_check, _restore_doctor_check, _routine_health_doctor_check, _tailcat_sidecar_doctor_check, _workflow_worker_cleanup_doctor_check, _zenoh_doctor_check, doctor
from hyprial.shell.impl.cli.commands.workflow.internal.worktrees import _WORKTREE_GIT_TIMEOUT_SECONDS, _WORKTREE_STATES, _dirty_activity_at_ms, _dirty_paths, _display_worktree_path, _git_count, _parse_worktree_porcelain, _render_worktree_repository, _resolve_worktree_repository, _run_worktree_git, _scan_worktree, _scan_worktree_repository, _worktree_bases, _worktree_git_output, _worktree_ref_exists, _worktree_remove_command, worktrees_command
from hyprial.shell.impl.cli.commands.host.overview import _TARGETS_KIND_OPTIONS, _render_hosts, _render_targets, hosts, targets
from hyprial.shell.impl.cli.commands.common.support import _render_routine_list, _render_workflow_list
from hyprial.shell.impl.cli.commands.message.commands import _validate_from_identity, ack, delivery_app, delivery_status, outbox_app, outbox_list, outbox_prune, reply, send
from hyprial.shell.impl.cli.commands.log.commands import RFC3339, _AGENT_TIMELINE_EVENTS, _TRAJECTORY_LOG_EVENTS, _entry_timestamp, _format_trajectory, _is_log_entry, _is_trajectory_log_entry, _log_result, _parse_boundary, _read_log_history, _status_timestamp, _trajectory_event_state, _trajectory_node, _trajectory_ordering, _trajectory_result, log_command, query_command, trajectory_command
from hyprial.shell.impl.cli.commands.init.commands import FIRST_RUN_ROUTE_NAME, _complete_initialization, _configured_adapter_names, _initialize_org_context, _onboarding_apply_actions, _onboarding_scope_authorized, _onboarding_snapshot, _org_init_warning, _zenoh_endpoint_warning, init, onboarding_app, onboarding_apply, onboarding_plan
from hyprial.shell.impl.cli.commands.daemon.commands import daemon_app, daemon_run, daemon_status, daemon_stop
from hyprial.shell.impl.cli.commands.service import service_app
from hyprial.shell.impl.cli.commands.agent.grant import grants_check, grants_visibility
from hyprial.shell.impl.cli.commands.agent.runtime.start import start
from hyprial.shell.impl.cli.commands.agent.runtime.down import down
from hyprial.shell.impl.cli.commands.upgrade.commands import _desired_connector_rows, _expected_interruption_seconds, _follow_up_restore_confirmation, _pending_connector_count, _perform_upgrade, _perform_upgrade_and_report, _poll_restore_phase, _read_restore_phase, _resolve_upgrade_target, upgrade
from hyprial.shell.impl.cli.commands.common.pending_restart import PENDING_RESTART_FILE, _clear_pending_restart, _pending_restart_path, _read_pending_restart, _restart_daemon_onto_install, _write_pending_restart
from hyprial.shell.impl.cli.commands.daemon.restart import _daemon_restart_identity, _interrupted_turns, _record_autoupdate_restart_confirmation, _record_autoupdate_restart_not_required, _restart_is_interactive, _restart_status, restart_command
from hyprial.shell.impl.cli.commands.gui.commands import gui_command, gui_status, start_gui_background, stop_gui
from hyprial.shell.impl.cli.commands.login.flow import _ensure_login_device, _prove_daemon_absent, _run_login_cli_flow
from hyprial.shell.impl.cli.commands.squire.commands import _delivery_agent_live, _parse_combo, _squire_setup_operation, _stored_user_proxy_route, squire_app, squire_probe, squire_setup, user_proxy_app, user_proxy_setup, user_proxy_status
from hyprial.shell.impl.cli.commands.agent.admin import agent_app, agent_destroy, agent_keep_app, agent_list, agent_unblock, migration_app, secret_app
from hyprial.shell.impl.cli.commands.agent.create import _create_agent_for_start, agent_create, agent_host_invite
from hyprial.shell.impl.cli.commands.agent.grant import agent_grant, agent_grants, agent_revoke
from hyprial.shell.impl.cli.commands.agent.home import _MIGRATION_ROUTINE_BINDING_FIELDS, _guard_agent_migration_routines, _migration_manifest, _routine_binding_matches_agent, agent_home_census, agent_migrate_execute, agent_migrate_preflight, agent_migrate_rollback
from hyprial.shell.impl.cli.commands.agent.restore import agent_keep_add, agent_keep_list, agent_keep_remove, agent_restore_policy, agent_restore_threshold
from hyprial.shell.impl.cli.commands.agent.secret import agent_secret_entry_write, agent_secret_grant, agent_secret_list, agent_secret_revoke
from hyprial.shell.impl.cli.commands.agent.runtime.interactive import _announce_plugin_skips, _channel_registration_confirmed, _handover_prompt, _interactive_actor, _interactive_actor_name, _launch_detached_tui, _pi_attach_registration, _plugin_skip_warnings, _runtime_context_environment, _runtime_context_projection, _runtime_launch_custody, _terminate_process, _wait_foreground, _with_plugin_warnings, _write_launch_config
from hyprial.shell.impl.cli.commands.agent.runtime.claude import _CLAUDE_HARNESS_TOOLS, _CLAUDE_SESSION_IDENTITY_FLAGS, _reject_claude_session_identity_args, _start_interactive_claude
from hyprial.shell.impl.cli.commands.agent.runtime.sessions import _CODEX_SESSION_FLAGS, _PI_SESSION_IDENTITY_FLAGS, _reject_codex_session_args, _reject_pi_session_identity_args, _start_interactive_codex, _start_interactive_pi
from hyprial.shell.impl.cli.commands.user.commands import user_add, user_app, user_bind, user_bindings_audit, user_list, user_show, user_unbind, user_whois
from hyprial.shell.impl.cli.commands.adapter.admin import _adapter_secret_from_text, _confirm_adapter_stopped, _parse_adapter_routes, _present_verification_prompt, _read_adapter_secret, _verification_expiry, adapter_add, adapter_app, adapter_doctor, adapter_list, adapter_onboard, adapter_reload, adapter_remove, adapter_start, adapter_status, adapter_stop
from hyprial.shell.impl.cli.commands.adapter.authorize import _authorize_interactively, _present_authorization_prompt, adapter_authorize
from hyprial.shell.impl.cli.commands.adapter.pins import adapter_pin, adapter_pins, adapter_unpin
from hyprial.shell.impl.cli.commands.adapter.routes import _parse_one_route, _route_candidates, _route_error_code, _route_operation, adapter_route_add, adapter_route_candidates, adapter_route_list, adapter_route_remove, route_app
from hyprial.shell.impl.cli.commands.adapter.data import _checked_identity_kind, _identities_result, _identity_json, adapter_identities_find, adapter_identities_list, adapter_identities_sync, adapter_media_get, identities_app, media_app
from hyprial.shell.impl.cli.commands.meta.mcp import mcp_agent_channel, mcp_app, mcp_claude_channel, mcp_claude_channel_recover, mcp_claude_turn_ended
from hyprial.shell.impl.cli.commands.routine.commands import _routine_identity, routine_add, routine_list, routine_pause, routine_resume, routine_rm, routine_set, routine_status
from hyprial.shell.impl.cli.commands.workflow.misc import workflow_gc
from hyprial.shell.impl.cli.commands.workflow.internal.dispatch import dispatch_app, dispatch_matrix
from hyprial.shell.impl.cli.commands.host.network import network_app, network_expose, network_exposures, network_peer_key, network_unexpose
from hyprial.shell.impl.cli.commands.profile.commands import _profile_rows, _require_profile_name, profile_app, profile_create, profile_list, profile_use
from hyprial.shell.impl.cli.commands.adapter.lark_auth import _canonical_lark_auth_state_path, _lark_auth_guard_notify_anchor, _lark_auth_send, _lark_auth_state_path, _lark_auth_timer_check, lark_auth_app, lark_auth_check, lark_auth_complete
from hyprial.shell.impl.cli.commands.upgrade.autoupdate import _autoupdate_result, _local_minute, _pending_restart_state, _render_autoupdate_status, _timer_config, autoupdate_app, autoupdate_install, autoupdate_restart, autoupdate_run, autoupdate_status, autoupdate_uninstall
from hyprial.shell.impl.cli.commands.config.settings import config_app, config_set
from hyprial.shell.impl.cli.commands.fs.commands import _fs_run, fs_app, fs_checkout, fs_create, fs_export, fs_history, fs_import, fs_invite, fs_join, fs_ls, fs_members, fs_mkdir, fs_mv, fs_purge, fs_purge_plan, fs_purge_status, fs_read, fs_remove_member, fs_resolve, fs_restore, fs_rm, fs_serve, fs_spaces, fs_status, fs_trash, fs_unban, fs_watch, fs_web, fs_write
from hyprial.shell.impl.cli.commands.org.commands import _org_full_document, _org_meta, org_app, org_create, org_execute, org_fetch, org_import, org_invite, org_network, org_show, org_status
from hyprial.shell.impl.cli.commands.routine.commands import routine_app
from hyprial.shell.impl.cli.commands.work import work_app
from hyprial.shell.impl.cli.commands.workflow.run import workflow_app
from hyprial.shell.impl.cli.commands.transfer import (
    TransferCliDependencies,
    register_transfer_commands,
)


# --- late-bound wrappers for imported, externally rebindable names ---------
# Kept as thin wrappers so composition never imports daemon/
# identity modules at module level while monkeypatching entry.<name> keeps
# working (the services snapshot below reads these module globals).


def configured_hyprial_home(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.kernel.impl.home.configured_hyprial_home``."""
    from hyprial.kernel import configured_hyprial_home as _real

    return _real(*args, **kwargs)


def default_hyprial_home(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.kernel.impl.home.default_hyprial_home``."""
    from hyprial.kernel import default_hyprial_home as _real

    return _real(*args, **kwargs)


def notify_upgrade_failure(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.daemon.impl.autoupdate.alert.notify_upgrade_failure``."""
    from hyprial.daemon import notify_upgrade_failure as _real

    return _real(*args, **kwargs)


def process_cpu_seconds(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.daemon.impl.process_diagnostics.process_cpu_seconds``."""
    from hyprial.daemon import process_cpu_seconds as _real

    return _real(*args, **kwargs)


def record_alert_outcome(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.daemon.impl.autoupdate.alert.record_alert_outcome``."""
    from hyprial.daemon import record_alert_outcome as _real

    return _real(*args, **kwargs)


def require_initialized_hyprial_home(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.kernel.impl.home.require_initialized_hyprial_home``."""
    from hyprial.kernel import require_initialized_hyprial_home as _real

    return _real(*args, **kwargs)


def resolve_profile(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.daemon.impl.network_profile.resolve_profile``."""
    from hyprial.daemon import resolve_profile as _real

    return _real(*args, **kwargs)


def tcp_probe(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.daemon.impl.peer_reachability.tcp_probe``."""
    from hyprial.daemon import tcp_probe as _real

    return _real(*args, **kwargs)


def write_failure_marker(*args, **kwargs):
    """Late-bound passthrough to ``hyprial.daemon.impl.autoupdate.alert.write_failure_marker``."""
    from hyprial.daemon import write_failure_marker as _real

    return _real(*args, **kwargs)


def initialize_hyprial_home() -> Path:
    """Shell composition of the kernel-owned home initializer.

    Kernel owns selection/mkdir semantics; the dispatch-policy callback is
    daemon-owned and injected here so kernel never imports
    daemon (kernel signature decision, PY-BATCH-2026-10-01 integration).
    """
    from hyprial.daemon import ensure_dispatch_policy
    from hyprial.kernel import initialize_hyprial_home as _kernel_initialize

    return _kernel_initialize(
        ensure_dispatch_policy=ensure_dispatch_policy,
    )


def _cli_services() -> CliServices:
    # Read module globals when invoked, preserving the long-standing
    # monkeypatch contract.  Do not call home/state functions here.
    return CliServices(
        CliError=CliError,
        time=time,
        print=print,
        uuid4=uuid4,
        _complete_initialization=_complete_initialization,
        _create_agent_for_start=_create_agent_for_start,
        _daemon_probe=_daemon_probe,
        _daemon_ready_for_process=_daemon_ready_for_process,
        _daemon_request=_daemon_request,
        _describe_survivor=_describe_survivor,
        _dsh_host_describe=_dsh_host_describe,
        _execute=_execute,
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
        _start_interactive_pi=_start_interactive_pi,
        _state_dir=_state_dir,
        _stdin_isatty=_stdin_isatty,
        _stop_daemon_for_identity_switch=_stop_daemon_for_identity_switch,
        _stop_daemon_gracefully=_stop_daemon_gracefully,
        _stopped_process=_stopped_process,
        _stored_user_proxy_route=_stored_user_proxy_route,
        _squire_setup_operation=_squire_setup_operation,
        _timer_config=_timer_config,
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


set_service_resolver(_cli_services)


# --- group wiring (original add_typer order) -------------------------------
agent_app.add_typer(secret_app, name="secret")
agent_app.add_typer(migration_app, name="migrate")
agent_app.add_typer(agent_keep_app, name="keep")
app.add_typer(adapter_app, name="adapter")
app.add_typer(daemon_app, name="daemon")
app.add_typer(service_app, name="service")
app.add_typer(mcp_app, name="mcp")
app.add_typer(squire_app, name="squire")
app.add_typer(user_proxy_app, name="user-proxy")
app.add_typer(outbox_app, name="outbox")
app.add_typer(delivery_app, name="delivery")
app.add_typer(autoupdate_app, name="autoupdate")
app.add_typer(config_app, name="config")
app.add_typer(lark_auth_app, name="lark-auth")
app.add_typer(agent_app, name="agent")
app.add_typer(org_app, name="org")
org_app.add_typer(work_app, name="work")
app.add_typer(fs_app, name="fs")
app.add_typer(profile_app, name="profile")
app.add_typer(network_app, name="network")

transfer_runtime_app = typer.Typer(
    help="Probe an explicitly supplied transfer runtime without starting an agent.",
    no_args_is_help=True,
)
app.add_typer(transfer_runtime_app, name="transfer-runtime")
app.add_typer(workflow_app, name="workflow")
app.add_typer(dispatch_app, name="dispatch")
app.add_typer(routine_app, name="routine")
app.add_typer(onboarding_app, name="onboarding")
adapter_app.add_typer(route_app, name="route")
adapter_app.add_typer(identities_app, name="identities")
app.add_typer(user_app, name="user")
adapter_app.add_typer(media_app, name="media")


def _transfer_cli_dependencies() -> TransferCliDependencies:
    # Resolve functions when invoked, preserving existing CLI monkeypatch and
    # missing-home behavior. Do not call state/home functions during registration.
    return TransferCliDependencies(
        execute=_execute,
        daemon_request=_daemon_request,
        state_dir=_state_dir,
        error_type=CliError,
        configured_home=configured_hyprial_home,
        default_home=default_hyprial_home,
        print=print,
        parse_depends=_parse_depends,
    )


_transfer_registration_start = len(app.registered_commands)
_transfer_commands = register_transfer_commands(
    app, transfer_runtime_app, _transfer_cli_dependencies,
)
# Command modules register on import; the late dependency wiring must retain
# the original transfer slot before upgrade/restart/gui in generated help.
_transfer_registrations = app.registered_commands[_transfer_registration_start:]
del app.registered_commands[_transfer_registration_start:]
_upgrade_registration = next(
    index for index, command in enumerate(app.registered_commands)
    if command.callback is upgrade
)
app.registered_commands[_upgrade_registration:_upgrade_registration] = _transfer_registrations
del _transfer_registration_start, _transfer_registrations, _upgrade_registration
transfer_runtime_downgrade_state = _transfer_commands.transfer_runtime_downgrade_state
transfer_runtime_probe = _transfer_commands.transfer_runtime_probe
transfer = _transfer_commands.transfer
transfer_bundle_export = _transfer_commands.transfer_bundle_export
transfer_bundle_validate = _transfer_commands.transfer_bundle_validate
transfer_bundle_receive = _transfer_commands.transfer_bundle_receive
transfer_bundle_land = _transfer_commands.transfer_bundle_land
transfer_credential_seal = _transfer_commands.transfer_credential_seal
transfer_credential_open = _transfer_commands.transfer_credential_open
transfer_bundle_complete = _transfer_commands.transfer_bundle_complete
transfer_precheck = _transfer_commands.transfer_precheck
transfer_receive = _transfer_commands.transfer_receive
transfer_cred_stage = _transfer_commands.transfer_cred_stage
transfer_container_home = _transfer_commands.transfer_container_home
transfer_session_path = _transfer_commands.transfer_session_path
_parse_depends = _transfer_commands.parse_depends


def main() -> None:
    """Installed ``hyprial`` entry point."""

    json_output = "--json" in sys.argv[1:]
    try:
        exit_code = app(standalone_mode=False)
    except TyperClickException as error:
        report_error(
            RequestPortError(ipc_errors.INVALID_ARGUMENT, error.format_message()),
            json_output=json_output,
        )
        raise SystemExit(error.exit_code) from error
    except (TyperAbort, typer.Abort) as error:  # typer.confirm raises typer.Abort
        report_error(RequestPortError("ABORTED", "operation aborted"), json_output=json_output)
        raise SystemExit(1) from error
    if isinstance(exit_code, int) and exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()



# --- public shape (pre-extraction hyprial.cli module attributes) ----------
__all__ = [
    "CliError", "DAEMON_START_HARD_CAP_SECONDS", "DAEMON_START_IDLE_BUDGET_SECONDS", "FIRST_RUN_ROUTE_NAME", "JsonObject",
    "PENDING_RESTART_FILE", "RFC3339", "_AGENT_TIMELINE_EVENTS", "_CLAUDE_HARNESS_TOOLS", "_CLAUDE_SESSION_IDENTITY_FLAGS",
    "_CODEX_SESSION_FLAGS", "_CONNECT_BACKLOG_ERRNOS", "_CONNECT_BACKLOG_RETRY_INTERVAL", "_CUSTODY_STARTUP_ERROR_SHAPES", "_DAEMON_IPC_ROUNDTRIP_SECONDS",
    "_DAEMON_LOG_READ_BYTES", "_DEVICE_SIGN_IN_HINT", "_INIT_READY_TIMEOUT_ENV",
    "_MIGRATION_ROUTINE_BINDING_FIELDS", "_PEER_GONE_ERRNOS", "_PI_SESSION_IDENTITY_FLAGS", "_RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS",
    "_RESTORE_WAIT_DEFAULT_SECONDS", "_RESTORE_WAIT_MAX_SECONDS", "_RESTORE_WAIT_POLL_SECONDS", "_SAFE_DAEMON_STARTUP_EVENTS", "_SQUIRE_SETUP_PARTS",
    "_TARGETS_KIND_OPTIONS", "_TOP_SEVERITY", "_TRAJECTORY_LOG_EVENTS", "_WORKTREE_GIT_TIMEOUT_SECONDS",
    "_WORKTREE_STATES", "_adapter_secret_from_text", "_agent_workspace", "_announce_plugin_skips",
    "_announce_setup_guidance", "_append_warning", "_authorize_interactively", "_autoupdate_result", "_canonical_lark_auth_state_path",
    "_channel_registration_confirmed", "_checked_identity_kind", "_clear_pending_restart", "_complete_initialization",
    "_configured_adapter_names", "_confirm_adapter_stopped", "_connect_daemon_socket", "_create_agent_for_start",
    "_daemon_launch_capture", "_daemon_launch_log_summary", "_daemon_probe", "_daemon_ready_for_process", "_daemon_request",
    "_daemon_request_once", "_daemon_restart_identity", "_daemon_startup_failure_evidence", "_daemon_startup_phase_summary", "_delivery_agent_live",
    "_describe_survivor", "_desired_connector_rows", "_device_key_doctor_check", "_dirty_activity_at_ms", "_dirty_paths", "_display_worktree_path",
    "_doctor_result", "_dsh_doctor_check", "_dsh_endpoint_label", "_dsh_host_describe", "_duplicate_instance_doctor_check",
    "_emit", "_endpoint_args", "_ensure_login_device", "_entry_timestamp", "_execute", "_expected_interruption_seconds",
    "_fail", "_follow_up_restore_confirmation", "_format_quota_window", "_format_top_age", "_format_top_reset",
    "_format_top_rss", "_format_top_seconds", "_format_top_window_seconds", "_format_trajectory", "_fs_run",
    "_git_count", "_guard_agent_migration_routines", "_handover_prompt", "_historical_inbox_doctor_check", "_hyprial_home",
    "_identities_result", "_identity_json", "_initialize_org_context", "_install_locked_requirement", "_interactive_actor",
    "_interactive_actor_name", "_interrupted_turns", "_is_log_entry", "_is_trajectory_log_entry", "_json_failure",
    "_lark_auth_guard_notify_anchor", "_lark_auth_send", "_lark_auth_state_path", "_lark_auth_timer_check",
    "_lark_inbound_doctor_check", "_launch_daemon_process", "_launch_daemon_process_locked", "_launch_detached_tui", "_launch_process_error",
    "_local_minute", "_local_operator_identity", "_log_result",
    "_login_route_is_first_time_setup", "_maintenance_doctor_check", "_mcp_channel_doctor_check", "_message_claim_params", "_migration_manifest",
    "_observe_restart_process", "_onboarding_apply_actions", "_onboarding_scope_authorized", "_onboarding_snapshot",
    "_org_full_document", "_org_init_warning", "_org_meta", "_overview_age", "_overview_duration",
    "_overview_last_segment", "_overview_next", "_overview_sender", "_overview_table", "_pac_gc_doctor_check",
    "_parse_adapter_routes", "_parse_boundary", "_parse_combo", "_parse_depends", "_parse_one_route",
    "_parse_ps_duration", "_parse_worktree_porcelain", "_peer_gone",
    "_pending_connector_count", "_pending_restart_path",
    "_pending_restart_state", "_perform_upgrade", "_perform_upgrade_and_report", "_pi_attach_registration", "_plugin_skip_warnings",
    "_poll_restore_phase", "_present_authorization_prompt", "_present_verification_prompt", "_prime_post_install_restart_imports", "_probe_reports_running",
    "_profile_rows", "_prove_daemon_absent", "_read_adapter_secret", "_read_log_history", "_read_pending_restart",
    "_read_restore_phase", "_record_autoupdate_restart_confirmation", "_record_autoupdate_restart_not_required", "_reject_claude_session_identity_args", "_reject_codex_session_args",
    "_reject_pi_session_identity_args", "_render_autoupdate_status", "_render_hosts", "_render_routine_list", "_render_targets",
    "_render_top", "_render_top_quota", "_render_workflow_list", "_render_worktree_repository", "_require_installed_commit",
    "_require_profile_name", "_resolve_upgrade_target", "_resolve_worktree_repository", "_resolved_agent_cwd",
    "_restart_daemon_onto_install", "_restart_is_interactive", "_restart_status", "_restore_doctor_check", "_restore_wait_budget",
    "_route_candidates", "_route_error_code", "_route_operation", "_routine_binding_matches_agent", "_routine_health_doctor_check",
    "_routine_identity", "_run_login_cli_flow", "_run_worktree_git",
    "_running_daemon_before_upgrade", "_runtime_context_environment", "_runtime_context_projection", "_runtime_launch_custody",
    "_sample_process_table", "_scan_worktree", "_scan_worktree_repository", "_semantic_version", "_socket_path",
    "_squire_setup_operation", "_squire_setup_warning", "_start_interactive_claude", "_start_interactive_codex", "_start_interactive_pi",
    "_state_dir", "_status_timestamp", "_stdin_isatty", "_stop_daemon_for_identity_switch", "_stop_daemon_for_operator",
    "_stop_daemon_gracefully", "_stop_daemon_with_proof", "_stopped_process", "_stored_user_proxy_route", "_subtree_totals",
    "_tailcat_sidecar_doctor_check",
    "_terminate_process", "_timer_config", "_top_state_label", "_trajectory_event_state", "_trajectory_node",
    "_trajectory_ordering", "_trajectory_result", "_transfer_cli_dependencies", "_transfer_commands",
    "_user_proxy_launch_params", "_user_proxy_setup_warning", "_utc_now", "_validate_from_identity",
    "_verification_expiry", "_version_result", "_wait_daemon_teardown_receipt", "_wait_for_daemon", "_wait_foreground",
    "_with_plugin_warnings", "_workflow_worker_cleanup_doctor_check", "_worktree_bases", "_worktree_git_output",
    "_worktree_ref_exists", "_worktree_remove_command", "_write_launch_config", "_write_pending_restart", "_zenoh_doctor_check",
    "_zenoh_endpoint_warning", "ack", "adapter_add", "adapter_app", "adapter_authorize",
    "adapter_doctor", "adapter_identities_find", "adapter_identities_list", "adapter_identities_sync",
    "adapter_list", "adapter_media_get", "adapter_onboard", "adapter_pin", "adapter_pins",
    "adapter_reload", "adapter_remove", "adapter_route_add", "adapter_route_candidates", "adapter_route_list",
    "adapter_route_remove", "adapter_start", "adapter_status", "adapter_stop", "adapter_unpin",
    "agent_app", "agent_create", "agent_destroy", "agent_grant", "agent_grants",
    "agent_home_census", "agent_host_invite", "agent_keep_add", "agent_keep_app", "agent_keep_list",
    "agent_keep_remove", "agent_list", "agent_migrate_execute", "agent_migrate_preflight", "agent_migrate_rollback",
    "agent_restore_policy", "agent_restore_threshold", "agent_revoke", "agent_secret_entry_write", "agent_secret_grant",
    "agent_secret_list", "agent_secret_revoke", "agent_unblock", "app", "autoupdate_app",
    "autoupdate_install", "autoupdate_restart", "autoupdate_run", "autoupdate_status", "autoupdate_uninstall",
    "config_app", "config_set", "configured_hyprial_home", "daemon_app", "daemon_run",
    "daemon_status", "daemon_stop", "default_hyprial_home", "delivery_app", "delivery_status", "dispatch_app",
    "dispatch_matrix", "doctor", "down", "fs_app", "fs_checkout",
    "fs_create", "fs_export", "fs_history", "fs_import", "fs_invite",
    "fs_join", "fs_ls", "fs_members", "fs_mkdir", "fs_mv",
    "fs_purge", "fs_purge_plan", "fs_purge_status", "fs_read", "fs_remove_member",
    "fs_resolve", "fs_restore", "fs_rm", "fs_serve", "fs_spaces",
    "fs_status", "fs_trash", "fs_unban", "fs_watch", "fs_web",
    "fs_write", "grants_check", "grants_visibility", "gui_command", "gui_status",
    "help_command", "hosts", "identities_app", "init", "initialize_hyprial_home",
    "install", "lark_auth_app", "lark_auth_check", "lark_auth_complete",
    "log_command", "login", "main", "mcp_agent_channel", "mcp_app",
    "mcp_claude_channel", "mcp_claude_channel_recover", "mcp_claude_turn_ended", "media_app", "migration_app",
    "network_app", "network_expose", "network_exposures", "network_peer_key", "network_unexpose",
    "notify_upgrade_failure", "old_pid_from", "onboarding_app", "onboarding_apply",
    "onboarding_plan", "org_app", "org_create", "org_execute", "org_fetch", "org_import",
    "org_invite", "org_network", "org_show",
    "org_status", "outbox_app", "outbox_list", "outbox_prune", "process_cpu_seconds",
    "process_status", "profile_app", "profile_create", "profile_list", "profile_use",
    "query_command", "record_alert_outcome", "reply", "require_initialized_hyprial_home", "resolve_profile",
    "restart_command", "route_app", "routine_add", "routine_app", "routine_list",
    "routine_pause", "routine_resume", "routine_rm", "routine_set", "routine_status",
    "secret_app", "send", "service_app", "shutil", "socket",
    "squire_app", "squire_probe", "squire_setup", "start", "start_gui_background",
    "stop_gui", "subprocess", "targets", "tcp_probe", "time",
    "top_status", "trajectory_command", "transfer", "transfer_bundle_complete", "transfer_bundle_export",
    "transfer_bundle_land", "transfer_bundle_receive", "transfer_bundle_validate", "transfer_container_home", "transfer_cred_stage",
    "transfer_credential_open", "transfer_credential_seal", "transfer_precheck", "transfer_receive", "transfer_runtime_app",
    "transfer_runtime_downgrade_state", "transfer_runtime_probe", "transfer_session_path", "upgrade",     "user_add", "user_app", "user_bind", "user_bindings_audit", "user_list",
    "user_proxy_app", "user_proxy_setup", "user_proxy_status", "user_show", "user_unbind", "user_whois",
    "uuid4", "version", "workflow_app", "workflow_gc", "worktrees_command",
    "write_failure_marker",
    "work_app",
]
