"""GUI onboarding runtime host over one explicit home (A4).

Decision: gui-m2-onboarding-a4-admission-20261106.

The GUI supervisor owns a local request entry (``/daemon/request/<method>``)
whose backend is this host.  The host admits exactly one of two phases from
durable state alone:

* ``serving`` -- an authenticated home (the public identity snapshot has an
  owner) is served by the A3 ``ServingOnboardingGateway`` over the one
  operation journal.  The owner is read from that snapshot; it is never taken
  from a request body, bearer, ``phase``/``home``/``view`` field or a
  constructor override, and no principal is synthesized.
* ``bootstrap`` -- an empty home is forwarded to the daemon that answers in
  bootstrap, so ``onboarding.status``/``auth.login.begin`` reach the real B0-B6
  runtime.  No owner exists in this phase and none is invented.

``METHODS`` is the same four A4 methods the legacy bootstrap host serves.
``auth.relogin`` stays serving-only (A6).  ``auth.logout`` also serves an
authenticated bootstrap before initialization: the daemon runs it and the
transport is rotated before another login (C1b), so it is advertised whenever
the bootstrap gateway can act on it.

Decision: L1 (owner 2026-11-21).  ``ACCOUNT_METHODS`` is only the catalog
projection: those names are callable while an owner resolves and
``ACCOUNT_REQUIRED`` otherwise.  ``auth.logout`` is in it and is never executed
here: the daemon owns the whole logout transaction and the host forwards the
call (``hyprial logout`` sends the same one request).

Decision: gui-bootstrap-device-ensure-reach-20261110.  The settings
convergence admits exactly one bootstrap-phase write on this host:
``onboarding.device.ensure`` is projected callable from the first catalog
and forwarded like the frozen four.  The GUI session pins the catalog at
its one-time handshake, which happens before the login commit, so a
commit-time flip could never reach the pinned session; the gateway itself
still refuses the write until the worker commit (the declared
IDENTITY_UNBOUND), so admitting the method early widens reachability
without changing any outcome.  ``onboarding.initialize`` requires explicit
the daemon's foreground lifecycle, and serving keeps both bootstrap-only methods
not callable (BOOTSTRAP_ONLY per the published method contract).

Decision: daemon-first-z6a-bootstrap-forwarding-20261117.  The ping of the
home's own daemon socket is the phase authority whenever a daemon answers
(D1): a daemon in ``bootstrap`` with no owner is *forwarded to*, even when the
identity snapshot already has an owner (a login committed but initialize has
not run).  Such a forwarding host holds no lock, writes no ``bootstrap.json``,
opens no journal and never promotes; it reports the daemon's own ping and the
daemon's catalog re-projected with the GUI policy (see
daemon-first-z6c-phase-states-20261117 for the states).  ``attach_only`` makes
the constructor keep raising ``DaemonOwnershipBusy`` while no daemon answers
(the signal ``reopen`` waits on, without a deadline); the host keeps no record
of the daemon process, because nothing decides on whether it is alive (the
daemon-liveness answer existed only for the reopen deadline, deleted in Z-6c).
A forward is refused (``DaemonUnavailableError``, sticky) once the daemon's ping shows a
phase or epoch other than the one this host attached in, so a bearer of the
old bootstrap never reaches the promoted daemon through this host; the check
is one ping per forwarded call, not an atomic fence.

Decision: daemon-first-z6b-no-hosted-bootstrap-20261117.  This process never
takes ``daemon.lock`` or the identity-transaction lock, writes no
``bootstrap.json``, opens no onboarding journal, never promotes and never builds
a ``DaemonApplication``: when nothing answers on an ownerless home the
constructor raises ``DaemonUnavailableError`` and the child refuses to start.
The daemon is the only owner of the home (R1); initialize, logout and the
promotion that follows them happen in the daemon.

Decision: daemon-first-z6c-phase-states-20261117.  Phase unavailability is
``not-ready``, never ``disabled``: a method the daemon exposes over HTTP but the
current phase cannot call is ``not-ready`` with reason ``ACCOUNT_REQUIRED``
(bootstrap) or ``BOOTSTRAP_ONLY`` (serving, the two ``DISABLED_METHODS``), and
``disabled`` stays for methods the daemon does not expose.  The HTTP route set
and both contract digests therefore do not depend on the phase, so one
listener, one pinned browser catalog and one set of generated routes serve every
phase.

Decision: daemon-first-z6c-no-local-projection-20261117.  A serving home has no
projection of its own either: the ping and the catalog are the daemon's, and a
daemon that does not answer is ``DaemonUnavailableError`` for the constructor and
for every call, never a stale view and never a signal that the phase changed.

"""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any

