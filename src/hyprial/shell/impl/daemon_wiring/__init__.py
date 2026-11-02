"""Explicit upper composition callbacks for DaemonApplication."""

from .routines import routine_runtime_factory, routine_template_renderer, routine_text_loader
from .usage import usage_factory
from .quota import quota_evaluator_factory, quota_hook_consumer_factory
from .hooks import hook_consumers, reply_reminder_consumer_factory


def daemon_stdio_stamp():
    """Return ``(stamp, context)`` that stamps a supervised daemon's stdio.

    A service manager appends stdout/stderr to one file across restarts, so
    each record must name when it was written and which daemon wrote it.
    """
    from hyprial.kernel import LogStamp, stamp_std_streams

    stamp = LogStamp()
    return stamp, stamp_std_streams(stamp)


def daemon_dependencies():
    """Constructor callbacks; usage_factory belongs only to from_environment."""
    from hyprial.biz import ServiceDiscoveryError, discover_installed_services
    from hyprial.daemon import ServiceConnectError
    from hyprial.kernel import ipc_errors

    def service_discovery():
        # Convert at the composition boundary: the daemon consumes this
        # callback without knowing biz types (rank direction), while the
        # operator-facing message (directory + offending field names, the
        # split spec's field-naming requirement) survives verbatim.
        try:
            return discover_installed_services()
        except ServiceDiscoveryError as error:
            raise ServiceConnectError(
                ipc_errors.SERVICE_REGISTRY_INVALID, str(error)
            ) from error

    return {
        "quota_evaluator_factory": quota_evaluator_factory,
        "quota_hook_consumer_factory": quota_hook_consumer_factory,
        "reply_reminder_consumer_factory": reply_reminder_consumer_factory,
        "hook_consumers": hook_consumers,
        "routine_runtime_factory": routine_runtime_factory,
        "routine_text_loader": routine_text_loader,
        "routine_template_renderer": routine_template_renderer,
        "service_discovery": service_discovery,
    }


__all__ = ["daemon_dependencies", "usage_factory"]
