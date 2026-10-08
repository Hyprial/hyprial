"""Network endpoint resolution: isolation, listen derivation and peer discovery."""

from __future__ import annotations

from __future__ import annotations
import os
import shlex
import socket
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, TYPE_CHECKING
from hyprial.kernel import DEFAULT_POLICIES, EXTERNAL_IO
from hyprial.kernel import configured_hyprial_home
from hyprial.kernel import (
    FORWARDING_COMMAND_ENV,
    FORWARDING_UP_ENV,
)
from hyprial.daemon.impl.forwarding_config import (
    FORWARDING_MODE_ENV,
    AutomaticForwarding,
    ForwardingConfigurationError,
    ForwardingPolicy,
    automatic_forwarding,
    daemon_forwarding_environment,
    forwarding_policy,
)
from hyprial.daemon.impl.bootstrap.discovery  import (
    CommandEndpoints,
    merge_endpoints,
)
from hyprial.daemon.impl.transport import (
    ZenohConfig,
    ZenohTransport,
    zenoh_environment_flag,
)
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority
from hyprial.kernel import HarnessLaunchSpec
from hyprial.kernel import (
    TEARDOWN_BUDGETED_SECONDS,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
)


NETWORK_ISOLATED_ENV = "HYPRIAL_NETWORK_ISOLATED"

ISOLATED_DEFAULT_LISTEN = "tcp/127.0.0.1:0"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

def network_isolated_from_environment() -> bool:
    """Fail closed once set: only UNSET means "not isolated".

    The shared zenoh flag parser treats an empty value as unset (default),
    which is exactly how ``export HYPRIAL_NETWORK_ISOLATED=`` or an unfilled
    template would leak a test daemon onto the network.  So an empty or
    whitespace value is refused here, and an unknown value is refused by the
    shared parser.  Unset stays "not isolated" because production does not
    set it; the test entry points refuse to start without it instead.
    """

    raw = os.environ.get(NETWORK_ISOLATED_ENV)
    if raw is None:
        return False
    if not raw.strip():
        raise ValueError(
            f"{NETWORK_ISOLATED_ENV} is set but empty; use 1 to isolate "
            "this daemon or unset it"
        )
    return zenoh_environment_flag(NETWORK_ISOLATED_ENV)

def _endpoint_host(endpoint: str) -> str | None:
    """``tcp/127.0.0.1:7447`` -> ``127.0.0.1``; ``tcp/[::1]:0`` -> ``::1``."""

    locator = endpoint.split("#", 1)[0].split("?", 1)[0]
    _, slash, address = locator.partition("/")
    if not slash:
        return None
    # A zenoh locator's address is host:port (IPv6 bracketed): the standard
    # authority parser handles both, brackets included.
    try:
        return urlsplit(f"//{address}").hostname
    except ValueError:
        return None

def compose_daemon_worker_launch(
    *,
    registry: Any,
    hyprial_home: Path,
    spec: HarnessLaunchSpec,
    channel: Any,
    environ: Mapping[str, str],
) -> Any:
    """The daemon's child-environment factory body, one call per worker launch.

    Every managed carrier -- pi, codex app-server, the Claude agent SDK,
    jev and the PTY connectors -- receives its environment from here, so
    this is the one place the ``workerProxy`` route is decided.  It is read
    per launch, never at startup: ``hyprial config set workerProxy.*``
    applies to the next worker without a daemon restart, and a damaged
    setting fails THIS start loudly.  Module level so the wiring itself is
    testable without building a daemon.
    """

    from hyprial.identity import compose_worker_child_launch
    from hyprial.identity import launch_worker_proxy_route

    return compose_worker_child_launch(
        registry=registry,
        hyprial_home=hyprial_home,
        channel=channel,
        environ=environ,
        agent_name=spec.name,
        worker_proxy=launch_worker_proxy_route(hyprial_home, spec),
    )

