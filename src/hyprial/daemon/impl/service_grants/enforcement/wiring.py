# Decision notes (service grant S4-py, 2026-11-26):
# - The daemon owns one enforcer for the lifetime of its forwarding supervisor
#   and one bounded cadence that runs it. A trigger never derives: it marks the
#   inputs stale and offers the cadence a tick. The derivation, with its
#   bounded roster read and its pushes, therefore runs only on the cadence's
#   effect lane, never on an IPC thread, on the scheduler thread or on the
#   sidecar's relaunch path.
# - The supervisor's installed hook is the first trigger, and the only one at
#   start-up. Installing the enforcer offers no tick, because until a sidecar
#   is up there is nothing to push to and such a tick would only find no
#   backend. The hook fires once per sidecar incarnation, right after the
#   replay that left every exposure admitting nobody.
# - The maintenance tick drives the same cadence every second without marking
#   anything stale. That is the throttled re-derivation: it picks up a grant
#   written on another path, directory changes, expiry, and a roster that
#   completed after the grant was written. It also re-offers a tick after one
#   that the cadence could not admit, so a stale mark is never lost.
# - The inputs are read per derivation through the application's public
#   places, exactly as the service grant IPC handler reads them per call. A
#   long-lived object that captured the OrgFS runtime or the owner at creation
#   would keep serving the previous identity after a relogin.
# - An unexpose takes the port off the sidecar and out of the exposure store
#   under the enforcer's own lock, and revokes the port's grants once that
#   succeeded. The lock is what keeps a derivation that listed the exposure a
#   moment earlier from exposing the port again; the revocation is best
#   effort: the enforcer reports a failure as one log event and never raises
#   into the caller, which is already past the point where the port should
#   stay open.
# - A trigger reports whether the tick it offered reached the cadence. It never
#   raises, because the change that triggered it is already durable, but it
#   returns what happened and the caller logs it: a grant whose re-derivation
#   was not scheduled must not look exactly like one whose was. The stale mark
#   is set whatever the outcome, so the maintenance tick still derives.
# - When there is no supervisor there is no sidecar to push to and nothing is
#   created: a daemon without forwarding has no exposures to admit anyone to.
"""Binding the admission enforcer to the daemon application."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.identity import read_device_record
from hyprial.kernel import AdmissionResult

from ..ipc import build_service, quarantine_for
from .enforcer import ServiceAdmissionEnforcer
from .models import EnforcementInputs

CADENCE = "service-admission"
TRIGGER_FAILED = "service.grant.admission_trigger_failed"


def build_enforcer(app: Any) -> ServiceAdmissionEnforcer:
    """The application's enforcer, over its live supervisor and exposure store."""

    log_fn = app._log
    return ServiceAdmissionEnforcer(
        supervisor=app._forwarding_supervisor,
        exposure_store=app._exposure_store,
        grants_service=lambda: build_service(app),
        inputs=lambda: _inputs(app),
        now_ms=lambda: time.time_ns() // 1_000_000,
        logger=lambda level, component, event, **fields: log_fn(
            level, component, event, **fields
        ),
    )


def _inputs(app: Any) -> EnforcementInputs:
    fs = app._require_orgfs_runtime().facade
    home = app.hyprial_home
    record = read_device_record(home)
    return EnforcementInputs(
        store=OrgFsDirectoryStore(fs, invalid_devices=quarantine_for(app)),
        fs=fs,
        home=home,
        owner=app.owner,
        device_id=record.device_id if record is not None else "",
    )


#: A trigger that could not offer its tick; the event's ``reason`` is one of these.
TRIGGER_RAISED = "failed"
TRIGGER_UNSCHEDULED = "unscheduled"


