"""Shared subprocess helpers for isolated daemon E2E runners."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import sys
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from hyprial.kernel import ipc_errors
from hyprial.kernel import TEARDOWN_BUDGETED_SECONDS

ROOT = Path(__file__).resolve().parents[2]
_TEST_HELPERS = str(ROOT)
if _TEST_HELPERS not in sys.path:
    sys.path.insert(0, _TEST_HELPERS)
from tests.support.runtime._daemon_diagnostics import (  # noqa: E402
    child_pid_state,
    daemon_log_summary,
    write as write_daemon_diagnostics,
)

from contract.e2e._runner_shared.ps import ps_from_socket  # noqa: E402 - ROOT bootstrap for standalone path loading.
from contract.e2e._runner_shared.versions import (  # noqa: E402 - ROOT bootstrap for standalone path loading.
    PROJECT_VERSION as PROJECT_VERSION,
    write_project_version as write_project_version,
    _expect_version as _expect_version,
    _dynamic_version_file as _dynamic_version_file,
)

from contract.e2e._runner_shared.artifacts import (  # noqa: E402
    artifacts_root as artifacts_root,
    preserve_failure_state as preserve_failure_state,
    scenario_tempdir as scenario_tempdir,
    short_temp_root as short_temp_root,
)

from contract.e2e._runner_shared.probe_support import (  # noqa: E402
    bounded_repr as bounded_repr,
    free_tcp_endpoint as free_tcp_endpoint,
    teardown_guard as teardown_guard,
)

from contract.e2e._hyprial_resolution import (  # noqa: E402 - ROOT bootstrap above; see that module.
    HYPRIALResolution as HYPRIALResolution,
    assert_tree_matches_declared_sha as assert_tree_matches_declared_sha,
    hyprial_executable as hyprial_executable,
    record_resolution as record_resolution,
    resolve_hyprial as resolve_hyprial,
)

# PR #332 F4②: the transient judgment lives in the shared registry
# (contracts/ipc_errors.py) -- one hierarchy, one registration point, both
# ends.  The CLI reports a transient daemon-IPC failure as one JSON object
# with ok:false (cli.py _daemon_request); the code it carries deserialises
# back into the SAME registered class the daemon minted, so this runner and
# every other client answer "is this transient?" with
# ``except TransientDaemonError``.  DAEMON_RESTORING joined the transient
# table in PR #332 F4①: the restore gate's refusal is the restart window's
# most common answer, not a scenario failure.


def launch_result_is_ready(result: dict[str, Any]) -> bool:
    """Does an ``hyprial init`` answer report a running daemon?

    The third reader of ``DaemonLaunchResult``
    (src/hyprial/contracts/daemon_launch.py, PR #332 F2): init's ``--json``
    output is that type's ``to_payload()`` — the daemon's ping payload
    spread flat (``running``/``pid``/``phase`` at the top level), not the
    nested ps-shaped ``daemon`` object.  The nested read stays for one
    line so an older daemon's answer fails the caller's assertion, not a
    KeyError above it.
    """

    daemon = result.get("daemon")
    return result.get("ok") is True and (
        result.get("running") is True
        or (isinstance(daemon, dict) and daemon.get("running") is True)
    )


def transient_cli_error(
    stdout: str, message: str
) -> ipc_errors.TransientDaemonError | None:
    """Return the registered transient error when stdout is a CLI IPC-failure object.

    PR #332 F4②: deserialisation goes through the shared registry, so the
    instance this returns is the SAME class the daemon minted and every
    other client (CLI / MCP agent-channel / adapter worker) raises.  An
    unknown code (a version-skewed peer minted one this build has never
    registered) is not transient here: it returns None and the command
    fails as a hard scenario error carrying the code verbatim.
    """

    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("ok") is not False:
        return None
    code = value.get("code")
    if not isinstance(code, str):
        return None
    return ipc_errors.transient_error_from_code(code, message)


# --------------------------------------------------------------------------- #
# Ambient worker-environment stripping (shared convention, 2026-08-19)
# --------------------------------------------------------------------------- #
# Any probe that spawns isolated daemons or harness TUIs from INSIDE a hyprial
# worker process inherits that worker's own identity: HYPRIAL_WORKER_* signs the
# probe's daemon IPC as the worker, HYPRIAL_HOME / HARNESS_STATE_DIR /
# HARNESS_SOCKET_PATH pin the PRODUCTION daemon's socket, and PI_* session
# variables leak a parent pi session into a child pi.  Every disposable
# daemon environment and every TUI env line must strip these keys before
# adding its own roots -- the pi attach leg hit this for real.
STRIP_ENV_KEYS = (
    "HYPRIAL_WORKER_ACTOR",
    "HYPRIAL_WORKER_SESSION_REF",
    "HYPRIAL_WORKER_SESSION_REF_FILE",
    "HYPRIAL_MANAGED_WORKER",
    "HYPRIAL_AGENT_SESSION",
    "HARNESS_SOCKET_PATH",
    "PI_SESSION_ID",
    "PI_SESSION_FILE",
    "PI_CODING_AGENT",
    "PI_PROVIDER",
    "PI_MODEL",
    "PI_REASONING_LEVEL",
)


def sanitized_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` minus every ambient worker-identity key (see STRIP_ENV_KEYS)."""

    cleaned = dict(env)
    for key in STRIP_ENV_KEYS:
        cleaned.pop(key, None)
    return cleaned


def env_strip_flags() -> list[str]:
    """``env -u KEY`` flags for inline tmux/subprocess env commands."""

    return [flag for key in STRIP_ENV_KEYS for flag in ("-u", key)]









def desired_state_document(root: Path) -> dict[str, Any]:
    """The daemon's desired-state document, read through its store.

    Runners used to open ``<state>/desired-state.json.v1`` directly.  On
    intg/daemon-readiness the authority is SQLite (U0a-2) and that file does
    not exist -- E2E-006/012/013 went red on intg 7935229d with "[Errno 2]"
    while every assertion they make is about the document's ``providers``
    rows, not about a file.  ``DesiredStateStore`` reads whichever storage
    the tree under test uses (JSON on dev, SQLite on intg) and ``to_json``
    renders the same document shape on both, so the assertions stay exactly
    as written and no runner learns a storage's file name.
    """
    from hyprial.daemon import DesiredStateStore

    return DesiredStateStore(root / "state" / "desired-state.json").load().to_json()












ALLOW_UNISOLATED_ENV = "HYPRIAL_E2E_ALLOW_UNISOLATED_DAEMON"
#: Every variable that lets a scenario bypass an isolation refusal.  The gate
#: strips all of them from what it passes to scenarios, and a test fails if a
#: scenario reads an HYPRIAL_E2E_ALLOW_* name that is not registered here --
#: so adding a bypass without closing it on the gate path turns red.
BYPASS_ENVIRONMENT = frozenset({ALLOW_UNISOLATED_ENV})

#: Production Feishu bots.  A gateway or credential under one of these names
#: refuses start() in every case: declared scenario, fake worker, or neither.
PRODUCTION_FEISHU_APPS = ("hq-adjutant-feishu", "hyprial-kanban", "mac-studio-feishu")

#: The ONLY scenarios that load Feishu gateways at boot on purpose, each with
#: the named test Apps (by appId) it may load.  This is the single source:
#: docs/guides/network-isolation.md lists the same scenarios, and
#: tests/daemon/network/test_network_isolated.py fails if the two sets differ or this is
#: empty.  A scenario opts in with ``allow_feishu_test_apps(Path(__file__).name)``.
FEISHU_EXEMPT_SCENARIOS: dict[str, frozenset[str]] = {
    # E2E-013: a fabricated pin-probe gateway (never started) and, when the
    # Lark prerequisites hold, the same dedicated test App.
    "run-all-harness-lifecycle.py": frozenset(
        {"isolated_e2e013_probe", "cli_aaf9ea19d4b8dbe4"}
    ),
    # E2E-011: synthetic Apps registered through its loopback stub, loaded at
    # boot by step A8.
    "run-lark-app-onboarding.py": frozenset(
        {"cli_env_app", "cli_e2e011_created_app", "cli_existing_app"}
    ),
}


def production_daemon_alive(home: Path | None = None) -> bool:
    """Whether a live production daemon answers on this machine.

    The login user's real home (from the password database, not $HOME, which
    scenarios redirect) + ``.hyprial/state/daemon.sock``: a connectable socket
    means this machine is a production node on the mesh.
    """

    import pwd
    import socket as _socket

    base = home if home is not None else Path(pwd.getpwuid(os.getuid()).pw_dir)
    path = base / ".hyprial" / "state" / "daemon.sock"
    if not path.exists():
        return False
    probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


#: A test node's identity (Allen 2026-09-24, «放行e2e-前缀的测试节点»; scope
#: per infra-ops the same day): full match, case-sensitive.
E2E_TEST_NODE_ID = re.compile(r"e2e-[a-z0-9][a-z0-9._-]*")
E2E_TEST_OWNER = "e2e"
_LOOPBACK_LISTEN = re.compile(r"tcp/127\.0\.0\.1:[1-9][0-9]{0,4}")


def e2e_test_node_problems(environment: dict[str, str]) -> list[str]:
    """Why ``environment`` (the dict the scenario launches with) is not a test node.

    A test node is identified by what the daemon will actually run as, read
    from the launch dict itself -- never by a switch variable:
    HYPRIAL_NODE_ID full-matches E2E_TEST_NODE_ID, HYPRIAL_OWNER is exactly
    "e2e" (a daemon under the real user's identity is not a test node, only a
    renamed one), and HYPRIAL_ZENOH_LISTEN is a loopback endpoint so it never
    binds the production daemon's tailnet address on the same machine.
    """

    problems = []
    node_id = environment.get("HYPRIAL_NODE_ID", "")
    if not E2E_TEST_NODE_ID.fullmatch(node_id):
        problems.append(f"HYPRIAL_NODE_ID {node_id!r} is not e2e-<name>")
    if environment.get("HYPRIAL_OWNER") != E2E_TEST_OWNER:
        problems.append(f"HYPRIAL_OWNER is not exactly {E2E_TEST_OWNER!r}")
    if not _LOOPBACK_LISTEN.fullmatch(environment.get("HYPRIAL_ZENOH_LISTEN", "")):
        problems.append("HYPRIAL_ZENOH_LISTEN is not tcp/127.0.0.1:<port>")
    return problems


def refuse_unisolatable_daemon(
    name: str, environment: dict[str, str], *, home: Path | None = None
) -> int | None:
    """Exit code 3 when a scenario that cannot isolate its daemon must not run.

    E2E-005/E2E-010 start released builds that predate
    HYPRIAL_NETWORK_ISOLATED.  A daemon launched from ``environment`` as an
    e2e test node (e2e_test_node_problems) runs, on any machine.  Otherwise it
    runs only with the explicit override AND on a machine with no live
    production daemon -- a condition no environment variable can satisfy.
    """

    if not e2e_test_node_problems(environment):
        print(
            f"RESULT NOTE {name}: unisolated test node "
            f"{environment['HYPRIAL_NODE_ID']} (owner {E2E_TEST_OWNER}, loopback listen)",
            file=sys.stderr,
        )
        return None
    if production_daemon_alive(home):
        reason = "a live production daemon answers on this machine"
    elif os.environ.get(ALLOW_UNISOLATED_ENV) != "1":
        reason = f"{ALLOW_UNISOLATED_ENV}=1 is not set"
    else:
        return None
    print(
        f"RESULT REFUSED {name}: starts a released build that predates "
        f"HYPRIAL_NETWORK_ISOLATED, so its daemon cannot be isolated ({reason}). "
        "It runs only on a machine with no production mesh, with "
        f"{ALLOW_UNISOLATED_ENV}=1 (docs/guides/network-isolation.md).",
        file=sys.stderr,
    )
    return 3


#: The product's own readiness wait for ``hyprial init`` when the
#: environment does not override it (``--ready-timeout`` default in cli.py).
PRODUCT_INIT_READY_DEFAULT_SECONDS = 15.0
#: How long the harness keeps waiting past the product's readiness wait, so
#: that init always fails with its own JSON verdict (DAEMON_START_TIMEOUT and
#: the child's state) instead of being killed by subprocess.TimeoutExpired.
INIT_HARNESS_MARGIN_SECONDS = 45.0
#: Floor for the harness-side wait on ``hyprial init``.
INIT_HARNESS_MIN_SECONDS = 90.0


def product_init_ready_seconds(environment: Mapping[str, str]) -> float:
    """The readiness wait ``hyprial init`` will use under ``environment``."""

    raw = environment.get("HYPRIAL_INIT_READY_TIMEOUT", "")
    try:
        value = float(raw)
    except ValueError:
        return PRODUCT_INIT_READY_DEFAULT_SECONDS
    return value if value > 0 else PRODUCT_INIT_READY_DEFAULT_SECONDS


#: How long the harness keeps waiting past the product's own teardown budget
#: for ``hyprial daemon stop``.
STOP_HARNESS_MARGIN_SECONDS = 15.0


def stop_harness_timeout() -> float:
    """Harness-side subprocess timeout for ``hyprial daemon stop``.

    Always greater than the product's budgeted teardown (the close-step
    budgets plus the exit backstop, contracts/daemon_teardown.py), and so
    also greater than the daemon's shutdown-stall dump threshold, which
    otherwise could never leave evidence before the harness kills the stop.
    On 2026-09-28, task 36521 (E2E-002), the daemon logged daemon.exiting
    and the flat 30 s harness wait killed ``daemon stop`` with no product
    verdict and no stall dump -- the same inversion #911 fixed for init.
    """

    return TEARDOWN_BUDGETED_SECONDS + STOP_HARNESS_MARGIN_SECONDS


def init_harness_timeout(environment: Mapping[str, str]) -> float:
    """Harness-side subprocess timeout for ``hyprial init``.

    Always strictly greater than the product's own readiness wait.  On
    2026-09-27 the E2E gates raised HYPRIAL_INIT_READY_TIMEOUT to 45 s (#907)
    while this runner still killed init at a flat 30 s, so a slow start
    surfaced as TimeoutExpired with no product verdict (task 36275, E2E-002
    and E2E-003).  Deriving the harness wait from the product wait keeps the
    two from inverting again, whatever the workflow sets.
    """

    return max(
        INIT_HARNESS_MIN_SECONDS,
        product_init_ready_seconds(environment) + INIT_HARNESS_MARGIN_SECONDS,
    )


def paired_user_id(owner: str) -> str:
    """The test uid (``ou_`` + 32 hex) paired with an isolated owner."""

    return "ou_" + hashlib.sha256(f"user:{owner}".encode("utf-8")).hexdigest()[:32]


def with_paired_user_id(env: Mapping[str, str]) -> dict[str, str]:
    """*env* with ``HYPRIAL_USER_ID`` derived from ``HYPRIAL_OWNER`` unless set.

    The cell runtime composes only with a uid authority (OC1a): an owner
    override alone gives no cell runtime and so no headless delivery.  The
    uid follows the owner a runner assigned after construction; a runner
    that needs a specific uid sets ``env["HYPRIAL_USER_ID"]`` itself.
    """

    paired = dict(env)
    owner = paired.get("HYPRIAL_OWNER", "").strip()
    if owner and not paired.get("HYPRIAL_USER_ID", "").strip():
        paired["HYPRIAL_USER_ID"] = paired_user_id(owner)
    return paired


class IsolatedDaemon:
    def __init__(
        self,
        executable: Path,
        root: Path,
        node_id: str,
        *,
        listen: str | None = None,
        connect: str | None = None,
        tailcat_forwarding: bool = False,
        gossip: bool = False,
    ) -> None:
        if gossip and not tailcat_forwarding:
            # Fail closed: gossip is only reachable through the forwarding opt-in, so
            # a caller cannot turn it on while claiming the blanket isolation switch.
            raise ValueError("gossip=True requires tailcat_forwarding=True")
        self.executable = executable
        self.root = root
        self.node_id = node_id
        self.tailcat_forwarding = tailcat_forwarding  # E2E-025 opt-in; see _tailcat_isolation
        self.gossip = gossip  # S1/v12 opt-in; the guard asserts this value is what runs
        self.env = sanitized_env(os.environ)
        self.env.pop("HYPRIAL_USER_ID", None)
        self.env.update(
            {
                # #361 made the node's owner mandatory (HYPRIAL_OWNER, else
                # settings.json "owner", else the daemon refuses to start).
                # An isolated home has no settings.json, and inheriting the
                # host shell's HYPRIAL_OWNER would stamp the developer's real
                # identity on a throwaway node (or nothing at all on the CI
                # runner -- E2E-002 on 2026-09-05, intg a9673b02).  The
                # isolation defines the identity; runners that need a
                # specific owner assign env["HYPRIAL_OWNER"] after construction.
                "HYPRIAL_OWNER": "e2e",
                "HYPRIAL_HOME": str(root / "home"),
                "HARNESS_STATE_DIR": str(root / "state"),
                "HYPRIAL_NODE_ID": node_id,
                # An isolated daemon must never discover the host's production
                # mesh. Runners that intentionally exercise discovery must opt
                # back in after construction.
                "HYPRIAL_PEER_DISCOVERY": "0",
                # The switch that actually keeps it here: loopback endpoints
                # only, no discovery/derivation/forwarding/gossip/usage fetch
                # (HYPRIAL_PEER_DISCOVERY alone left forwarding and zenoh's
                # default listener open -- 2026-09-16 ban).  start() refuses
                # a daemon that does not REPORT itself isolated.
                "HYPRIAL_NETWORK_ISOLATED": "1",
            }
        )
        if tailcat_forwarding:  # lazy: this module is also loaded by path, off sys.path
            from _tailcat_isolation import tailcat_forwarding_env
            self.env.update(tailcat_forwarding_env(gossip=gossip))
        if listen:
            self.env["HYPRIAL_ZENOH_LISTEN"] = listen
        else:
            self.env.pop("HYPRIAL_ZENOH_LISTEN", None)
        if connect:
            self.env["HYPRIAL_ZENOH_CONNECT"] = connect
        else:
            self.env.pop("HYPRIAL_ZENOH_CONNECT", None)
        # The FEISHU_EXEMPT_SCENARIOS key this daemon declared, if any.
        self.feishu_scenario: str | None = None

    def allow_feishu_test_apps(self, scenario: str) -> None:
        """Declare that this scenario loads its listed Feishu test Apps at boot.

        Only the Feishu part of require_no_external_triggers changes, and only
        for this daemon: every gateway must be one of the scenario's listed
        appIds, and every secrets/lark-*.json must belong to one of them.
        Autoupdate and owner identity are still refused, and the mesh must
        still report isolation.
        """

        if scenario not in FEISHU_EXEMPT_SCENARIOS:
            raise AssertionError(
                f"{scenario} is not in FEISHU_EXEMPT_SCENARIOS; loading Feishu "
                "gateways at boot needs an entry there and in the doc"
            )
        self.feishu_scenario = scenario

    def command(
        self,
        *args: str,
        exits: tuple[int, ...] = (0,),
        timeout: float = 30.0,
        transient_timeout: float = 0.0,
        stdin: str | None = None,
    ) -> dict[str, Any]:
        """Run one hyprial command and return its JSON object.

        Transient daemon-IPC failures (the shared registry in
        contracts/ipc_errors.py) raise the registered TransientDaemonError
        subclass.  With ``transient_timeout`` the command is retried for
        that many seconds first -- use it ONLY for read-only or idempotent
        commands: a retried mutation whose first attempt actually executed
        would run twice.
        """

        if args and args[0] == "init":
            timeout = max(timeout, init_harness_timeout(self.env))
        if tuple(args[:2]) == ("daemon", "stop"):
            timeout = max(timeout, stop_harness_timeout())
        deadline = time.monotonic() + transient_timeout
        while True:
            completed = subprocess.run(
                [str(self.executable), *args],
                env=with_paired_user_id(self.env),
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if completed.returncode in exits:
                break
            # The message is composed first so the raised transient error
            # still carries the stdout/stderr evidence (failure forensics).
            message = (
                f"hyprial {' '.join(args)} exited {completed.returncode}; "
                f"stdout={completed.stdout[-2000:]!r} stderr={completed.stderr[-2000:]!r}"
            )
            transient = transient_cli_error(completed.stdout, message)
            if transient is not None and time.monotonic() < deadline:
                time.sleep(0.2)
                continue
            if transient is not None:
                raise transient
            raise AssertionError(message)
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"hyprial {' '.join(args)} did not emit one JSON object: {completed.stdout!r}"
            ) from error
        if not isinstance(value, dict):
            raise AssertionError(f"hyprial {' '.join(args)} returned a non-object")
        return value

    def ps(self, *, transient_timeout: float = 0.0) -> dict[str, Any]:
        """Read the full status snapshot over IPC (R7 retired the CLI ``ps``)."""
        return ps_from_socket(
            self.root / "state" / "daemon.sock", transient_timeout=transient_timeout
        )
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call the public daemon IPC seam without adding a CLI-only command."""

        request_id = uuid4().hex
        frame = {
            "version": 1,
            "id": request_id,
            "method": method,
            "params": params,
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15.0)
            client.connect(str(self.root / "state" / "daemon.sock"))
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    raise AssertionError("daemon disconnected before IPC response")
                buffer.extend(chunk)
        response = json.loads(buffer.partition(b"\n")[0])
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise AssertionError(f"daemon returned an invalid IPC response: {response}")
        failure = response.get("error")
        if isinstance(failure, dict):
            raise AssertionError(f"daemon IPC {method} failed: {failure}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise AssertionError(f"daemon IPC {method} returned no object result")
        return result

    def start(self, init_args: tuple[str, ...] = ()) -> dict[str, Any]:
        """Run `hyprial init` (plus optional extra init flags) and await readiness."""

        # Before init launches the daemon: a pre-seeded home would trigger the
        # conditional paths the switch does not cover at the first tick.
        self.require_no_external_triggers()
        result = self.command("init", "--no-service", *init_args, "--json")
        if not launch_result_is_ready(result):
            raise AssertionError(f"daemon did not become ready: {result}")
        self.require_no_external_triggers()
        if self.tailcat_forwarding:
            from _tailcat_isolation import require_tailcat_forwarding_isolation
            require_tailcat_forwarding_isolation(self)
        else:
            self.require_network_isolated()
        return result

    def require_no_external_triggers(self) -> None:
        """Refuse a home that would take a path HYPRIAL_NETWORK_ISOLATED does not close.

        docs/guides/network-isolation.md lists three conditional paths the switch does
        not cover; each has a trigger in the home or environment, asserted
        absent here so "usually not triggered" becomes "cannot start":
        Feishu adapters (gateways in channels.json or secrets/lark-*.json),
        autoupdate (settings.json autoUpgrade), and `tailscale whoami` owner
        identity (no HYPRIAL_OWNER).  Feishu gateways are allowed in exactly
        two cases (see _feishu_problems); production bots never are.
        """

        home = self.root / "home"
        problems = self._feishu_problems(home)
        settings = home / "settings.json"
        if settings.exists():
            if json.loads(settings.read_text(encoding="utf-8")).get("autoUpgrade") is True:
                problems.append("settings.json enables autoUpgrade")
        if not self.env.get("HYPRIAL_OWNER", "").strip():
            problems.append("HYPRIAL_OWNER is unset (identity would ask `tailscale whoami`)")
        if problems:
            raise AssertionError(
                "isolated test home would reach the network through a path "
                "HYPRIAL_NETWORK_ISOLATED does not close: " + "; ".join(problems)
            )

    def _feishu_problems(self, home: Path) -> list[str]:
        """Feishu gateways/credentials in the home that this daemon may not load.

        Allowed only when (a) the Lark worker is replaced
        (HYPRIAL_LARK_WORKER_COMMAND set, E2E-007's fake worker), or (b) the
        scenario declared itself via allow_feishu_test_apps and every gateway
        is one of its listed test Apps.  A production bot is refused in all
        cases.  Residual, see the doc: a route send the scenario itself
        issues goes from the daemon to Feishu directly, not via the worker.
        """

        channels = home / "channels.json"
        gateways: list[dict[str, Any]] = []
        if channels.exists():
            loaded = json.loads(channels.read_text(encoding="utf-8")).get("gateways")
            gateways = [g for g in loaded or [] if isinstance(g, dict)]
        secrets = home / "secrets"
        credentials = sorted(secrets.glob("lark-*.json")) if secrets.is_dir() else []
        if not gateways and not credentials:
            return []
        production = sorted(
            {str(g.get("name")) for g in gateways if g.get("name") in PRODUCTION_FEISHU_APPS}
            | {
                path.stem.removeprefix("lark-")
                for path in credentials
                if path.stem.removeprefix("lark-") in PRODUCTION_FEISHU_APPS
            }
        )
        if production:
            return [f"production Feishu bot(s) in the home: {', '.join(production)}"]
        if self.env.get("HYPRIAL_LARK_WORKER_COMMAND", "").strip():
            return []
        if self.feishu_scenario is None:
            problems = []
            if gateways:
                problems.append(f"channels.json has {len(gateways)} Feishu gateway(s)")
            if credentials:
                problems.append("secrets/ holds Feishu credentials")
            return problems
        allowed = FEISHU_EXEMPT_SCENARIOS[self.feishu_scenario]
        problems = [
            f"gateway {g.get('name')!r} (appId {g.get('appId')!r}) is not a test App "
            f"listed for {self.feishu_scenario}"
            for g in gateways
            if g.get("appId") not in allowed
        ]
        refs = {str(g.get("credentialRef")) for g in gateways if g.get("appId") in allowed}
        problems.extend(
            f"secrets/{path.name} belongs to no listed test App gateway"
            for path in credentials
            if path.stem not in refs
        )
        return problems

    def require_network_isolated(self, restore_timeout: float = 120.0) -> None:
        """Refuse to continue unless the daemon itself reports isolation.

        Reads the daemon's EFFECTIVE state (``zenoh.isolated`` is computed from
        what startup did, not echoed from the environment), so a runner that
        dropped or mistyped the switch stops here instead of running a test
        daemon on the network.  ``ps`` waits out the restore window over the
        IPC ``ping`` seam, bounded by ``restore_timeout``.
        """

        status = self.ps(transient_timeout=restore_timeout)
        zenoh = status.get("zenoh") if isinstance(status.get("zenoh"), dict) else {}
        if zenoh.get("isolated") is not True:
            try:
                self.stop()
            finally:
                still_restoring = (
                    f"; still restoring after {restore_timeout:g}s, no full ps to read"
                    if status.get("restoring") is True
                    else ""
                )
                raise AssertionError(
                    "isolated test daemon does not report network isolation "
                    f"(zenoh.isolation={zenoh.get('isolation')!r}{still_restoring}); "
                    "refusing to run"
                )

    def stop(self) -> None:
        if (self.root / "state" / "daemon.sock").exists():
            result = self.command("daemon", "stop", "--json")
            if result.get("ok") is not True:
                raise AssertionError(f"daemon stop failed: {result}")

    def wait_stopped(self, timeout: float = 10.0) -> None:
        """Wait for the isolated daemon's complete teardown boundary.

        ``hyprial daemon stop`` returns after the public socket disappears, while
        the daemon deliberately releases ``daemon.lock`` only after adapter
        workers and log drainers have stopped.  Scenarios that immediately
        delete their temporary root must wait for that later boundary or a
        legitimate final adapter log can race ``TemporaryDirectory`` cleanup.
        """

        lock_path = self.root / "state" / "daemon.lock"
        started = time.monotonic()
        deadline = time.monotonic() + timeout
        with lock_path.open("a+", encoding="utf-8") as stream:
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    if time.monotonic() >= deadline:
                        metadata_path = self.root / "state" / "daemon.json"
                        child: dict[str, Any] = {
                            "pid": None,
                            "alive": False,
                            "cpuSeconds": None,
                        }
                        try:
                            metadata = json.loads(
                                metadata_path.read_text(encoding="utf-8")
                            )
                            pid = metadata.get("pid")
                            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                                child = child_pid_state(pid)
                        except (OSError, ValueError):
                            pass
                        write_daemon_diagnostics(
                            "e2e-daemon-wait-stopped",
                            {
                                "kind": "child-stop-timeout",
                                "elapsedSeconds": time.monotonic() - started,
                                "child": child,
                                "daemonLog": daemon_log_summary(
                                    self.root / "state"
                                ),
                            },
                        )
                        raise AssertionError(
                            f"daemon did not finish teardown within {timeout:g}s"
                        ) from error
                    time.sleep(0.05)
                    continue
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return

    def sigterm(self) -> None:
        metadata_path = self.root / "state" / "daemon.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        pid = metadata.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise AssertionError(f"daemon metadata has invalid pid: {metadata}")
        os.kill(pid, signal.SIGTERM)
        socket_path = self.root / "state" / "daemon.sock"
        self.wait(lambda: socket_path.exists(), lambda exists: exists is False)

    def wait(
        self,
        probe: Callable[[], Any],
        accept: Callable[[Any], bool],
        timeout: float = 12.0,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        transient: ipc_errors.TransientDaemonError | None = None
        while time.monotonic() < deadline:
            try:
                last = probe()
            except ipc_errors.TransientDaemonError as error:
                # One restart-window blip must not fail the scenario; the
                # deadline -- not the first failure -- ends the wait.
                transient = error
                time.sleep(0.1)
                continue
            transient = None
            if accept(last):
                return last
            time.sleep(0.1)
        detail = f"; last transient failure: {transient}" if transient else ""
        raise AssertionError(
            f"condition did not become true; last={bounded_repr(last)}{detail}"
        )