from hyprial.identity import read_identity_snapshot
from hyprial.kernel import (
    DaemonRequestError,
    RequestPortError,
    daemon_request_once,
    ipc_errors,
)

from hyprial.daemon.impl.configuration.ownership import DaemonOwnershipBusy

from .account_lifecycle import startup_decision
from .errors import OnboardingGatewayError

#: The four methods a GUI runtime host serves in both phases (A4 freeze).
METHODS = frozenset(
    {
        "onboarding.status",
        "auth.login.begin",
        "auth.operation.get",
        "auth.operation.cancel",
    }
)
SERVING_ONLY_METHODS = frozenset({"auth.logout", "auth.relogin"})
DISABLED_METHODS = frozenset({"onboarding.device.ensure", "onboarding.initialize"})
ACCOUNT_METHODS = frozenset(
    {
        "onboarding.status",
        "auth.login.begin",
        "auth.operation.get",
        "auth.operation.cancel",
        "auth.relogin",
        "auth.logout",
    }
)

_GUI_CALLERS = frozenset({"local-gui"})
DAEMON_WATCH_SECONDS = 0.5
PROBE_TIMEOUT_SECONDS = 2
_PROBE_FAILURES = (DaemonRequestError, RequestPortError, OSError, TimeoutError)


def _is_bootstrap_ping(ping: dict[str, Any]) -> bool:
    epoch = ping.get("epoch")
    return (
        ping.get("phase") == "bootstrap"
        and ping.get("owner") is None
        and isinstance(epoch, str)
        and bool(epoch)
    )


