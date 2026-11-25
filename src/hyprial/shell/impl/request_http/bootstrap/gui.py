"""GUI lifecycle of the ``local-gui`` principal: a client of the daemon.

The host forwards every request to the daemon that owns the home and observes
it.  A phase or owner change of that daemon is a principal change: the host
keeps its process, listener and port, closes its old daemon handle, attaches to
the daemon again and installs the new principal under a new bearer
(``RequestHttpServer.rebind``).  The owner is read from the daemon's
ping and cross-checked with the durable identity snapshot, never taken from a
request.  After a logout the host attaches to the daemon's own next bootstrap.

Decision: daemon-first-z6b-no-hosted-bootstrap-20261117.  The host never
promotes, never takes ``daemon.lock`` or the identity-transaction lock, writes
no ``state/bootstrap.json``, opens no onboarding journal and constructs no
``DaemonApplication``: initialize and logout happen in the daemon.

Decision: daemon-first-z6c-principal-change-in-place-20261117.  R3: a principal
change is the phase class changing (bootstrap versus anything else), the owner
changing, or the epoch changing while in bootstrap (one bootstrap epoch is one
login attempt); a serving epoch change under the same owner is not one.  R4: the
old host is closed before anything of the new binding exists, and a request
dispatches through the host it captured with its principal.  R5: a new
principal always has a new bearer (the transport generation of the retired config block is gone); the endpoint,
the pid and the listener never change for the life of the host.  R6: the owner
comes from the daemon ping and must equal the identity snapshot's owner; it never
comes from a request, a header or the launcher.  R2: a silent daemon is never a
change and never an end: while no daemon answers the host keeps its binding and
its requests answer ``DAEMON_UNAVAILABLE``; ``reopen`` waits without a deadline
(so a CLI logout with the GUI open leaves the host running) and the identity
snapshot is a cross-check, never a trigger.  Only a daemon whose owner disagrees
with the snapshot for longer than the drain budget is refused
(``PRINCIPAL_CONFIG_MISMATCH``).

Decision: daemon-first-z6c-rebind-retry-20261118.  A daemon that answered the
attach ping but cannot give a coherent catalog yet (``BACKEND_UNAVAILABLE``, or
``EPOCH_INCONSISTENT`` while its epoch is still moving) is a daemon that is
still coming up, so ``watch_principal`` closes that daemon handle and attaches
again every 0.1 s instead of ending the host; any other refusal ends it.  Meanwhile the
previous binding stays installed with its host closed: the old bearer is still
accepted (401 only comes after a successful rebind) and every request answers
503 ``DAEMON_UNAVAILABLE``, not dispatched, so a client sees the same answer as
for a daemon that is gone.  One ``rebind_waiting`` event (code and reason, never
a bearer) is logged to stderr each time the pair changes, not once per cycle.
The wait has no bound; a persistent ``EPOCH_INCONSISTENT`` means a daemon whose
ping and catalog never agree, which the launcher's own readiness checks and the
daemon's logs surface better than a silent host exit.

Decision: embedded-request-http-listen-false-20261119 (2822, B2).  The watcher is
``watch_principal(server, config, live, stop)``, usable on any started server.  It
never closes the live host: ``live`` (a ``LiveHost``) always holds it, and
the embedding host closes it after the server has drained (R4 on
every exit: stop, stop during ``reopen``, an exception).  Publishing a rebound
binding is the server's ``on_rebound`` hook, not a watcher argument.

Decision: host-serves-daemon-in-process-20261119 (2822, B3).  ``local_gui_config``
and ``open_gui_host`` are the public builders an embedding host uses instead of
importing the daemon: the first is the in-memory ``local-gui`` config (loopback
names only, no runtime directory: it points at the null device so any accidental
use of it fails instead of writing, no transport binding), the second opens the
admitted ``GuiRuntimeHost`` and returns it with the config completed by the
principal that host read from durable state and the daemon ping (R6), and maps
the two startup refusals to exit code 3 (``BACKEND_UNAVAILABLE``,
``DAEMON_OWNERSHIP_BUSY``, and the gateway's own code).  The standalone ``serve``
entry builds no GUI host: it refuses a ``local-gui`` config with
``CONFIG_INVALID``, exit 2.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

from hyprial.daemon import DaemonOwnershipBusy, GuiRuntimeHost, OnboardingGatewayError
from hyprial.identity import read_identity_snapshot
from hyprial.kernel import canonical_user_uri, ipc_errors

from ..config import DEFAULT_BUDGETS, RequestHttpConfig, _budgets
from ..server import StartupRefusal


def rebound_config(config, *, owner):
    return replace(config, owner=owner, actor=canonical_user_uri(owner) if owner else None)


def local_gui_config(*, home, state_dir, socket_path):
    """The ``local-gui`` config of a host that serves the dispatch in its own process."""
    return RequestHttpConfig(
        socket_path=Path(socket_path),
        owner=None,
        actor=None,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1", "localhost"),
        budgets=_budgets(DEFAULT_BUDGETS),
        runtime_dir=Path(os.devnull),
        source=Path(home),
        principal_kind="local-gui",
        home=Path(home),
        state_dir=Path(state_dir),
    )


def open_gui_host(config):
    """Open the admitted host of ``config`` and return it with the principal it serves.

    Raises ``StartupRefusal`` (exit code 3) when no daemon answers or the daemon
    is still stopping.
    """
    try:
        host = new_host(config)
    except ipc_errors.DaemonUnavailableError as error:
        raise StartupRefusal("BACKEND_UNAVAILABLE", "the daemon is not reachable", exit_code=3) from error
    except DaemonOwnershipBusy as error:
        raise StartupRefusal(error.code, str(error), exit_code=3) from error
    except OnboardingGatewayError as error:
        raise StartupRefusal(error.code, str(error), exit_code=3) from error
    owner = None if host.phase == "bootstrap" else host.owner
    return host, replace(config, owner=owner, actor=canonical_user_uri(owner) if owner else None)


def new_host(config, **extra):
    return GuiRuntimeHost(
        config.home,
        state_dir=config.state_dir,
        runtime_dir=config.runtime_dir,
        actor=config.actor,
        **extra,
    )


def reopen(config, stop, previous):
    """Attach to the daemon's bootstrap or serving phase; never host.

    Returns the next host and the config it is served under, or ``None`` when
    stopped.  While no daemon answers the wait has no deadline; an owner that
    disagrees with the identity snapshot is refused after the drain budget.
    """
    disagreement = None
    while not stop.is_set():
        hint = read_identity_snapshot(previous.home).owner
        candidate = rebound_config(config, owner=hint)
        try:
            host = new_host(candidate, attach_only=True)
        except DaemonOwnershipBusy:
            stop.wait(0.1)
            continue
        if host.phase == "bootstrap":
            return host, replace(candidate, owner=None, actor=None)
        daemon_owner = getattr(host, "daemon_owner", None)
        if daemon_owner is not None and daemon_owner != host.owner:
            host.close()
            raise StartupRefusal("PRINCIPAL_CONFIG_MISMATCH", "the daemon owner differs from the home owner")
        if host.owner != hint:
            host.close()
            now = time.monotonic()
            if disagreement is None:
                disagreement = now + config.budgets.shutdown_drain
            if now >= disagreement:
                raise StartupRefusal("PRINCIPAL_CONFIG_MISMATCH", "the home owner changed while reopening")
            stop.wait(0.1)
            continue
        return host, candidate
    return None


class LiveHost:
    """The host the watcher currently serves through; the caller closes it.

    ``host`` is replaced as soon as the watcher attaches the next one, so on
    every exit of ``watch_principal`` (stopped, refused, any exception) it is the
    host a request may still be dispatching through.
    """

    def __init__(self, host):
        self.host = host


def watch_principal(server, config, live, stop):
    """Rebind ``server`` as the daemon changes phase until ``stop`` is set.

    ``live`` is a ``LiveHost``; the watcher closes the hosts it abandons and never
    the live one, which the caller closes after the server has drained.  The hook
    that publishes each new binding is the server's own ``on_rebound``.
    """
    pending = False
    noted = []
    while not stop.wait(0.1):
        if not (pending or live.host.daemon_changed()):
            continue
        live.host.close()
        reopened = reopen(config, stop, live.host)
        if reopened is None:
            break
        live.host, config = reopened
        pending = not _rebound(server, config, live.host, noted)


def _rebound(server, config, host, noted):
    try:
        server.rebind(config, request_once=host)
    except StartupRefusal as refusal:
        if refusal.code != "BACKEND_UNAVAILABLE" and refusal.reason != "EPOCH_INCONSISTENT":
            raise
        host.close()
        key = (refusal.code, refusal.reason)
        if noted[:1] != [key]:
            noted[:] = [key]
            record = {"event": "rebind_waiting", "code": refusal.code, "reason": refusal.reason}
            print(json.dumps(record, separators=(",", ":"), sort_keys=True), file=sys.stderr, flush=True)
        return False
    noted.clear()
    return True
