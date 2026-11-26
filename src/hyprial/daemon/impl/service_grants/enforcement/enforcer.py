# Decision notes (service grant S4-py, 2026-11-26, coordinator ruling on the
# design-mismatch evidence):
# - One object owns the admission sets of this device's user exposures. It
#   derives them, compares with what it last pushed, pushes what changed, and
#   remembers only that. The sidecar holds the gate; nothing is persisted.
# - The roster is read once per derivation, with a bound, because
#   membership-status without a tailnet id returns every tailnet's rows and
#   each org picks its own effective tailnet out of that one observation.
# - A derivation never raises into the maintenance path. A roster that cannot
#   be read, or answers nothing, derives an empty key set: the port tightens
#   until a later tick can read it. That is reported per grant, without keys.
# - Over the sidecar's key limit nothing is pushed for that port. The last
#   set stays in force rather than being replaced by a wider or empty one,
#   because a half pushed set would admit the wrong devices.
# - Unexpose revokes this device's grants for the port through S3's
#   revoke_all_for_port. A failure there is logged once and swallowed: the
#   caller is already past the point where the port should stay exposed.
# - Taking a port off the sidecar and revoking its grants runs under the same
#   lock as a derivation. A derivation lists the exposures once and spends
#   its roster budget before it pushes, so an unexpose inside that gap would
#   be undone by a push from the stale list, and the port would stay exposed
#   with nobody left to unexpose it.
# - What was last pushed belongs to one sidecar incarnation. A relaunched
#   sidecar replays every exposure with nobody admitted, so when the live
#   backend is another object than the one the sets were pushed to, that
#   memory is dropped and an equal set is pushed again instead of skipped.
# - The inputs (directory store, facade, home, owner, device id) are asked for
#   at each derivation, never captured at construction, so a relogin or a
#   replaced OrgFS runtime is seen by the next tick. That is the same rule the
#   service grant IPC handler follows when it builds its service per call.
# - A caller that changed the inputs marks them stale and returns at once;
#   the derivation runs on the cadence that owns it. The mark is cleared only
#   by a derivation that ran to its push, so a derivation that could not run
#   (no sidecar key yet, a failing read) leaves the change owed to the next
#   tick instead of waiting out the whole interval.
# - The key limit is the forwarding policy's: the sidecar's parser and this
#   object must agree on one number, and there is one place that states it.
"""Pushing each exposure's derived admission set to the sidecar."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hyprial.daemon.impl.forwarding.policy import MAX_ADMISSION_KEYS, ForwardingSidecarError
from hyprial.daemon.impl.forwarding.store import ExposureStore
from hyprial.daemon.impl.org.network.binding import resolve_org_directory_space
from hyprial.daemon.impl.org.network.create.sub_org import effective_tailnet_id
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.daemon.impl.service_grants import ServiceGrantService, StoredServiceGrant

from .derive import derive_admission
from .models import AdmissionSet, EnforcementInputs, NOBODY, UnresolvedGrant
from .reader import orgs, own_grants

MAX_KEYS = MAX_ADMISSION_KEYS
COMPONENT = "service"
KEY_UNAVAILABLE = "service.grant.key_unavailable"
LIMIT_EVENT = "service.grant.admission_limit"
UNEXPOSE_FAILED = "service.grant.unexpose_revoke_failed"
DERIVATION_FAILED = "service.grant.admission_unavailable"
ROSTER_UNREADABLE = "service.grant.roster_unreadable"
PUSH_REJECTED = "network.exposure.admission_rejected"
DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_MEMBERSHIP_TIMEOUT = 5.0


def _error_code(error: BaseException) -> str:
    """The error's wire code when it has one, else its type name."""

    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__