def _resolve_forwarding(
    hyprial_home: Path,
    node_id: str,
    *,
    isolated: bool,
    zenoh_listen: tuple[str, ...],
) -> tuple[ForwardingPolicy, dict[str, str], AutomaticForwarding | None, str | None]:
    """The daemon's forwarding decision: policy, launch variables, plan, reason.

    Precedence: isolation vetoes; ``off`` suppresses every source (a durable
    rollback, even over explicit or generated variables); explicit or
    generated variables win otherwise (step 4a); ``auto``/``on`` then use a
    sidecar-joined home's automatic plan. ``on`` refuses to start when that
    plan is unavailable; ``auto`` records the reason and stays off.
    """

    try:
        policy = forwarding_policy(os.environ, hyprial_home)
    except ForwardingConfigurationError as error:
        raise ValueError(f"{error.code}: {error}") from error
    if isolated:
        if policy.mode == "on":
            raise ValueError(
                f"{NETWORK_ISOLATED_ENV} is set but {FORWARDING_MODE_ENV}=on; "
                "an isolated daemon never forwards"
            )
        return policy, {}, None, None
    if policy.mode == "off":
        return policy, {}, None, None
    environment = _resolve_forwarding_environment(node_id)
    if environment:
        if not zenoh_listen:
            raise ValueError("forwarding requires explicit HYPRIAL_ZENOH_LISTEN")
        return policy, environment, None, None
    if policy.mode not in ("auto", "on"):
        return policy, {}, None, None
    automatic, reason = automatic_forwarding(
        hyprial_home, os.environ, node_id=node_id
    )
    if automatic is not None and zenoh_listen:
        # An explicit listen list stays a complete override: reuse a bound
        # loopback entry in it, never append a hidden one.
        target = next(
            (
                endpoint.removeprefix("tcp/")
                for endpoint in zenoh_listen
                if _endpoint_host(endpoint) == "127.0.0.1"
                and not endpoint.endswith(":0")
            ),
            None,
        )
        if target is None:
            automatic, reason = None, "LISTEN_CONFLICT"
        else:
            return policy, automatic.environment(target), None, None
    if automatic is None and policy.mode == "on":
        raise ValueError(
            f"FORWARDING_UNAVAILABLE: {FORWARDING_MODE_ENV}=on but {reason}"
        )
    return policy, {}, automatic, reason

