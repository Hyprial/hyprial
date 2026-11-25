# Decision record:
# - One implementation turns a constructed ``DaemonApplication`` into the
#   serving end of a promoted bootstrap.  Its one caller is the daemon's
#   ownerless lifecycle loop; the GUI host that used to carry a second copy
#   (``run_promoted``) was deleted in Z-6b.
# - The helper only wires the serving onboarding port: it migrates the
#   journal's transport generation, adopts the migrated surface, installs the
#   port and marks the application as serving.  It never starts, stops or
#   interrupts anything; the caller owns ``run_daemon`` and ``interrupt``.
"""Install the serving onboarding port on a promoted application."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.daemon.impl.onboarding.operations import (
    ControllerIdentity,
    RecoveryAuthority,
)
from hyprial.daemon.impl.onboarding.runtime import BootstrapRuntime
from hyprial.daemon.impl.request_catalog.onboarding import CallContext

from ..account_lifecycle import ServingOnboardingGateway

__all__ = ["install_serving_port"]


def install_serving_port(
    runtime: BootstrapRuntime,
    application: Any,
    *,
    context: CallContext,
    completed: Callable[[], None],
) -> Any:
    """Migrate the claim to ``context`` and mount the serving port; return the application."""

    surface = context.surface
    owner = context.owner
    controller = ControllerIdentity.current()
    port = ServingOnboardingGateway(
        operations=runtime.operation_service,
        home=runtime.home,
        complete_initialize=completed,
        promotion_operation_id=runtime.claimed_operation_id,
        request_stop=lambda: application._request_stop("auth-logout"),
    )
    port.migrate_generation(
        context,
        authority=RecoveryAuthority(
            home_binding=surface.home_binding,
            host_control_session_id=surface.host_control_session_id,
            view_session_id=surface.view_session_id,
            owner=owner,
            controller_pid=controller.pid,
            controller_birth=controller.birth_identity,
        ),
    )
    runtime.adopt_promoted_surface(surface)
    application.install_onboarding(port)
    application._onboarding_serving = True
    return application
