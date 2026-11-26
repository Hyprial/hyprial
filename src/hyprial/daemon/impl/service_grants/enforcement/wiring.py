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
# - When there is no supervisor there is no sidecar to push to and nothing is
#   created: a daemon without forwarding has no exposures to admit anyone to.
"""Binding the admission enforcer to the daemon application."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.identity import read_device_record

from ..ipc import build_service, quarantine_for
from .enforcer import ServiceAdmissionEnforcer
from .models import EnforcementInputs

CADENCE = "service-admission"


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


def mark_stale(app: Any) -> None:
    """A trigger: the inputs changed, so the next tick must derive. Never raises.

    Runs on IPC threads and on the sidecar's relaunch path, so it does the
    least that is sufficient: one assignment under the enforcer's own lock,
    then one non-blocking submit. A daemon without a forwarding supervisor has
    no enforcer and nothing to push, so there is nothing to mark.
    """

    try:
        enforcer = getattr(app, "_service_admission_enforcer", None)
        if enforcer is None:
            return
        enforcer.invalidate()
        offer_tick(app)
    except Exception:  # noqa: BLE001 - the change that triggered it is durable
        pass


def offer_tick(app: Any) -> bool:
    """Offer the cadence one tick without marking anything stale.

    Returns ``False`` when there is no cadence to offer one to. A tick the
    cadence cannot admit is not a loss: the maintenance tick offers the next
    one within about a second, and the enforcer keeps a stale mark until a
    derivation has run.
    """

    cadence = getattr(app, "_service_admission_cadence", None)
    if cadence is None:
        return False
    cadence.submit(time.time_ns() // 1_000_000)
    return True


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
    "build_enforcer",
    "install",
    "mark_stale",
    "offer_tick",
    "retire",
    "unexpose_port",
]