class ServiceAdmissionEnforcer:
    """Derives and pushes the admission set of every user exposure."""

    def __init__(
        self,
        *,
        supervisor: Any,
        exposure_store: ExposureStore,
        grants_service: Callable[[], ServiceGrantService],
        inputs: Callable[[], EnforcementInputs] | None = None,
        store: OrgFsDirectoryStore | None = None,
        fs: OrgFs | None = None,
        home: Any = None,
        owner: str | None = None,
        device_id: str | None = None,
        roster_reader: Callable[[], Mapping[str, Any] | None] | None = None,
        revoke_all_for_port: Callable[[int], dict[str, Any]] | None = None,
        grants_reader: Callable[[], Mapping[str, Sequence[StoredServiceGrant]]] | None = None,
        now_ms: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
        logger: Callable[..., None] | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        membership_timeout: float = DEFAULT_MEMBERSHIP_TIMEOUT,
    ) -> None:
        if inputs is None:
            fixed = EnforcementInputs(
                store=store, fs=fs, home=home, owner=owner or "", device_id=device_id or ""
            )

            def inputs() -> EnforcementInputs:
                return fixed

        self._supervisor = supervisor
        self._exposure_store = exposure_store
        self._inputs = inputs
        self._grants_service = grants_service
        self._roster_reader = roster_reader
        self._revoke_all_for_port = revoke_all_for_port
        self._grants_reader = grants_reader
        self._now_ms = now_ms
        self._clock = clock
        self._logger = logger
        self._interval = interval_seconds
        self._membership_timeout = membership_timeout
        self._lock = threading.Lock()
        self._pushed: dict[int, AdmissionSet] = {}
        self._backend: Any = object()
        self._last_derivation = float("-inf")
        self._dirty = True

    def invalidate(self) -> None:
        """Mark the sets stale after a change that the caller knows about.

        Never derives and never raises, so it is safe on an IPC thread, on
        the sidecar's relaunch path and inside a lock the derivation takes:
        it takes only this object's own lock, for one assignment.
        """

        with self._lock:
            self._dirty = True

    def tick(self) -> None:
        """One cadence tick: derive now when stale, else at most once per interval."""

        with self._lock:
            if self._dirty or self._clock() - self._last_derivation >= self._interval:
                self._derive_and_push()

    def last_set(self, port: int) -> str | None:
        """The mode last pushed for ``port``: "allow", "keys", or nothing yet."""

        with self._lock:
            pushed = self._pushed.get(port)
        return None if pushed is None else pushed.admission

    def apply_now(self) -> None:
        """Derive and push now; never raises."""

        with self._lock:
            self._derive_and_push()

    def reconcile(self) -> None:
        """The maintenance tick's entry: ``apply_now`` at most once per interval."""

        with self._lock:
            if self._clock() - self._last_derivation < self._interval:
                return
            self._derive_and_push()

    def exclusive(self) -> Any:
        """The lock every derivation holds, for a caller that removes an exposure.

        ``network.unexpose`` takes the port off the sidecar and out of the
        exposure store, then revokes the port's grants. Those steps have to
        happen with no derivation in flight: a derivation lists the exposures
        once and then spends up to its roster budget before it pushes, so an
        unexpose that slips into that gap would be undone by a push from the
        stale list, and nothing would ever unexpose the port again. Running
        the three steps under this lock orders them either wholly before a
        derivation, which then no longer sees the port, or wholly after one,
        whose push the unexpose then removes.
        """

        return self._lock

    def forget_port(self, port: int) -> dict[str, Any]:
        """Revoke this device's grants for ``port`` and forget what was pushed.

        To be called under :meth:`exclusive`, after the port is off the
        sidecar and out of the exposure store. Returns
        ``{"port", "revoked", "failed"}`` in the shape of S3's
        revoke_all_for_port. A failure is one ``failed`` entry and one log
        event; it does not raise.
        """

        self._pushed.pop(port, None)
        try:
            result = self._revoke(port)
        except Exception as error:  # noqa: BLE001
            code = _error_code(error)
            self._log("warn", UNEXPOSE_FAILED, port=port, org="*", code=code)
            return {"port": port, "revoked": [], "failed": [{"org": "*", "code": code}]}
        for item in result.get("failed", []):
            self._log(
                "warn",
                UNEXPOSE_FAILED,
                port=port,
                org=item.get("org"),
                code=item.get("code"),
            )
        return result

    def _revoke(self, port: int) -> dict[str, Any]:
        if self._revoke_all_for_port is not None:
            return self._revoke_all_for_port(port)
        return self._grants_service().revoke_all_for_port(port)

    def _derive_and_push(self) -> None:
        self._last_derivation = self._clock()
        try:
            backend = self._supervisor.endpoints()
            if backend is not self._backend:
                self._backend = backend
                self._pushed.clear()
            server_public = self._server_public(backend)
            if server_public is None:
                return
            exposures = list(self._exposure_store.list())
            roster = self._read_roster()
            inputs = self._inputs()
            grants = self._read_grants(inputs)
            tailnet_ids = self._tailnet_ids(inputs, grants)
            projection = derive_admission(
                grants,
                {org: roster for org in grants},
                tailnet_ids,
                owner=inputs.owner,
                device_id=inputs.device_id,
                server_public=server_public,
                now_ms=self._now_ms(),
            )
        except Exception as error:  # noqa: BLE001
            self._log("warn", DERIVATION_FAILED, errorType=type(error).__name__)
            return
        self._push(projection, exposures)
        self._dirty = False

    def _push(self, projection: Mapping[int, Any], exposures: Sequence[Mapping[str, object]]) -> None:
        for exposure in exposures:
            port = exposure.get("port")
            if not isinstance(port, int) or isinstance(port, bool):
                continue
            result = projection.get(port)
            if result is None:
                self._push_one(port, exposure, NOBODY)
                continue
            self._report(port, result.unresolved)
            if result.admission.admission == "keys" and len(result.admission.keys) > MAX_KEYS:
                self._log(
                    "warn", LIMIT_EVENT, port=port, count=len(result.admission.keys), limit=MAX_KEYS
                )
                continue
            self._push_one(port, exposure, result.admission)

    def _push_one(self, port: int, exposure: Mapping[str, object], admission: AdmissionSet) -> None:
        if self._pushed.get(port) == admission:
            return
        try:
            self._supervisor.expose(
                {key: exposure[key] for key in ("port", "target", "proxyProtocol")},
                admission=admission.admission,
                keys=list(admission.keys),
            )
        except ForwardingSidecarError as error:
            self._log("error", PUSH_REJECTED, port=port, code=error.code)
            return
        self._pushed[port] = admission

    def _report(self, port: int, unresolved: Sequence[UnresolvedGrant]) -> None:
        for item in unresolved:
            self._log(
                "warn",
                KEY_UNAVAILABLE,
                org=item.org,
                port=port,
                grantId=item.grant_id,
                recipient=dict(item.recipient),
                reason=item.reason,
            )

    @staticmethod
    def _server_public(backend: Any) -> str | None:
        controller = getattr(backend, "controller", None)
        server_public = getattr(controller, "server_public", None)
        return server_public if isinstance(server_public, str) and server_public else None

    def _read_roster(self) -> Mapping[str, Any] | None:
        """One bounded membership-status read; ``None`` when it cannot be read."""

        try:
            if self._roster_reader is not None:
                return self._roster_reader()
            return self._supervisor.membership_request(
                "membership-status", {}, timeout=self._membership_timeout
            )
        except Exception as error:  # noqa: BLE001
            self._log("warn", ROSTER_UNREADABLE, code=_error_code(error))
            return None

    def _read_grants(
        self, inputs: EnforcementInputs
    ) -> dict[str, tuple[StoredServiceGrant, ...]]:
        if self._grants_reader is not None:
            return dict(self._grants_reader())
        return own_grants(inputs.store, orgs(inputs.store), owner=inputs.owner)

    def _tailnet_ids(
        self, inputs: EnforcementInputs, grants: Mapping[str, Sequence[StoredServiceGrant]]
    ) -> dict[str, str]:
        """Each org's effective tailnet id: a sub-org inherits its parent's tailnet.

        An org whose id cannot be resolved is left out, which derives it with
        no roster: its grants contribute nothing until it can be resolved.
        """

        ids: dict[str, str] = {}
        for org in grants:
            try:
                space, _reason = resolve_org_directory_space(inputs.fs, org)
                if space is not None:
                    ids[org] = effective_tailnet_id(
                        inputs.fs, inputs.home, org, space_id=space.space_id
                    )
            except Exception:  # noqa: BLE001
                continue
        return ids

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, COMPONENT, event, **fields)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MEMBERSHIP_TIMEOUT",
    "KEY_UNAVAILABLE",
    "LIMIT_EVENT",
    "MAX_KEYS",
    "UNEXPOSE_FAILED",
    "ServiceAdmissionEnforcer",
]