class GuiRuntimeHost:
    """One local GUI request entry over one home, serving or bootstrap."""

    def __init__(
        self,
        home: Path,
        *,
        identity_reader=read_identity_snapshot,
        state_dir: Path | None = None,
        runtime_dir: Path | None = None,
        actor: str | None = None,
        attach_only: bool = False,
    ) -> None:
        requested = Path(home)
        if requested.is_symlink():
            raise OnboardingGatewayError(
                ipc_errors.DAEMON_NOT_READY, "the GUI home is not a real directory"
            )
        # Allen 2026-11-07 (ruling c): the GUI layer does not repeat the OS
        # home-custody gate.  The secret store still checks home privacy when it
        # connects, so dropping this duplicate does not remove that protection.
        self.home = requested.resolve()
        # The admission may put state beside the home (HARNESS_STATE_DIR); the
        # caller passes that explicitly so this host never opens a second
        # journal at home/state while the real one lives elsewhere.
        self.state_dir = self.home / "state" if state_dir is None else Path(state_dir)
        self.socket_path = self.state_dir / "daemon.sock"
        self._lock = threading.RLock()
        self._closed = False
        # Retained for call-site compatibility: the request-http config still
        # names the supervisor runtime directory, and this host reads nothing
        # from it — the supervisor logout-custody file has no reader anymore
        # since logout became daemon-owned.
        self._runtime_dir = None if runtime_dir is None else Path(runtime_dir)
        # The canonical actor admission derived from the verified owner and node.
        # It is only published in the serving catalog principal; no request input
        # can supply or override it.
        self._actor = actor
        self._identity_reader = identity_reader
        self.forwarding = False
        self.daemon_owner: str | None = None
        self._watched_at = time.monotonic()
        self._stale = False
        decision = startup_decision(home=self.home, identity_reader=identity_reader)
        self.phase = decision.mode
        self.owner = decision.owner
        ping = self._probe_daemon()
        if ping is None and attach_only:
            raise DaemonOwnershipBusy(
                "no daemon answers on the previous daemon's socket yet"
            )
        if ping is None:
            raise ipc_errors.DaemonUnavailableError(
                "no daemon answers on this home's socket"
            )
        self._record_daemon(ping)
        epoch = ping.get("epoch")
        self.epoch = epoch if isinstance(epoch, str) else ""
        if _is_bootstrap_ping(ping):
            self.phase = "bootstrap"
            self.owner = None
            self.forwarding = True
            return
        if decision.bootstrap_required:
            raise DaemonOwnershipBusy(
                "the daemon still reports an account the home no longer has"
            )

    def _probe_daemon(self) -> dict[str, Any] | None:
        try:
            ping = daemon_request_once(
                "ping",
                {},
                socket_path=self.socket_path,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except _PROBE_FAILURES:
            return None
        return ping if isinstance(ping, dict) else None

    def _record_daemon(self, ping: dict[str, Any]) -> None:
        owner = ping.get("owner")
        self.daemon_owner = owner if isinstance(owner, str) and owner else None

    def _require_attached(self) -> None:
        if not self._stale:
            ping = self._probe_daemon()
            if ping is None or (
                _is_bootstrap_ping(ping) and ping.get("epoch") == self.epoch
            ):
                return
            self._stale = True
        raise ipc_errors.DaemonUnavailableError(
            "the bootstrap daemon changed phase; the GUI entry is being reopened"
        )

    def daemon_changed(self) -> bool:
        """Whether the daemon this host attached to moved to another phase or owner.

        A bootstrap host sees a phase change or a new bootstrap epoch; a serving
        host sees a bootstrap phase or another owner (a serving epoch under the
        same owner is not a change).  At most one ping per ``DAEMON_WATCH_SECONDS``.  A silent daemon is not a
        change: the child keeps serving and its requests answer
        ``DAEMON_UNAVAILABLE``; it never starts hosting on its own.
        """

        with self._lock:
            if self._closed:
                return False
            if self._stale:
                return True
            now = time.monotonic()
            if now - self._watched_at < DAEMON_WATCH_SECONDS:
                return False
            self._watched_at = now
        ping = self._probe_daemon()
        if ping is None:
            return False
        if _is_bootstrap_ping(ping) != (self.phase == "bootstrap"):
            return True
        if self.forwarding:
            return ping.get("epoch") != self.epoch
        return ping.get("owner") != self.daemon_owner

    # -- request entry -----------------------------------------------------

    def __call__(self, method: str, params=None, *, socket_path: Path, timeout=None):
        with self._lock:
            if self._closed or Path(socket_path) != self.socket_path:
                raise ipc_errors.DaemonUnavailableError(
                    "the GUI host is unavailable"
                )
            if self._stale and not self.forwarding:
                raise ipc_errors.DaemonUnavailableError(
                    "the daemon changed phase; the GUI entry is rebinding"
                )
            if self.forwarding:
                if method == "daemon.catalog":
                    return self._bootstrap_catalog()
                if method != "ping":
                    self._require_attached()
                return daemon_request_once(
                    method,
                    params or {},
                    socket_path=self.socket_path,
                    timeout=timeout,
                )
            if method == "ping":
                return self._serving_ping()
            if method == "daemon.catalog":
                return self._catalog()
            # Everything, the account surface included, is the daemon's own: one
            # ordinary request over the local daemon socket as the local
            # operator, exactly the way the CLI calls it.  No local port, no
            # allowlist, no second dispatcher and no error re-mapping: a daemon
            # code reaches the caller unchanged.
            return daemon_request_once(
                method,
                params or {},
                socket_path=self.socket_path,
                timeout=timeout,
            )

    # -- projections -------------------------------------------------------

    def _current_owner(self) -> str | None:
        """Re-read durable ownership; an account-only method needs a live owner."""

        try:
            decision = startup_decision(
                home=self.home, identity_reader=self._identity_reader
            )
        except OnboardingGatewayError:
            return None
        return decision.owner

    def _serving_ping(self) -> dict[str, Any]:
        """The daemon's ping with only ``phase`` normalized to ``serving``.

        A serving daemon may answer ``reconciled`` while the request-http entry
        only accepts ``bootstrap``/``serving``.  Everything else (version,
        nodeId, owner, epoch) is the daemon's own answer, never re-derived here.
        A daemon that does not answer is ``DaemonUnavailableError``; this host
        has no projection of its own to put in its place.
        """

        try:
            ping = daemon_request_once(
                "ping", {}, socket_path=self.socket_path, timeout=10
            )
        except _PROBE_FAILURES as error:
            raise ipc_errors.DaemonUnavailableError(
                "the daemon is not reachable"
            ) from error
        if _is_bootstrap_ping(ping) or ping.get("owner") != self.daemon_owner:
            self._stale = True
            raise ipc_errors.DaemonUnavailableError(
                "the daemon changed phase; the GUI entry is rebinding"
            )
        normalized = dict(ping)
        normalized["phase"] = "serving"
        return normalized

    def _bootstrap_catalog(self) -> dict[str, Any]:
        """The daemon's bootstrap catalog under the GUI policy (D2)."""

        try:
            daemon = daemon_request_once(
                "daemon.catalog", {}, socket_path=self.socket_path, timeout=10
            )
        except (RequestPortError, OSError, TimeoutError) as error:
            raise ipc_errors.DaemonUnavailableError(
                "the bootstrap daemon catalog is unavailable"
            ) from error
        entries = daemon.get("methods") if isinstance(daemon, dict) else None
        if not isinstance(entries, list):
            raise ipc_errors.DaemonUnavailableError(
                "the bootstrap daemon returned no method catalog"
            )
        methods: list[dict[str, Any]] = []
        for raw in entries:
            entry = dict(raw)
            if entry.get("callable") is True:
                state, usable, reason = "available", True, None
            elif entry.get("httpExposed") is True:
                state, usable, reason = "not-ready", False, "ACCOUNT_REQUIRED"
            else:
                state, usable, reason = "disabled", False, None
            methods.append(
                {
                    **entry,
                    "declared": True,
                    "state": state,
                    "callable": usable,
                    "reason": reason,
                }
            )
        projection = dict(daemon)
        projection.pop("principal", None)
        projection["methods"] = methods
        return projection

    def _catalog(self) -> dict[str, Any]:
        owner = self._current_owner()
        try:
            daemon = daemon_request_once(
                "daemon.catalog", {}, socket_path=self.socket_path, timeout=10
            )
        except _PROBE_FAILURES as error:
            raise ipc_errors.DaemonUnavailableError(
                "the daemon catalog is unavailable"
            ) from error
        entries = daemon.get("methods") if isinstance(daemon, dict) else None
        if not isinstance(entries, list):
            raise ipc_errors.DaemonUnavailableError(
                "the daemon returned no method catalog"
            )
        methods: list[dict[str, Any]] = []
        for raw in entries:
            entry = dict(raw)
            name = entry["name"]
            if name in DISABLED_METHODS:
                state, callable_, reason = "not-ready", False, "BOOTSTRAP_ONLY"
            elif name in ACCOUNT_METHODS and owner is not None:
                state, callable_, reason = "available", True, None
            elif name in ACCOUNT_METHODS:
                state, callable_, reason = (
                    "not-ready",
                    False,
                    "ACCOUNT_REQUIRED",
                )
            else:
                state = entry.get("state", "disabled")
                callable_ = bool(entry.get("callable"))
                reason = entry.get("reason")
            methods.append(
                {
                    **entry,
                    "declared": True,
                    "state": state,
                    "callable": callable_,
                    "reason": reason,
                }
            )
        projection: dict[str, Any] = dict(daemon)
        projection["methods"] = methods
        projection["principal"] = {
            "kind": "local-gui",
            "owner": owner,
            "actor": self._actor,
        }
        return projection

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True


__all__ = [
    "DISABLED_METHODS",
    "GuiRuntimeHost",
    "METHODS",
    "SERVING_ONLY_METHODS",
]
