"""HYPRIAL_NETWORK_ISOLATED: a test daemon that cannot leave this machine.

The 2026-09-16 ban on "isolated" test daemons: isolation by HOME +
HYPRIAL_PEER_DISCOVERY=0 left forwarding (which runs before the discovery
switch and joins the tailnet through a sidecar), zenoh's default listener and
the usage fetcher open.  This switch closes them and the daemon reports what
it EFFECTIVELY did.

The positive control is the point of this file: the same daemon, with peer
discovery requested and a discovered peer available, DIALS it when the switch
is off and does not when it is on -- judged by an attempt reaching (or not
reaching) a sentinel socket, never by the absence of a log line.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import ast
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import hyprial.kernel.impl.primitives.node_identity as node_identity_module
from hyprial.daemon.impl.application.netendpoints.endpoints import ISOLATED_DEFAULT_LISTEN,NETWORK_ISOLATED_ENV,_isolated_endpoints,network_isolated_from_environment
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies, usage_factory


def _ipc_ps(state: Path) -> dict[str, Any]:
    """Read the daemon ``ps`` payload over the public IPC seam.

    R7 deleted the top-level ``ps`` CLI command; this test needs the top-level
    ``zenoh`` block, which ``daemon status`` does not carry, so it talks IPC.
    """

    from uuid import uuid4

    frame = {"version": 1, "id": uuid4().hex, "method": "ps", "params": {}}
    deadline = time.monotonic() + 15.0
    while True:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(60)
            client.connect(str(state / "daemon.sock"))
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    raise AssertionError("daemon disconnected before IPC response")
                buffer.extend(chunk)
        response = json.loads(buffer.partition(b"\n")[0])
        result = response.get("result")
        if isinstance(result, dict):
            return result
        assert time.monotonic() < deadline, response
        time.sleep(0.2)

# --- the flag fails closed once set -----------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("on", True), ("true", True), ("0", False), ("off", False)])
def test_a_set_flag_is_parsed_strictly(monkeypatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv(NETWORK_ISOLATED_ENV, raw)
    assert network_isolated_from_environment() is expected


def test_unset_means_not_isolated_as_in_production(monkeypatch) -> None:
    monkeypatch.delenv(NETWORK_ISOLATED_ENV, raising=False)
    assert network_isolated_from_environment() is False


@pytest.mark.parametrize("raw", ["", "   ", "yes please", "isolated"])
def test_empty_or_mistyped_refuses_instead_of_meaning_off(monkeypatch, raw: str) -> None:
    """`export HYPRIAL_NETWORK_ISOLATED=` or an unfilled template must not
    quietly run a networked test daemon (the shared zenoh parser treats an
    empty value as unset; this one does not)."""

    monkeypatch.setenv(NETWORK_ISOLATED_ENV, raw)
    with pytest.raises(ValueError, match=NETWORK_ISOLATED_ENV):
        network_isolated_from_environment()


# --- endpoint rules ---------------------------------------------------------


def _no_forwarding(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("HYPRIAL_FORWARDING_"):
            monkeypatch.delenv(name)


def test_isolation_listens_on_explicit_loopback_by_default(monkeypatch) -> None:
    _no_forwarding(monkeypatch)
    assert _isolated_endpoints((), ()) == ((ISOLATED_DEFAULT_LISTEN,), ())


@pytest.mark.parametrize(
    "endpoint",
    ["tcp/127.0.0.1:7447", "tcp/[::1]:0", "tcp/localhost:9000"],
)
def test_loopback_endpoints_are_accepted(monkeypatch, endpoint: str) -> None:
    _no_forwarding(monkeypatch)
    assert _isolated_endpoints((endpoint,), (endpoint,)) == ((endpoint,), (endpoint,))


@pytest.mark.parametrize(
    ("listen", "connect"),
    [
        (("tcp/100.64.10.3:7447",), ()),
        (("tcp/0.0.0.0:7447",), ()),  # the IPv4 wildcard
        (("tcp/[::]:0",), ()),  # the IPv6 wildcard
        ((), ("tcp/10.0.0.2:7447",)),  # a remote dial
    ],
)
def test_non_loopback_endpoints_are_refused(monkeypatch, listen, connect) -> None:
    _no_forwarding(monkeypatch)
    with pytest.raises(ValueError, match="non-loopback"):
        _isolated_endpoints(listen, connect)


@pytest.mark.parametrize(
    "name",
    [
        "HYPRIAL_FORWARDING_COMMAND",
        "HYPRIAL_FORWARDING_UP",
        "HYPRIAL_FORWARDING_SIDECAR",
        "HYPRIAL_FORWARDING_INBOUND_TARGET",
    ],
)
def test_forwarding_is_refused_by_presence_not_address(monkeypatch, name: str) -> None:
    """The sidecar's endpoints are loopback and tunnel to the tailnet, so an
    address check would wave it through."""

    _no_forwarding(monkeypatch)
    monkeypatch.setenv(name, "anything")
    with pytest.raises(ValueError, match=name):
        _isolated_endpoints((), ())


def test_from_environment_marks_the_daemon_and_drops_the_usage_fetcher(
    tmp_path: Path, monkeypatch
) -> None:
    _no_forwarding(monkeypatch)
    monkeypatch.setenv(NETWORK_ISOLATED_ENV, "1")
    monkeypatch.delenv("HYPRIAL_ZENOH_LISTEN", raising=False)
    monkeypatch.delenv("HYPRIAL_ZENOH_CONNECT", raising=False)
    monkeypatch.delenv("HYPRIAL_USAGE_DISABLE", raising=False)
    app = DaemonApplication.from_environment(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        usage_factory=usage_factory,
        **daemon_dependencies(),
    )
    assert app.network_isolated is True
    assert app.zenoh_listen == (ISOLATED_DEFAULT_LISTEN,)
    status = app._network_isolation_status()
    assert status["checks"]["usageFetchOff"] is True
    assert status["checks"]["endpointsLoopbackOnly"] is True


def test_daemon_explicit_empty_node_id_warns_and_reads_the_home_file(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    _no_forwarding(monkeypatch)
    monkeypatch.setenv(NETWORK_ISOLATED_ENV, "1")
    monkeypatch.setenv("HYPRIAL_NODE_ID", " \t ")
    monkeypatch.delenv("HYPRIAL_ZENOH_LISTEN", raising=False)
    monkeypatch.delenv("HYPRIAL_ZENOH_CONNECT", raising=False)
    home = node_identity_module.configured_hyprial_home()[0]
    node_identity_module.node_id_path(home).write_text(" node-a \n", encoding="utf-8")

    app = DaemonApplication.from_environment(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        usage_factory=usage_factory,
        **daemon_dependencies(),
    )

    assert app.node_id == "node-a"
    assert [
        record.getMessage()
        for record in caplog.records
        if "HYPRIAL_NODE_ID is empty" in record.getMessage()
    ] == [
        "HYPRIAL_NODE_ID is empty after trimming; ignoring it and reading "
        "the home's node-id file"
    ]


# --- positive control: a real daemon, discovery requested, a peer available --


class _Sentinel:
    """A loopback listener standing in for a discovered peer; records dials."""

    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.socket.close()


def _short_root() -> str:
    configured = os.environ.get("HYPRIAL_TEST_TMP_ROOT")
    if configured:
        # CI points this at the /ci-tmpfs RAM disk: daemons started on the
        # shared btrfs stall on its commits during startup (infra-ops,
        # 2026-09-28).  Locally it is unset and the lookup below is unchanged.
        return os.path.realpath(configured)
    for candidate in ("/private/tmp", "/tmp"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    return tempfile.gettempdir()


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_daemon(root: Path, *, isolated: bool, sentinel: _Sentinel, observe: float) -> dict[str, Any]:
    home, state, bin_dir, logs = root / "home", root / "state", root / "bin", root / "logs"
    for directory in (home, state, bin_dir, logs):
        directory.mkdir(parents=True, exist_ok=True)
    discovery_log = logs / "discovery.log"
    # The discovery backend command (HYPRIAL_PEER_DISCOVERY_COMMAND): logs and
    # returns the sentinel's endpoint on a free port.
    discovery = _write_executable(
        bin_dir / "discover",
        "#!/bin/sh\n"
        f'echo called >> "{discovery_log}"\n'
        f'echo "tcp/127.0.0.1:{sentinel.port}"\n',
    )
    from tests.support.runtime.environment import assert_root_local_environment, isolated_environment

    environment = isolated_environment(root)
    environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}",
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_OWNER": "net-isolated-owner",
            "HYPRIAL_NODE_ID": f"net-isolated-{'on' if isolated else 'off'}",
            # Discovery REQUESTED in both runs: only the switch differs.
            "HYPRIAL_PEER_DISCOVERY": "1",
            "HYPRIAL_PEER_DISCOVERY_COMMAND": str(discovery),
            # The control run must not reach vendors or bind all interfaces
            # either: usage off, and an explicit loopback listener.
            "HYPRIAL_USAGE_DISABLE": "1",
            NETWORK_ISOLATED_ENV: "1" if isolated else "0",
        }
    )
    if not isolated:
        environment["HYPRIAL_ZENOH_LISTEN"] = "tcp/127.0.0.1:0"
    assert_root_local_environment(root, environment)
    daemon = subprocess.Popen(
        [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not (state / "daemon.sock").exists():
            assert daemon.poll() is None, "daemon exited during startup"
            assert time.monotonic() < deadline, "daemon socket never appeared"
            time.sleep(0.1)
        # Observe for a window, stopping early once the control has dialed.
        until = time.monotonic() + observe
        while time.monotonic() < until and not (not isolated and sentinel.accepted):
            time.sleep(0.2)
        status = _ipc_ps(state)
    finally:
        if daemon.poll() is None:
            os.kill(daemon.pid, signal.SIGTERM)
        try:
            daemon.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.kill(daemon.pid, signal.SIGKILL)
            daemon.wait(timeout=5)
    return {
        "accepted": sentinel.accepted,
        "discoveryCalls": discovery_log.read_text().splitlines() if discovery_log.exists() else [],
        "zenoh": status.get("zenoh", {}),
    }


def test_the_switch_is_the_only_difference_between_dialing_and_not() -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-netiso-", dir=_short_root()) as raw:
        root = Path(raw)
        control_sentinel, isolated_sentinel = _Sentinel(), _Sentinel()
        try:
            control = _run_daemon(root / "c", isolated=False, sentinel=control_sentinel, observe=20)
            isolated = _run_daemon(root / "i", isolated=True, sentinel=isolated_sentinel, observe=8)
        finally:
            control_sentinel.close()
            isolated_sentinel.close()

    # Control: discovery ran and the daemon actually dialed the peer.
    assert control["discoveryCalls"], control
    assert control["accepted"] >= 1, control
    assert control["zenoh"]["isolated"] is False
    # Isolated: nothing reached the peer, discovery was never consulted, and
    # the daemon reports the isolation as effective.
    assert isolated["accepted"] == 0, isolated
    assert isolated["discoveryCalls"] == [], isolated
    assert isolated["zenoh"]["isolated"] is True, isolated["zenoh"]
    assert isolated["zenoh"]["listen"] == [ISOLATED_DEFAULT_LISTEN]


# --- the contract runner refuses homes that would take an uncovered path ----

_SCENARIOS = Path(__file__).resolve().parents[3] / 'contract/e2e'
if str(_SCENARIOS) not in sys.path:
    sys.path.insert(0, str(_SCENARIOS))


def _runner(tmp_path: Path):
    from _daemon_runner import IsolatedDaemon  # noqa: PLC0415 - scenario helper

    (tmp_path / "home").mkdir(exist_ok=True)
    return IsolatedDaemon(Path(sys.executable), tmp_path, "trigger-check")


def test_a_clean_isolated_home_has_no_external_triggers(tmp_path: Path) -> None:
    _runner(tmp_path).require_no_external_triggers()


@pytest.mark.parametrize(
    ("relative", "content", "expected"),
    [
        ("channels.json", {"version": 1, "gateways": [{"name": "x"}]}, "Feishu gateway"),
        ("secrets/lark-x.json", {"appSecret": "redacted"}, "Feishu credentials"),
        ("settings.json", {"version": 1, "autoUpgrade": True}, "autoUpgrade"),
    ],
)
def test_a_home_that_would_reach_the_network_is_refused(
    tmp_path: Path, relative: str, content: dict, expected: str
) -> None:
    runner = _runner(tmp_path)
    target = tmp_path / "home" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(AssertionError, match=expected):
        runner.require_no_external_triggers()


def test_an_isolated_home_without_an_owner_is_refused(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.env.pop("HYPRIAL_OWNER", None)
    with pytest.raises(AssertionError, match="tailscale whoami"):
        runner.require_no_external_triggers()


def _seed_feishu(tmp_path: Path, name: str, app_id: str) -> None:
    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    channels = home / "channels.json"
    gateways = json.loads(channels.read_text())["gateways"] if channels.exists() else []
    gateways.append({"provider": "lark", "name": name, "appId": app_id,
                     "credentialRef": f"lark-{name}", "routes": []})
    channels.write_text(json.dumps({"version": 1, "gateways": gateways}))
    (home / "secrets" / f"lark-{name}.json").write_text('{"appSecret": "redacted"}')


def test_a_declared_scenario_may_load_its_listed_test_app(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    _seed_feishu(tmp_path, "allenwoods-hyprial-e2e", "cli_aaf9ea19d4b8dbe4")
    runner.allow_feishu_test_apps("run-all-harness-lifecycle.py")
    runner.require_no_external_triggers()


def test_a_declared_scenario_may_not_load_an_unlisted_app(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    _seed_feishu(tmp_path, "allenwoods-hyprial-e2e", "cli_aaf9ea19d4b8dbe4")
    _seed_feishu(tmp_path, "someone-else", "cli_not_a_test_app")
    runner.allow_feishu_test_apps("run-all-harness-lifecycle.py")
    with pytest.raises(AssertionError, match="not a test App listed for run-all-harness-lifecycle.py"):
        runner.require_no_external_triggers()


def test_a_declared_scenario_may_not_carry_a_stray_credential(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    _seed_feishu(tmp_path, "allenwoods-hyprial-e2e", "cli_aaf9ea19d4b8dbe4")
    (tmp_path / "home" / "secrets" / "lark-stray.json").write_text("{}")
    runner.allow_feishu_test_apps("run-all-harness-lifecycle.py")
    with pytest.raises(AssertionError, match="lark-stray.json belongs to no listed"):
        runner.require_no_external_triggers()


def test_an_unlisted_scenario_cannot_declare_itself(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="not in FEISHU_EXEMPT_SCENARIOS"):
        _runner(tmp_path).allow_feishu_test_apps("run-something-new.py")


def test_a_replaced_lark_worker_may_load_gateways_undeclared(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    _seed_feishu(tmp_path, "four-lark-1", "isolated_contract_1")
    runner.env["HYPRIAL_LARK_WORKER_COMMAND"] = json.dumps(["/bin/true"])
    runner.require_no_external_triggers()


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("fake_worker", [True, False])
@pytest.mark.parametrize("as_secret_only", [True, False])
def test_a_production_bot_is_refused_in_every_case(
    tmp_path: Path, declared: bool, fake_worker: bool, as_secret_only: bool
) -> None:
    runner = _runner(tmp_path)
    if as_secret_only:
        (tmp_path / "home" / "secrets").mkdir(parents=True)
        (tmp_path / "home" / "secrets" / "lark-hq-adjutant-feishu.json").write_text("{}")
    else:
        # Even when it carries a listed test App's id.
        _seed_feishu(tmp_path, "hq-adjutant-feishu", "cli_aaf9ea19d4b8dbe4")
    if declared:
        runner.allow_feishu_test_apps("run-all-harness-lifecycle.py")
    if fake_worker:
        runner.env["HYPRIAL_LARK_WORKER_COMMAND"] = json.dumps(["/bin/true"])
    with pytest.raises(AssertionError, match="production Feishu bot.*hq-adjutant-feishu"):
        runner.require_no_external_triggers()


def _documented_exempt_scenarios() -> set[str]:
    doc = (Path(__file__).resolve().parents[3] / 'docs/guides/network-isolation.md').read_text()
    start, end = "<!-- feishu-exempt-scenarios:start -->", "<!-- feishu-exempt-scenarios:end -->"
    assert doc.count(start) == 1 and doc.count(end) == 1, "exempt-table markers missing"
    table = doc.split(start, 1)[1].split(end, 1)[0]
    return set(re.findall(r"^\s*\| `(run-[^`]+\.py)`", table, flags=re.MULTILINE))


def test_the_exempt_list_in_code_and_in_the_doc_is_the_same_set() -> None:
    from _daemon_runner import FEISHU_EXEMPT_SCENARIOS  # noqa: PLC0415 - scenario helper

    documented = _documented_exempt_scenarios()
    assert FEISHU_EXEMPT_SCENARIOS, "an empty exempt list would pass every comparison"
    assert documented, "the doc table parsed to nothing"
    assert documented == set(FEISHU_EXEMPT_SCENARIOS)


_EXTRACTED_FEISHU_BINDING = (
    "run-all-harness-lifecycle.py",
    "_harness_lifecycle/scenario.py",
)
_EXTRACTED_UPGRADE_BINDING = (
    "run-upgrade-continuity.py",
    "_upgrade_continuity/scenario.py",
    "_upgrade_continuity",
)


def _function_map(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _entry_injects_original_filename(
    entry: ast.Module, module: str, helper_name: str = "scenario"
) -> bool:
    alias = "_scenario_impl"
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == module
        and any(item.name == helper_name and item.asname == alias for item in node.names)
        for node in entry.body
    )
    main = _function_map(entry).get("main")
    if not imported or main is None:
        return False
    executable = any(
        isinstance(node, ast.If)
        and ast.unparse(node.test) == "__name__ == '__main__'"
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "main"
            for statement in node.body
            for child in ast.walk(statement)
        )
        for node in entry.body
    )
    if not executable:
        return False
    statements = [
        statement for statement in main.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(statements) != 1 or not isinstance(statements[0], ast.Return):
        return False
    node = statements[0].value
    if not isinstance(node, ast.Call) or ast.unparse(node.func) != f"{alias}.main":
        return False
    if node.args and isinstance(node.args[0], ast.Dict):
        injected = {
            ast.unparse(key): ast.unparse(value)
            for key, value in zip(node.args[0].keys, node.args[0].values)
            if key is not None
        }
        if injected.get("'__file__'") == "__file__":
            return True
    return False


def _has_upgrade_guard_binding(entry_source: str, owner_source: str) -> bool:
    module = _EXTRACTED_UPGRADE_BINDING[2]
    try:
        entry = ast.parse(entry_source)
        owner = ast.parse(owner_source)
    except SyntaxError:
        return False
    if not _entry_injects_original_filename(entry, module):
        return False
    entry_main = _function_map(entry).get("main")
    owner_main = _function_map(owner).get("main")
    if entry_main is None or owner_main is None:
        return False
    injected_guard = any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) == "_scenario_impl.main"
        and node.args
        and isinstance(node.args[0], ast.Dict)
        and any(
            key is not None
            and ast.unparse(key) == "'refuse_unisolatable_daemon'"
            and ast.unparse(value) == "refuse_unisolatable_daemon"
            for key, value in zip(node.args[0].keys, node.args[0].values)
        )
        for node in ast.walk(entry_main)
    )
    guard = any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) == "_entry['refuse_unisolatable_daemon']"
        and [ast.unparse(argument) for argument in node.args]
        == ["'upgrade-continuity'", "env"]
        for node in ast.walk(owner_main)
    )
    fake_worker_uses_runner = any(
        isinstance(node, ast.Call)
        and ast.unparse(node) == "str(_entry['Path'](_entry['__file__']).resolve())"
        for node in ast.walk(owner_main)
    )
    return injected_guard and guard and fake_worker_uses_runner


def _has_lifecycle_feishu_declaration(entry_source: str, owner_source: str) -> bool:
    try:
        entry = ast.parse(entry_source)
        owner = ast.parse(owner_source)
    except SyntaxError:
        return False
    owner_main = _function_map(owner).get("main")
    if owner_main is None:
        return False
    if not _entry_injects_original_filename(entry, "_harness_lifecycle"):
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "allow_feishu_test_apps"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "daemon"
        and len(node.args) == 1
        and ast.unparse(node.args[0]) == "_entry['Path'](_entry['__file__']).name"
        for node in ast.walk(owner_main)
    )


def test_every_exempt_scenario_exists_and_declares_itself() -> None:
    from _daemon_runner import (  # noqa: PLC0415 - scenario helper
        FEISHU_EXEMPT_SCENARIOS,
        PRODUCTION_FEISHU_APPS,
    )

    assert FEISHU_EXEMPT_SCENARIOS
    assert _EXTRACTED_FEISHU_BINDING[0] in FEISHU_EXEMPT_SCENARIOS
    for name, apps in FEISHU_EXEMPT_SCENARIOS.items():
        if name == _EXTRACTED_FEISHU_BINDING[0]:
            entry_source = (_SCENARIOS / name).read_text(encoding="utf-8")
            owner_source = (_SCENARIOS / _EXTRACTED_FEISHU_BINDING[1]).read_text(encoding="utf-8")
            assert _has_lifecycle_feishu_declaration(entry_source, owner_source), name
        else:
            source = (_SCENARIOS / name).read_text(encoding="utf-8")
            assert "allow_feishu_test_apps(Path(__file__).name)" in source, name
        assert apps and not apps & set(PRODUCTION_FEISHU_APPS), name


def test_feishu_policy_reader_rejects_comments_wrong_owner_and_missing_injection() -> None:
    entry_path = _SCENARIOS / "run-all-harness-lifecycle.py"
    owner_path = _SCENARIOS / "_harness_lifecycle/scenario.py"
    entry_source = entry_path.read_text(encoding="utf-8")
    owner_source = owner_path.read_text(encoding="utf-8")
    commented = owner_source.replace(
        "daemon.allow_feishu_test_apps(_entry['Path'](_entry['__file__']).name)",
        "# daemon.allow_feishu_test_apps(_entry['Path'](_entry['__file__']).name)",
    )
    assert not _has_lifecycle_feishu_declaration(entry_source, commented)
    assert not _has_lifecycle_feishu_declaration(
        entry_source.replace("from _harness_lifecycle import scenario as _scenario_impl", ""),
        owner_source,
    )
    assert not _has_lifecycle_feishu_declaration(
        entry_source.replace("return _scenario_impl.main({", "return 0 # detached helper\n    return _scenario_impl.main({"),
        owner_source,
    )
    assert not _has_lifecycle_feishu_declaration(
        entry_source.replace("'__file__': __file__,", ""),
        owner_source,
    )
    assert not _has_lifecycle_feishu_declaration(
        entry_source,
        owner_source.replace("_entry['__file__']).name", "'wrong-runner.py').name"),
    )
    assert not _has_lifecycle_feishu_declaration(
        entry_source.replace(
            "from _harness_lifecycle import scenario as _scenario_impl",
            "from _harness_lifecycle import configuration as _scenario_impl",
        ),
        owner_source,
    )
    upgrade_entry = (_SCENARIOS / _EXTRACTED_UPGRADE_BINDING[0]).read_text(encoding="utf-8")
    upgrade_owner_path = _SCENARIOS / _EXTRACTED_UPGRADE_BINDING[1]
    upgrade_owner = upgrade_owner_path.read_text(encoding="utf-8")
    assert _has_upgrade_guard_binding(upgrade_entry, upgrade_owner)
    assert not _has_upgrade_guard_binding(
        upgrade_entry,
        upgrade_owner.replace(
            "        refused = _entry['refuse_unisolatable_daemon'](\"upgrade-continuity\", env)",
            "        refused = None",
        ),
    )
    assert not _has_upgrade_guard_binding(
        upgrade_entry,
        upgrade_owner.replace(
            "str(_entry['Path'](_entry['__file__']).resolve())",
            "str(_entry['Path']('wrong-runner.py').resolve())",
        ),
    )


def _scripted_ps(runner, answers: list[dict]) -> list[str]:
    """Make ``runner.ps(...)`` return ``answers`` in order (last repeats)."""

    calls: list[str] = []

    def ps(*, transient_timeout: float = 0.0) -> dict:
        calls.append("ps")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    runner.ps = ps
    return calls


_RESTORING = {"ok": True, "restoring": True, "daemon": {"phase": "restoring"}}


def test_a_restoring_ps_is_waited_out_then_the_full_ps_is_judged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from _runner_shared import ps as ps_module  # noqa: PLC0415 - scenario helper

    answers = [
        {"restorePending": True, "phase": "restoring"},
        {"restorePending": True, "phase": "restoring"},
        {"zenoh": {"isolated": True}},
    ]
    seen: list[str] = []

    def fake_request(_socket_path, method: str, _params: dict) -> dict:
        seen.append(method)
        if method == "ps":
            return answers[0]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(ps_module, "_request", fake_request)
    result = ps_module.ps_from_socket(tmp_path / "daemon.sock", transient_timeout=30.0)

    assert result["zenoh"]["isolated"] is True
    assert seen == ["ping", "ping", "ping", "ps"]


def test_a_daemon_still_restoring_at_the_deadline_is_refused(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    _scripted_ps(runner, [_RESTORING])
    with pytest.raises(AssertionError, match="still restoring after 0.6s"):
        runner.require_network_isolated(restore_timeout=0.6)


def test_a_full_ps_without_isolation_is_refused_at_once(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    calls = _scripted_ps(runner, [{"ok": True, "zenoh": {"isolated": False}}])
    with pytest.raises(AssertionError, match=r"does not report network isolation \(zenoh"):
        runner.require_network_isolated(restore_timeout=30.0)
    assert calls == ["ps"]


def test_the_runner_sets_the_switch() -> None:
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as raw:
        assert _runner(Path(raw)).env[NETWORK_ISOLATED_ENV] == "1"


# --- bypass variables cannot travel through the gate; the gate reports INCOMPLETE ---


def _gate():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_isolated_gate", _SCENARIOS / "run-isolated-gate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gate_never_passes_a_bypass_to_a_scenario(monkeypatch) -> None:
    from _daemon_runner import BYPASS_ENVIRONMENT  # noqa: PLC0415

    for name in BYPASS_ENVIRONMENT:
        monkeypatch.setenv(name, "1")
    environment = _gate().scenario_environment("E2E-005")
    assert not (set(environment) & BYPASS_ENVIRONMENT)


def test_every_bypass_a_scenario_reads_is_registered() -> None:
    """Exhaustive: a new HYPRIAL_E2E_ALLOW_* read anywhere in the scenarios but
    missing from BYPASS_ENVIRONMENT would reach scenarios through the gate."""

    import re

    from _daemon_runner import BYPASS_ENVIRONMENT  # noqa: PLC0415

    used = set()
    for path in _SCENARIOS.glob("*.py"):
        used |= set(re.findall(r"HYPRIAL_E2E_ALLOW_[A-Z_]+", path.read_text(encoding="utf-8")))
    assert used, "the scan found no bypass names at all -- it is looking in the wrong place"
    assert used <= BYPASS_ENVIRONMENT, used - BYPASS_ENVIRONMENT


def test_refused_scenarios_make_the_gate_incomplete_not_pass(monkeypatch, capsys, tmp_path) -> None:
    gate = _gate()
    monkeypatch.setattr(gate, "diag_dir", lambda: tmp_path / "diag")
    codes = {"E2E-001": 0, "E2E-005": 3}
    monkeypatch.setattr(
        gate, "run_scenario", lambda scenario, script, **_: codes[scenario]
    )
    assert gate.main(["E2E-001", "E2E-005"]) == gate.INCOMPLETE_EXIT
    out = capsys.readouterr().out
    assert "RESULT INCOMPLETE GATE" in out and "E2E-005=REFUSED" in out
    assert "RESULT PASS GATE" not in out


# --- the refusal cannot be opened on a production node ----------------------


def test_a_live_production_daemon_refuses_even_with_the_override(monkeypatch, tmp_path) -> None:
    from _daemon_runner import ALLOW_UNISOLATED_ENV, refuse_unisolatable_daemon  # noqa: PLC0415

    home = Path(tempfile.mkdtemp(prefix="hp-", dir=_short_root()))
    state = home / ".hyprial" / "state"
    state.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(state / "daemon.sock"))
    listener.listen(1)
    try:
        monkeypatch.setenv(ALLOW_UNISOLATED_ENV, "1")
        # A non-test-node launch dict: the production-node refusal still holds.
        assert refuse_unisolatable_daemon("probe", {}, home=home) == 3
        # ...and an e2e test node runs on the same production node.
        assert refuse_unisolatable_daemon("probe", dict(_TEST_NODE), home=home) is None
    finally:
        listener.close()
        (state / "daemon.sock").unlink()


def test_no_production_daemon_still_needs_the_explicit_override(monkeypatch, tmp_path) -> None:
    from _daemon_runner import ALLOW_UNISOLATED_ENV, refuse_unisolatable_daemon  # noqa: PLC0415

    monkeypatch.delenv(ALLOW_UNISOLATED_ENV, raising=False)
    assert refuse_unisolatable_daemon("probe", {}, home=tmp_path) == 3
    monkeypatch.setenv(ALLOW_UNISOLATED_ENV, "1")
    assert refuse_unisolatable_daemon("probe", {}, home=tmp_path) is None


_TEST_NODE = {
    "HYPRIAL_NODE_ID": "e2e-upgrade-continuity",
    "HYPRIAL_OWNER": "e2e",
    "HYPRIAL_ZENOH_LISTEN": "tcp/127.0.0.1:47123",
}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Prefix: partial, case and boundary bypasses.
        ("HYPRIAL_NODE_ID", "xe2e-node"),
        ("HYPRIAL_NODE_ID", "E2E-node"),
        ("HYPRIAL_NODE_ID", "e2E-node"),
        ("HYPRIAL_NODE_ID", "e2e-"),
        ("HYPRIAL_NODE_ID", "e2e"),
        ("HYPRIAL_NODE_ID", "e2e_node"),
        ("HYPRIAL_NODE_ID", "e2e-Node"),
        ("HYPRIAL_NODE_ID", "e2e-node\n"),
        ("HYPRIAL_NODE_ID", " e2e-node"),
        ("HYPRIAL_NODE_ID", "e2e--node"),
        ("HYPRIAL_NODE_ID", "e2e-node/x"),
        ("HYPRIAL_NODE_ID", "mac-studio"),
        # Owner: must be exactly "e2e".
        ("HYPRIAL_OWNER", None),
        ("HYPRIAL_OWNER", "E2E"),
        ("HYPRIAL_OWNER", "e2e "),
        ("HYPRIAL_OWNER", "allen"),
        # Listen: loopback only, never the tailnet address.
        ("HYPRIAL_ZENOH_LISTEN", None),
        ("HYPRIAL_ZENOH_LISTEN", "tcp/100.64.10.3:7447"),
        ("HYPRIAL_ZENOH_LISTEN", "tcp/0.0.0.0:7447"),
        ("HYPRIAL_ZENOH_LISTEN", "tcp/127.0.0.1:0"),
        ("HYPRIAL_ZENOH_LISTEN", "tcp/127.0.0.1:47123,tcp/100.64.10.3:7447"),
    ],
)
def test_anything_short_of_a_test_node_is_refused_on_a_production_node(
    monkeypatch, key: str, value: str | None
) -> None:
    import _daemon_runner  # noqa: PLC0415 - scenario helper

    monkeypatch.setattr(_daemon_runner, "production_daemon_alive", lambda home=None: True)
    monkeypatch.setenv(_daemon_runner.ALLOW_UNISOLATED_ENV, "1")
    environment = dict(_TEST_NODE)
    if value is None:
        environment.pop(key)
    else:
        environment[key] = value
    assert _daemon_runner.e2e_test_node_problems(environment)
    assert _daemon_runner.refuse_unisolatable_daemon("probe", environment) == 3


def test_a_test_node_runs_on_a_production_node_without_any_switch(monkeypatch) -> None:
    import _daemon_runner  # noqa: PLC0415 - scenario helper

    monkeypatch.setattr(_daemon_runner, "production_daemon_alive", lambda home=None: True)
    monkeypatch.delenv(_daemon_runner.ALLOW_UNISOLATED_ENV, raising=False)
    assert _daemon_runner.e2e_test_node_problems(dict(_TEST_NODE)) == []
    assert _daemon_runner.refuse_unisolatable_daemon("probe", dict(_TEST_NODE)) is None


@pytest.mark.parametrize(
    ("script", "node_id"),
    [
        ("run-upgrade-continuity.py", "e2e-upgrade-continuity"),
        ("run-uv-install-flow.py", "e2e-uv-install-flow"),
    ],
)
def test_e2e005_and_e2e010_launch_as_test_nodes(script: str, node_id: str) -> None:
    """The two gated scenarios pass the gate on macos-host (a production node).

    Their launch dicts are built inline in main(), so this pins the three
    identity lines in the source and that the gate is handed that dict.
    """

    import _daemon_runner  # noqa: PLC0415 - scenario helper

    source = (_SCENARIOS / script).read_text(encoding="utf-8")
    assert _daemon_runner.E2E_TEST_NODE_ID.fullmatch(node_id)
    assert f'"{node_id}"' in source
    if script == "run-upgrade-continuity.py":
        assert "return _scenario_impl.main(" in source
        assert "'free_tcp_endpoint': free_tcp_endpoint," in source
        assert "'refuse_unisolatable_daemon': refuse_unisolatable_daemon," in source
        assert "'NODE_ID': NODE_ID," in source
        implementation = (_SCENARIOS / _EXTRACTED_UPGRADE_BINDING[1]).read_text(encoding="utf-8")
        assert script == _EXTRACTED_UPGRADE_BINDING[0]
        assert _has_upgrade_guard_binding(source, implementation)
        assert '"HYPRIAL_OWNER": "e2e"' in implementation
        assert '"HYPRIAL_NODE_ID": _entry[\'NODE_ID\']' in implementation
        assert '"HYPRIAL_ZENOH_LISTEN": _entry[\'free_tcp_endpoint\']()' in implementation
        assert '_entry[\'refuse_unisolatable_daemon\']("upgrade-continuity", env)' in implementation
    else:
        assert '"HYPRIAL_OWNER": "e2e"' in source
        assert '"HYPRIAL_ZENOH_LISTEN": free_tcp_endpoint()' in source
        assert re.search(r'refuse_unisolatable_daemon\("[a-z-]+", (env|environment)\)', source)
    # free_tcp_endpoint() is always a test-node listen.
    assert not _daemon_runner.e2e_test_node_problems(
        {**_TEST_NODE, "HYPRIAL_ZENOH_LISTEN": _daemon_runner.free_tcp_endpoint()}
    )