def _reserve_loopback_endpoint() -> str:
    """A free loopback port for this daemon's forwarding inbound listener.

    Zenoh 1.9 cannot report the port a ``:0`` listener bound, so the OS
    picks one here and the session binds it right after; a collision in
    between raises at session open and ``_open_transport`` picks again.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"

def _open_transport(
    config: ZenohConfig,
    forwarding_listen: tuple[str, ...],
    *,
    derived_listen: str | None = None,
    event_sink: Callable[[str, dict[str, object]], None] | None = None,
) -> tuple[TransportSessionAuthority, tuple[str, ...], str | None]:
    """Open the session; re-pick ONLY the forwarding loopback port on a bind
    collision on that exact endpoint. A best-effort derived listener gets one
    fallback open without that endpoint; configured listeners still surface
    every failure unchanged. Forwarding re-picks use the registered external-
    I/O restart budget."""

    rebinds = DEFAULT_POLICIES[EXTERNAL_IO].max_restarts
    derived_error: str | None = None
    while True:
        try:
            native = ZenohTransport(config, event_sink=event_sink)
        except Exception as error:
            error_text = str(error)
            if (
                forwarding_listen
                and rebinds > 0
                and forwarding_listen[0] in error_text
            ):
                rebinds -= 1
                replacement = (_reserve_loopback_endpoint(),)
                config = replace(
                    config,
                    listen=tuple(
                        replacement[0]
                        if endpoint == forwarding_listen[0]
                        else endpoint
                        for endpoint in config.listen
                    ),
                )
                forwarding_listen = replacement
                continue
            if derived_listen is None or derived_listen not in config.listen:
                raise
            derived_error = error_text
            config = replace(
                config,
                listen=tuple(
                    endpoint
                    for endpoint in config.listen
                    if endpoint != derived_listen
                ),
            )
            derived_listen = None
            continue
        # Only native-open errors can justify removing a derived listener.
        # Actor construction is a separate acquisition and keeps its error.
        try:
            return TransportSessionAuthority(native), forwarding_listen, derived_error
        except BaseException as error:
            try:
                native.close()
            except BaseException as cleanup_error:
                error.add_note(
                    "native transport close after authority construction raised "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

def _resolve_forwarding_environment(node_id: str) -> dict[str, str]:
    """The sidecar launch variables for this daemon, resolved in-process.

    Already-generated variables (the CLI launcher, a watchdog, a fixture)
    pass through unchanged. Otherwise the operator inputs -- the sidecar
    binary and inbound target -- are resolved here with the same function
    the launcher uses, so a direct ``daemon run`` or a watchdog restart
    keeps forwarding instead of silently starting without it. An invalid
    explicit configuration refuses startup, as the launcher does.
    """

    generated = {
        name: os.environ[name]
        for name in (FORWARDING_COMMAND_ENV, FORWARDING_UP_ENV)
        if os.environ.get(name)
    }
    if generated:
        return generated
    try:
        return daemon_forwarding_environment(
            configured_hyprial_home()[0], os.environ, node_id=node_id
        )
    except ForwardingConfigurationError as error:
        raise ValueError(f"{error.code}: {error}") from error

def _isolated_endpoints(
    listen: tuple[str, ...], connect: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate endpoints for an isolated daemon; refuse, never silently drop.

    Forwarding is refused by the PRESENCE of any HYPRIAL_FORWARDING_* variable,
    not by endpoint address: its sidecar exposes loopback endpoints that tunnel
    to the tailnet, so an address check would wave it through.
    """

    leaked = sorted(name for name in os.environ if name.startswith("HYPRIAL_FORWARDING_"))
    if leaked:
        raise ValueError(
            f"{NETWORK_ISOLATED_ENV} is set but forwarding is configured "
            f"({', '.join(leaked)}); unset them -- forwarding joins the tailnet"
        )
    remote = [
        endpoint
        for endpoint in (*listen, *connect)
        if _endpoint_host(endpoint) not in _LOOPBACK_HOSTS
    ]
    if remote:
        raise ValueError(
            f"{NETWORK_ISOLATED_ENV} is set but HYPRIAL_ZENOH_LISTEN/CONNECT "
            f"name non-loopback endpoints: {', '.join(remote)}"
        )
    return (listen or (ISOLATED_DEFAULT_LISTEN,)), connect

def _endpoint_list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())

def _reconcile_tick_budget() -> float:
    """Wall-clock budget for one periodic reconcile tick, in seconds.

    The tick runs inside the accept loop, so an over-budget tick is an IPC
    availability event: it is logged loudly (``daemon.reconcile_overrun``)
    with the slowest phase named.  Overrunning the budget NEVER terminates
    the serve loop -- this daemon is not launchd-supervised, so a dead loop
    is an outage with no restarter (postmortem 2026-08-23).
    ``HYPRIAL_RECONCILE_TICK_BUDGET`` overrides the 1s default; 0 disables.
    """

    raw = os.environ.get("HYPRIAL_RECONCILE_TICK_BUDGET")
    if raw is None:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return value if value >= 0 else 1.0

