"""Forwarding sidecar operations: supervisor start, exposure warnings, redial and status."""

from __future__ import annotations

from __future__ import annotations
import hashlib
import os
import time
from typing import TYPE_CHECKING
from hyprial.daemon.impl.bootstrap.discovery  import (
    ForwardingEndpoints,
    merge_endpoints,
)
from hyprial.daemon.impl.forwarding  import (
    ForwardingSidecarError,
    ForwardingSidecarSupervisor,
)
if TYPE_CHECKING:
    pass




#: How often an owner/admin daemon fulfils org leave requests (§4.3).
_ORG_LEAVE_INTERVAL_SECONDS = 30.0

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
        self._warn_rejected_exposures()
        supervisor = ForwardingSidecarSupervisor(
            {**os.environ, **self._forwarding_environment},
            event_log=lambda level, event, **fields: self._log(
                level, "zenoh", event, **fields
            ),
            scheduler=self._maintenance_scheduler,
            own_io=True,
            desired_exposures=self._exposure_store.list,
            directory=self._tailcat_directory_peers,
        )
        self._forwarding_supervisor = supervisor
        supervisor.ensure_started()

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
            }
            if self._forwarding_unavailable is not None:
                off["unavailable"] = self._forwarding_unavailable
            off["exposures"] = exposures
            return off
        status["policy"] = policy
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
            }
            status["peers"] = [
                {"peer": peer, "endpoint": endpoint, "dialed": endpoint in dialed}
                for peer, endpoint in sorted(peer_endpoints.items())
            ]
        return status

    def _reconcile_forwarding_endpoints(self) -> None:
        self._publish_org_directory_if_address_changed()
        self._process_org_leave_requests_throttled()
        backend = self._forwarding_backend()
        if backend is None:
            return
        previous = self._forwarding_effective
        current = backend.list_reachable_endpoints()
        self._forwarding_effective = current
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
        """§4.3 publish_self: refresh the org directories when the address file changes.

        The sidecar writes the Tailcat address file once it is up; until then
        there is nothing publishable, and after a rotation the row must be
        rewritten.  Keyed on the file's content digest so the tick stays
        cheap and only real changes reach OrgFS.  Best effort: a failure
        logs and retries on the next tick.
        """

        try:
            from hyprial.identity import address_path

            raw = address_path(self.hyprial_home).read_text("utf-8")
        except OSError:
            return
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if digest == getattr(self, "_org_directory_address_digest", None):
            return
        from hyprial.daemon.impl.org.network import publish_self_for_app

        outcome = publish_self_for_app(self)
        if outcome.get("published"):
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

