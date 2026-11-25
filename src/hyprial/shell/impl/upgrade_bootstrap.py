"""Public shell bootstrap for the old-interpreter upgrade tail.

The CLI must not import backend helpers just to keep their modules resident
before ``uv tool install`` replaces the running distribution.  This shell
capability owns that initialization boundary and returns the loaded module set;
the CLI only calls the public shell symbol.
"""

from __future__ import annotations

import sys


def prime_post_install_imports() -> frozenset[str]:
    """Load every module the post-install restart tail may resolve lazily."""

    from hyprial.daemon import (
        AUTOUPDATE_CHILD_ENV,
        AUTOUPDATE_TRIGGER_ENV,
        DesiredStateStore,
        ForwardingConfigurationError,
        RestartProcessObservation,
        RestartProcessState,
        ServiceConfig,
        ServiceManager,
        ServiceOwnershipError,
        ensure_dispatch_policy,
        harness_desired_generation,
        latest_start_failure,
        node_owner_or_none,
        owner_process_status,
        process_cpu_seconds,
        read_last_run,
        read_process_identity,
        resolve_node_owner,
        resolve_profile,
        restart_failure_detail,
        restore_followup_budget_seconds,
        run_mcp_entry,
        run_self_check,
        run_upgrade_steps,
        start_failure_line,
        tcp_probe,
        write_last_run,
    )
    from hyprial.identity import (
        EXPECTED_IDENTITY_GENERATION_ENV,
        IdentityTransactionBusy,
        IdentityTransactionLock,
    )
    from hyprial.kernel import DaemonLaunchResult, HarnessLaunchSpec
    from hyprial.shell.impl.gui import launcher as gui_launcher
    from hyprial.shell.impl.gui import termination as gui_termination
    from hyprial.shell.impl.login import generation as login_generation
    from hyprial.shell.impl.gui.apps import upgrade as unpack_gui
    from hyprial.shell.impl.gui.install import launch as gui_install_launch

    _ = (
        AUTOUPDATE_CHILD_ENV,
        AUTOUPDATE_TRIGGER_ENV,
        DesiredStateStore,
        ForwardingConfigurationError,
        RestartProcessObservation,
        RestartProcessState,
        ServiceConfig,
        ServiceManager,
        ServiceOwnershipError,
        ensure_dispatch_policy,
        harness_desired_generation,
        latest_start_failure,
        node_owner_or_none,
        owner_process_status,
        process_cpu_seconds,
        read_last_run,
        read_process_identity,
        resolve_node_owner,
        resolve_profile,
        restart_failure_detail,
        restore_followup_budget_seconds,
        run_mcp_entry,
        run_self_check,
        run_upgrade_steps,
        start_failure_line,
        tcp_probe,
        write_last_run,
        EXPECTED_IDENTITY_GENERATION_ENV,
        IdentityTransactionBusy,
        IdentityTransactionLock,
        DaemonLaunchResult,
        HarnessLaunchSpec,
        gui_install_launch,
        gui_launcher,
        gui_termination,
        unpack_gui,
        login_generation,
    )
    return frozenset(
        name for name in sys.modules if name == "hyprial" or name.startswith("hyprial.")
    )
