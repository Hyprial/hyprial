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
        OwnerMigrationHostedConflict,
        RestartProcessObservation,
        RestartProcessState,
        ServiceConfig,
        ServiceManager,
        ServiceOwnershipError,
        ensure_dispatch_policy,
        harness_desired_generation,
        latest_start_failure,
        notify_restore_followup,
        notify_upgrade_outcome,
        owner_process_status,
        prime_upgrade_alert_imports,
        process_cpu_seconds,
        read_last_run,
        read_process_identity,
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
        IDENTITY_TRANSACTION_FD_ENV,
        IdentityTransactionBusy,
        IdentityTransactionLock,
        UserProfileError,
        UserProfileStore,
    )
    from hyprial.kernel import DaemonLaunchResult, HarnessLaunchSpec, parse_channel_uri
    from hyprial.shell.impl.gui import runtime as gui_runtime
    from hyprial.shell.impl.gui.apps import upgrade as unpack_gui

    _ = (
        AUTOUPDATE_CHILD_ENV,
        AUTOUPDATE_TRIGGER_ENV,
        DesiredStateStore,
        ForwardingConfigurationError,
        OwnerMigrationHostedConflict,
        RestartProcessObservation,
        RestartProcessState,
        ServiceConfig,
        ServiceManager,
        ServiceOwnershipError,
        ensure_dispatch_policy,
        harness_desired_generation,
        latest_start_failure,
        notify_restore_followup,
        notify_upgrade_outcome,
        owner_process_status,
        prime_upgrade_alert_imports,
        process_cpu_seconds,
        read_last_run,
        read_process_identity,
        resolve_profile,
        restart_failure_detail,
        restore_followup_budget_seconds,
        run_mcp_entry,
        run_self_check,
        run_upgrade_steps,
        start_failure_line,
        tcp_probe,
        write_last_run,
        IDENTITY_TRANSACTION_FD_ENV,
        IdentityTransactionBusy,
        IdentityTransactionLock,
        UserProfileError,
        UserProfileStore,
        DaemonLaunchResult,
        HarnessLaunchSpec,
        parse_channel_uri,
        unpack_gui,
        gui_runtime,
    )
    prime_upgrade_alert_imports()
    return frozenset(
        name for name in sys.modules if name == "hyprial" or name.startswith("hyprial.")
    )
