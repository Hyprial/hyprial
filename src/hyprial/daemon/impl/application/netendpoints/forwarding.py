"""Forwarding sidecar operations: supervisor start, exposure warnings, redial and status."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import TYPE_CHECKING
from hyprial.kernel import FORWARDING_COMMAND_ENV
from hyprial.daemon.impl.bootstrap.discovery  import (
    ForwardingEndpoints,
    merge_endpoints,
)
from hyprial.daemon.impl.forwarding  import (
    ForwardingSidecarError,
    ForwardingSidecarSupervisor,
)
from hyprial.daemon.impl.forwarding.slots import (
    ensure_slots_dir,
    reconcile_slot_bindings,
    slot_store_for_app,
    slot_transport,
)
if TYPE_CHECKING:
    pass




#: How often an owner/admin daemon fulfils org leave requests (§4.3).
_ORG_LEAVE_INTERVAL_SECONDS = 30.0
_ORG_KEY_PUBLICATION_INTERVAL_SECONDS = 10.0


def record_tcp_slot_ports(app: object, reported: tuple[str, ...]) -> None:
    """Persist resolved TCP slots and surface a one-time port fallback."""

    store = slot_store_for_app(app)
    requested = store.specs()
    mismatches = store.record_tcp_ports(reported, requested=requested)
    for index, wanted, got in mismatches:
        app._log(
            "warn",
            "zenoh",
            "zenoh.slots.port_fallback",
            slot=index,
            requested=wanted,
            reported=got,
            detail="persisted tcp slot port was unavailable; using the reported port",
        )


class _ForwardingOpsMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _start_forwarding_supervisor(self) -> None:
        if (
            not self._forwarding_environment
            or self._forwarding_discovery is not None
            or self._forwarding_supervisor is not None
            or self._forwarding_start_attempted
        ):
            return
        self._forwarding_start_attempted = True
        self._ensure_tailcat_machine_binding()
        self._warn_rejected_exposures()
        if slot_transport() == "unix":
            # Every up request this supervisor is about to send names one
            # unix socket path per slot under the home, and a fresh home
            # ships without that directory -- the sidecar's listen-time
            # MkdirAll is the second line of defense, so the daemon owns
            # its side of the wire here: idempotent, unix-only, before
            # any slots-carrying up can leave the process (e2e 2026-10-08).
            ensure_slots_dir(self.hyprial_home)
        supervisor = ForwardingSidecarSupervisor(
            {**os.environ, **self._forwarding_environment},
            event_log=lambda level, event, **fields: self._log(
                level, "zenoh", event, **fields
            ),
            scheduler=self._maintenance_scheduler,
            own_io=True,
            desired_exposures=self._exposure_store.list,
            slots=lambda: slot_store_for_app(self).endpoints(),
            slot_transport=slot_transport(),
            on_slots_reported=lambda ids: record_tcp_slot_ports(self, ids),
            up_slots=lambda: slot_store_for_app(self).specs(),
        )
        self._forwarding_supervisor = supervisor
        supervisor.ensure_started()

    def _ensure_tailcat_machine_binding(self) -> None:
        """Adopt a legacy key or rotate a copied-home identity before launch."""

        from hyprial.daemon.impl.network.tailcat import BINARY_ENV, ensure_device_key
        from hyprial.identity import consume_rotation_event, read_device_record

        record = read_device_record(self.hyprial_home)
        if record is None:
            return
        environ = {**os.environ, **self._forwarding_environment}
        if BINARY_ENV not in environ:
            try:
                command = json.loads(environ.get(FORWARDING_COMMAND_ENV, "[]"))
            except (TypeError, ValueError):
                command = []
            if isinstance(command, list) and command and isinstance(command[0], str):
                environ[BINARY_ENV] = command[0]
        ensure_device_key(
            self.hyprial_home,
            owner=record.owner,
            device_id=record.device_id,
            environ=environ,
        )
        event = consume_rotation_event(self.hyprial_home)
        if event is not None:
            self._log(
                "warn",
                "zenoh",
                "tailcat.identity-reissued",
                **event,
            )

    def _warn_rejected_exposures(self) -> None:
        """Say out loud which persisted exposures will not be served.

        An entry written before a validation rule existed (v2 on a tcp
        target) is skipped rather than fatal; without this line the operator
        would only notice the port had gone quiet.
        """

        try:
            rejected = self._exposure_store.rejected()
        except ForwardingSidecarError:
            # An unreadable file is reported by the forwarding start itself.
            return
        for item in rejected:
            exposure = item.get("exposure")
            self._log(
                "warn",
                "network",
                "network.exposure.skipped",
                port=exposure.get("port") if isinstance(exposure, dict) else None,
                reason=str(item.get("reason", ""))[:500],
            )

    def _forwarding_backend(self) -> ForwardingEndpoints | None:
        """The live forwarding backend, supervisor-owned or test-injected."""

        if self._forwarding_supervisor is not None:
            return self._forwarding_supervisor.endpoints()
        return self._forwarding_discovery

    def _slot_mode(self) -> bool:
        """Whether forwarding dials the fixed slot pool on this daemon.

        A test-injected slot backend answers for itself; otherwise slot
        mode is "forwarding is configured" -- the fixed pool is the only
        way forwarding dials (no rollback valve).
        """

        backend = self._forwarding_backend()
        if backend is not None:
            return bool(getattr(backend, "slot_mode", False))
        return bool(self._forwarding_environment)

    def _forwarding_diagnostic_counters(self) -> dict[str, int]:
        snapshot = getattr(
            getattr(self, "_transport", None), "link_diagnostic_counters", None
        )
        if snapshot is None:
            return {"map_peer_remap": 0}
        try:
            counters = snapshot()
        except Exception:  # noqa: BLE001 - ps must stay answerable
            return {"map_peer_remap": 0}
        value = counters.get("map_peer_remap", 0)
        return {"map_peer_remap": value if isinstance(value, int) else 0}

    def _forwarding_status_json(self) -> dict[str, object]:
        """The forwarding half's verdict for status/ps -- never silently absent.

        A forwarding daemon used to have no state surface at all: a dead
        sidecar looked exactly like a healthy one with no peers. The state
        words are the supervisor's (running / degraded / restarting /
        failed); "off" is the not-configured answer, which is a fact about
        this node rather than a missing field.

        ``endpoints`` vs ``dialed`` answers the question the process state
        cannot: Zenoh fixes its connect set when the session opens, so a
        relaunch that changed the local-port set leaves the session dialing
        ports the new child no longer owns until ``_redial_forwarding``
        rebuilds it. Reporting ``running`` alone would masquerade as
        connected; the gap is reported instead (review finding D). The name
        ``restartRequired`` is published and kept: it is True while the
        session does not dial what the sidecar reports -- no session yet, a
        failed rebuild, or a sidecar that currently reports fewer peers.
        """

        policy = self._forwarding_policy.to_json()
        exposure_store = getattr(self, "_exposure_store", None)
        exposures = exposure_store.list() if exposure_store is not None else []
        supervisor = self._forwarding_supervisor
        if supervisor is not None:
            status: dict[str, object] = {
                "state": supervisor.state,
                "failures": supervisor.failures,
                "pid": supervisor.current_pid,
            }
        elif self._forwarding_discovery is not None:
            status = {"state": "running", "failures": 0, "pid": None}
        else:
            off: dict[str, object] = {
                "state": "off",
                "failures": 0,
                "pid": None,
                "policy": policy,
                "counters": self._forwarding_diagnostic_counters(),
            }
            if self._forwarding_unavailable is not None:
                off["unavailable"] = self._forwarding_unavailable
            from hyprial.daemon.impl.forwarding.membership.projection import readiness_for_app
            off['readiness'] = readiness_for_app(self)
            off["exposures"] = exposures
            off["connectionTelemetry"] = {
                "state": "unavailable",
                "reason": "capability-absent",
            }
            return off
        from hyprial.daemon.impl.forwarding.membership.projection import readiness_for_app
        status['readiness'] = readiness_for_app(self)
        status["policy"] = policy
        status["counters"] = self._forwarding_diagnostic_counters()
        effective = self._forwarding_effective
        # The session-open capture; before the first discovery pass it is
        # whatever the first pass returned -- including (), which is exactly
        # the "recovered later, dialed never" gap that must stay visible.
        dialed = (
            self._forwarding_dialed
            if self._forwarding_dialed is not None
            else effective
        )
        status["endpoints"] = list(effective)
        status["dialed"] = list(dialed)
        status["restartRequired"] = effective != dialed
        status["exposures"] = exposures
        # Who the sidecar reports and whether each is in the session's
        # dialed set, plus the last poll's outcome: the questions "can we see
        # that node?" and "did we dial it?" had no answer before (plan §C).
        backend = self._forwarding_backend()
        poll = getattr(backend, "last_poll", None) if backend is not None else None
        if isinstance(poll, dict):
            peers = poll.get("peers")
            peer_endpoints = peers if isinstance(peers, dict) else {}
            status["lastPoll"] = {
                "atMs": poll.get("atMs"),
                "ok": poll.get("ok"),
                "error": poll.get("error"),
                "peersReported": poll.get("peersReported"),
                "peerCount": len(peer_endpoints),
                "derpMapURL": poll.get("derpMapURL"),
                "derpMapSource": poll.get("derpMapSource"),
            }
            slots_by_peer: dict[str, int] = {}
            if getattr(backend, "slot_mode", False):
                slots_by_peer = slot_store_for_app(self).assignments
            telemetry = poll.get("connectionTelemetry")
            status["connectionTelemetry"] = (
                telemetry
                if isinstance(telemetry, dict)
                else {"state": "unavailable", "reason": "capability-absent"}
            )
            for key in ("derpMapURL", "derpMapSource"):
                if isinstance(poll.get(key), str):
                    status[key] = poll[key]
            status["peers"] = [
                {
                    "peer": peer,
                    "endpoint": endpoint,
                    "dialed": endpoint in dialed,
                    **(
                        {"slot": slots_by_peer[peer]}
                        if peer in slots_by_peer
                        else {}
                    ),
                }
                for peer, endpoint in sorted(peer_endpoints.items())
            ]
        if backend is not None and getattr(backend, "slot_mode", False):
            # The slot → peer projection the design asks status to answer:
            # which slot dials whom, how big the pool is, how much is free.
            store = slot_store_for_app(self)
            assignments = store.assignments
            # The live backend knows the locator per index on every
            # transport (tcp ports are per-incarnation); the store's
            # static set only covers unix.
            locators = getattr(backend, "slot_locators", None)
            endpoints_by_slot = (
                locators() if callable(locators)
                else dict(enumerate(store.endpoints()))
            )
            status["slots"] = {
                "poolSize": store.pool_size,
                "free": store.pool_size - len(assignments),
                "assigned": [
                    {
                        "slot": slot,
                        "peer": peer,
                        "endpoint": endpoints_by_slot.get(slot),
                    }
                    for peer, slot in sorted(
                        assignments.items(), key=lambda item: item[1]
                    )
                ],
            }
        return status

    def _reconcile_forwarding_endpoints(self) -> None:
        self._publish_org_directory_if_address_changed()
        self._process_org_leave_requests_throttled()
        backend = self._forwarding_backend()
        if backend is None:
            return
        if getattr(backend, "slot_mode", False):
            # Slot binding runs BEFORE the endpoint read: membership
            # changes only rebind slots (never touching the set), and a
            # pool growth must be visible in this very read so the one
            # remaining rebuild below dials it. Only the leased sidecar
            # projection selects candidates; this tick retries those candidates.
            reconcile_slot_bindings(self, reason="tick")
        previous = self._forwarding_effective
        current = backend.list_reachable_endpoints()
        self._forwarding_effective = current
        poll = getattr(backend, "last_poll", None)
        peers = poll.get("peers") if isinstance(poll, dict) else None
        set_endpoint_peers = getattr(self._transport, "set_endpoint_peers", None)
        if isinstance(peers, dict) and callable(set_endpoint_peers):
            set_endpoint_peers(peers)
        self._redial_forwarding(current)
        if current == previous:
            return
        # Reported after the redial attempt, so ``restartRequired`` is the
        # outcome: False once the session dials the new set, True while it
        # still cannot (no session yet, or the rebuild failed).
        self._log(
            "warn",
            "zenoh",
            "zenoh.forwarding.changed",
            previous=list(previous),
            current=list(current),
            restartRequired=current != self._forwarding_dialed,
        )

    def _process_org_leave_requests_throttled(self) -> None:
        """§4.3 owner/admin reconcile: fulfil pending org leave requests.

        Runs at most every ``_ORG_LEAVE_INTERVAL_SECONDS`` (the maintenance
        tick is ~1s and this reads OrgFS); best effort, never raises.
        """

        now = time.monotonic()
        last = getattr(self, "_org_leave_requests_checked_at", None)
        if last is not None and now - last < _ORG_LEAVE_INTERVAL_SECONDS:
            return
        self._org_leave_requests_checked_at = now
        from hyprial.daemon.impl.org.network import process_leave_requests_for_app

        process_leave_requests_for_app(self)

    def _publish_org_directory_if_address_changed(self) -> None:
        """Refresh directory address and per-server client-key projections.

        The sidecar writes the Tailcat address file once it is up; until then
        there is nothing publishable, and after a rotation the row must be
        rewritten. The address digest triggers immediate publication; a
        bounded cadence also discovers newly published peer server keys.
        Best effort: a failure is retried on a later maintenance tick.
        """

        try:
            from hyprial.identity import address_path

            raw = address_path(self.hyprial_home).read_text("utf-8")
        except OSError:
            return
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now = time.monotonic()
        address_changed = digest != getattr(
            self, "_org_directory_address_digest", None
        )
        last = getattr(self, "_org_directory_keys_checked_at", None)
        if (
            not address_changed
            and last is not None
            and now - last < _ORG_KEY_PUBLICATION_INTERVAL_SECONDS
        ):
            return
        from hyprial.daemon.impl.org.network import publish_self_for_app

        outcome = publish_self_for_app(self)
        self._org_directory_keys_checked_at = now
        if not outcome.get("skipped") and not outcome.get("errors"):
            self._org_directory_address_digest = digest

    def _redial_forwarding(self, current: tuple[str, ...]) -> None:
        """Rebuild the session to dial ``current`` when it names a new endpoint.

        Zenoh fixes its connect set when the session opens, so a peer the
        sidecar mapped later -- or a relaunch that re-mapped every port -- was
        never dialed until a daemon restart (plan §C, step 3). Only a NEW
        endpoint triggers a rebuild: a pure removal (the sidecar died and
        reports nothing) leaves Zenoh retrying a dead port, which is harmless,
        instead of costing two interruptions per sidecar blip. The configured
        and host-tailnet endpoints are recomposed in startup order, so a
        redial never drops them. Each distinct set is attempted once; a failed
        rebuild restores the old session and stays visible as
        ``restartRequired`` until the set changes again.
        """

        dialed = self._forwarding_dialed or ()
        if not set(current) - set(dialed):
            return
        transport = self._transport
        if transport is None or current == self._forwarding_redial_attempted:
            return
        self._forwarding_redial_attempted = current
        connect = merge_endpoints(
            self._connect_configured, current, self._connect_discovered
        )
        started_at = time.monotonic()
        try:
            transport.reconfigure_connect(connect)
        except Exception as error:  # noqa: BLE001 - the old session is restored
            self._log(
                "warn",
                "zenoh",
                "zenoh.forwarding.redial_failed",
                dialed=list(dialed),
                wanted=list(current),
                detail=str(error)[:300],
            )
            return
        self._forwarding_dialed = current
        self.zenoh_connect = connect
        self._log(
            "info",
            "zenoh",
            "zenoh.forwarding.redialed",
            previous=list(dialed),
            dialed=list(current),
            connect=len(connect),
            durationMs=int((time.monotonic() - started_at) * 1000),
        )

