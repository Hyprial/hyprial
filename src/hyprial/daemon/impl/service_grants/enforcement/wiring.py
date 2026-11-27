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
# - An unexpose registers its port with the enforcer, takes the port off the
#   sidecar and out of the exposure store, and revokes the port's grants once
#   that succeeded. The registration, not a lock held across the removal, is
#   what keeps a derivation that listed the exposure a moment earlier from
#   exposing the port again: the derivation sees the registration and drops
#   its push. Only a push of that same port already in flight is waited for,
#   with a bound (S4-py fix, 2026-11-27, critic finding 2), so the removal is
#   the port's last command on the wire; at the bound the unexpose fails
#   before the removal is asked for, and nothing has changed. The removal is
#   a second wait, behind whatever command the forwarding authority is
#   running; that one is the forwarding layer's. The revocation is best
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

from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
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
    """Take a port off the sidecar, then revoke its grants, without reopening it.

    ``remove`` is the caller's own step that takes the port off the sidecar
    and out of the exposure store; its result or exception is passed through
    unchanged. Only after it succeeded is the port forgotten, which revokes
    this device's grants for it and never raises.

    There are two waits, and this function owns the first.

    A derivation may be pushing this very port. The port is registered as
    being removed first, which makes any derivation that listed it earlier
    drop its push, and a push of it already in flight is waited for, so that
    the removal is the port's last command on the wire. That wait is bounded
    by one sidecar push (``DEFAULT_UNEXPOSE_WAIT_SECONDS``, 15 s). At the
    bound the unexpose fails with the error the exposure path already
    reports and nothing has changed: the exposure is still desired, still
    on the sidecar, and its grants are still effective, because the removal
    has not been asked for yet. The caller may retry.

    The second wait is inside ``remove``: the forwarding authority runs one
    sidecar command at a time, so the removal is queued behind whatever is
    ahead of it on that queue, a roster read or another port's push among
    them. Only the forwarding layer's own timeouts bound it (a roster read
    by its budget, 5 s, a push by the response timeout, 10 s), and they
    bound each command ahead, not the queue. The enforcer cannot shorten
    that wait, and it does not fail the unexpose. So the bound written here
    is the bound of the first wait alone, and the first wait is the only
    one that can end in "nothing changed".

    When ``remove`` itself fails the registration is withdrawn and the sets
    are marked stale, so the port is derived again as the exposure it still
    is; its grants are not touched. A second unexpose of a port whose first
    is still under way fails at once with the same error code and changes
    nothing: the first one's outcome is the one to read. A daemon without a
    forwarding supervisor has no enforcer and no derivation, so ``remove``
    runs as it is.
    """

    enforcer = getattr(app, "_service_admission_enforcer", None)
    if enforcer is None:
        return remove()
    try:
        free = enforcer.begin_removal(port)
    except ValueError as error:
        raise ForwardingSidecarError(str(error)) from error
    if not free:
        raise ForwardingSidecarError(
            f"port {port} is being pushed to the sidecar; unexpose it again"
        )
    try:
        result = remove()
    except BaseException:
        enforcer.cancel_removal(port)
        raise
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