def _lock_wait_timeout() -> float:
    """Seconds to wait for a previous daemon to release the state-dir lock.

    ⚠️ This used to be its own literal, "sized to clear a normal shutdown".
    A *normal* shutdown was the wrong thing to size against: on 2026-08-31 a
    replacement daemon gave up after 15s while the outgoing one was still
    inside a teardown budgeted at :data:`TEARDOWN_BUDGETED_SECONDS`. Waiting
    less than that is refusing to start for a reason the outgoing daemon was
    still working through.

    ⚠️ Waiting *at least* that long does not make the start safe. The budget
    is not a bound -- see the note beside the constant -- so this can still
    expire while the outgoing daemon sits in an unbudgeted step. It removes
    the case where we were wrong **by construction**; it does not remove the
    case where we are unlucky.

    So it derives from the budget rather than restating a number.
    Overridable via ``HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT`` (0 disables the wait and
    restores fail-fast behaviour).
    """

    raw = os.environ.get("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT")
    if raw is None:
        return TEARDOWN_BUDGETED_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return TEARDOWN_BUDGETED_SECONDS
    return value if value >= 0 else TEARDOWN_BUDGETED_SECONDS

def _zenoh_endpoints(
    *,
    env_listen: tuple[str, ...],
    env_connect: tuple[str, ...],
    stored_listen: tuple[str, ...],
    stored_connect: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Merge per-launch overrides with the persistent desired-state config.

    Environment variables win per side; an empty environment side falls back
    to the stored endpoints so `hyprial init --listen/--connect` keeps working
    across restarts without any exported variable.
    """

    return (
        env_listen if env_listen else stored_listen,
        env_connect if env_connect else stored_connect,
    )


class _EndpointResolutionMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _network_isolation_status(self) -> JsonObject:
        """Whether this daemon IS isolated, read off what it did and holds.

        ``effective`` is true only when isolation was requested AND every
        governed path is observably closed: endpoints all loopback, no listen
        derivation, discovery never consulted, gossip off, no forwarding
        sidecar or forwarded endpoints, no usage fetcher.  A path added later
        without a check here leaves ``effective`` unable to vouch for it --
        which is why the checks are listed, not summarised.
        """

        endpoints = (*self.zenoh_listen, *self.zenoh_connect)
        checks = {
            "endpointsLoopbackOnly": all(
                _endpoint_host(endpoint) in _LOOPBACK_HOSTS for endpoint in endpoints
            ),
            "listenNotDerived": not self._startup_network["listenDerived"],
            "discoveryNotConsulted": not self._startup_network["discoveryConsulted"],
            "gossipOff": not self._startup_network["gossip"],
            "forwardingOff": (
                self._forwarding_supervisor is None
                and not self._forwarding_effective
                and not self._forwarding_start_attempted
            ),
            "usageFetchOff": self._usage_cache is None,
        }
        return {
            "requested": self.network_isolated,
            "effective": self.network_isolated and all(checks.values()),
            "checks": checks,
        }

    def _derive_listen_endpoint(self) -> str | None:
        """Nothing to derive any more: the host tailscale is gone.

        The tailnet cutover (2026-10-03) removed host-tailscale discovery
        and tailnet listen derivation.  Inbound reachability now belongs to
        the Tailcat forwarding sidecar, whose loopback listener is composed
        by the automatic-forwarding path; a node with no configured listen
        endpoint and no forwarding simply listens nowhere, which the caller
        reports.  Kept as a method (returning None) because the startup
        wiring and its fallback budget still route through it.
        """

        return None

    def _discover_peer_endpoints(self) -> tuple[str, ...]:
        """Endpoints from forwarding and the peer directory, combined.

        Both sources are on by default and their union is the dial set:
        forwarding endpoints first, directory ones after, no duplicates.
        Either half failing leaves the other intact -- forwarding failures log
        `zenoh.forwarding.*` events and a directory that cannot answer is
        silence by construction (see `discovery.py`).  Nothing here may raise:
        the node's configured endpoints are already sufficient, and a sidecar
        or directory having a bad day must not be able to stop a daemon from
        starting.

        Two switches cut halves away, on purpose:

        * `HYPRIAL_PEER_DISCOVERY=0` turns off the command directory
          (leaving forwarding endpoints, if configured).  The default is on
          for the same reason it always was: discovery is additive (configured
          endpoints are all kept, so no working node can be cut off).  It
          does not create reachability; it acts on reachability that already
          existed.
        * `HYPRIAL_FORWARDING_EXCLUSIVE=1` restores the pre-coexist behaviour
          where configured forwarding is the *only* source: the S5 acceptance
          and the isolated negative controls depend on "sidecar stopped means
          traffic stopped", which a silent directory fallback would falsify.

        The forwarding sidecar is owned by `ForwardingSidecarSupervisor`,
        which relaunches it under the registered process-lifecycle budget;
        while it is down the forwarding half is simply empty rather than
        fatal.
        """

        forwarding_configured = bool(self._forwarding_environment)
        if forwarding_configured:
            # Synchronous on purpose: Zenoh fixes its connect set when the
            # session opens, so the first attempt belongs on this startup
            # path; every relaunch after a failure rides the scheduler.
            self._start_forwarding_supervisor()
        forwarding: tuple[str, ...] = ()
        backend = self._forwarding_backend()
        if backend is not None:
            forwarding = backend.list_reachable_endpoints()
            self._forwarding_effective = forwarding
        if self._forwarding_dialed is None:
            # What the Zenoh session actually dials is fixed by the FIRST
            # pass -- including the empty answer of a first-start failure,
            # which is exactly the "recovered later, dialed never" gap that
            # must stay visible until a redial closes it; only a successful
            # rebuild (``_redial_forwarding``) advances it after this.
            self._forwarding_dialed = forwarding
        if forwarding_configured and zenoh_environment_flag(
            "HYPRIAL_FORWARDING_EXCLUSIVE", default=False
        ):
            # Negative-control mode: explicitly configured forwarding never
            # falls back to the command directory.  Otherwise stopping the
            # sidecar could leave the same Zenoh traffic working and make
            # the negative control false.  Coexistence is the default;
            # exclusivity is now an explicit operator choice.
            return forwarding
        if not zenoh_environment_flag("HYPRIAL_PEER_DISCOVERY", default=True):
            return forwarding
        # HYPRIAL_PEER_DISCOVERY_COMMAND selects the escape-hatch backend:
        # any command printing one endpoint per line.  It is what makes a
        # multi-node mesh testable on a single host, and it is the seam for
        # a deployment whose directory is neither the org directory nor a
        # static list.  Without it there is no second backend: peer
        # reachability is the org directory's, fed to the sidecar through
        # the forwarding half's ``directory`` callback above.
        command = os.environ.get("HYPRIAL_PEER_DISCOVERY_COMMAND", "").strip()
        if not command:
            self._connect_discovered = ()
            return forwarding
        backend = CommandEndpoints(tuple(shlex.split(command)))
        try:
            discovered = backend.list_reachable_endpoints()
        except Exception as error:  # noqa: BLE001 - startup must not depend on it
            self._log(
                "warn",
                "zenoh",
                "zenoh.discovery.failed",
                detail=str(error),
            )
            discovered = ()
        self._connect_discovered = discovered
        # Forwarding first, directory after: a deliberately pinned peer
        # behaves predictably instead of racing the directory, and a daemon
        # that configured forwarding keeps its sidecar ports dialled first.
        return merge_endpoints(forwarding, discovered)

    def _tailcat_directory_peers(self) -> Mapping[str, str]:
        """Org-directory peers as ``deviceId -> Tailcat address``, self excluded."""

        from hyprial.daemon.impl.org.network import directory_peers_for_app

        return directory_peers_for_app(self)

    def _tailnet_status_json(
        self, *, refresh: bool = False
    ) -> dict[str, object]:
        """The ``ps`` wire's ``tailnet`` block, kept as an empty projection.

        # LAX(tailnet-cutover): 宿主 tailscale 已删除，正式做法是由组织目录
        # 与边车 status 投影出本块（W-D/W-E 接线）；本轮返回固定空形状，
        # 保持已发布的 wire 键不消失。
        """

        return {"self": {}, "peers": []}