def mark_stale(app: Any) -> str | None:
    """A trigger: the inputs changed, so the next tick must derive.

    Returns ``None`` when the tick reached the cadence, and otherwise the
    ``reason`` of the ``service.grant.admission_trigger_failed`` event the
    caller is to log. Never raises: the change that triggered it is durable,
    so the caller's own operation must succeed either way.

    Runs on IPC threads and on the sidecar's relaunch path, so it does the
    least that is sufficient: one assignment under the enforcer's own lock,
    then one non-blocking submit. The mark is set before the submit, so a
    tick that cannot be offered now is still owed to the maintenance tick.
    A daemon without a forwarding supervisor has no enforcer and nothing to
    push, so there is nothing to mark and nothing to report.
    """

    try:
        enforcer = getattr(app, "_service_admission_enforcer", None)
        if enforcer is None:
            return None
        enforcer.invalidate()
        return None if offer_tick(app) else TRIGGER_UNSCHEDULED
    except Exception:  # noqa: BLE001 - reported by its return value
        return TRIGGER_RAISED


def trigger_failed(app: Any, method: str, reason: str) -> None:
    """Log one undeliverable trigger: the operation and the kind of failure, nothing else."""

    log_fn = getattr(app, "_log", None)
    if not callable(log_fn):
        return
    try:
        log_fn("warn", "service", TRIGGER_FAILED, method=method, reason=reason)
    except Exception:  # noqa: BLE001 - logging must not fail the caller's operation
        pass


def offer_tick(app: Any) -> bool:
    """Offer the cadence one tick without marking anything stale.

    Returns ``True`` only when the cadence accepted the tick: ``False`` when
    there is no cadence, when it is closed, or when its mailbox is full. A
    tick the cadence cannot admit is not a loss: the maintenance tick offers
    the next one within about a second, and the enforcer keeps a stale mark
    until a derivation has run.
    """

    cadence = getattr(app, "_service_admission_cadence", None)
    if cadence is None:
        return False
    return cadence.submit(time.time_ns() // 1_000_000) is AdmissionResult.ACCEPTED


def install(app: Any) -> None:
    """Create the enforcer and its cadence for the supervisor that now exists.

    Called after the supervisor is on the application and before it is
    started. It offers no tick of its own: there is no sidecar to push to yet.
    The supervisor reports through its installed hook as soon as a sidecar is
    up, and that hook is the first trigger, after the start-up replay that
    left every exposure admitting nobody.
    """

    from hyprial.daemon.impl.correlation.bounded_cadence import BoundedCadence

    enforcer = build_enforcer(app)

    def effect(_observed_at_ms: int) -> None:
        enforcer.tick()

    app._service_admission_enforcer = enforcer
    app._service_admission_cadence = BoundedCadence(
        CADENCE,
        effect,
        actor_host=app._agent_session_domains.actor_hosts.host_for("daemon-runtime"),
    )


def retire(app: Any) -> None:
    """Close the cadence and drop the enforcer. Safe to call twice."""

    cadence = getattr(app, "_service_admission_cadence", None)
    if cadence is not None:
        cadence.close(5.0)
        app._service_admission_cadence = None
    app._service_admission_enforcer = None


def unexpose_port(app: Any, port: int, remove: Callable[[], Any]) -> Any:
    """Take a port off the sidecar with no derivation in flight, then revoke its grants.

    ``remove`` is the caller's own step that takes the port off the sidecar
    and out of the exposure store; it runs under the enforcer's lock and its
    result or exception is passed through unchanged. Only after it succeeded
    is the port forgotten, which revokes this device's grants for it and
    never raises. A daemon without a forwarding supervisor has no enforcer
    and no derivation, so ``remove`` runs as it is.
    """

    enforcer = getattr(app, "_service_admission_enforcer", None)
    if enforcer is None:
        return remove()
    with enforcer.exclusive():
        result = remove()
        enforcer.forget_port(port)
    return result


__all__ = [
    "CADENCE",
    "TRIGGER_FAILED",
    "TRIGGER_RAISED",
    "TRIGGER_UNSCHEDULED",
    "build_enforcer",
    "install",
    "mark_stale",
    "offer_tick",
    "retire",
    "trigger_failed",
    "unexpose_port",
]
