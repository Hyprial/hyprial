import ast
import json
from pathlib import Path
import runpy

import pytest

from hyprial.daemon.impl.forwarding.controller import ForwardingSidecarController


def test_device_fixture_uses_owner_path_and_current_address_protocol():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / "contract/e2e/run-orgfs-forwarding-redial.py"))
    calls = []

    class Daemon:
        def command(self, *args):
            calls.append(args)

    address = "tc-e2e-unix:/tmp/hr-fixture/b/zenoh.sock"
    runner["_plant_directory_device"](
        Daemon(), "space", device_id="node-b", owner="redial-b", address=address,
    )
    assert calls[0] == ("org", "fs", "mkdir", "space", "directory/devices/redial-b", "--json")
    assert calls[1][:5] == ("org", "fs", "write", "space", "directory/devices/redial-b/node-b.json")
    assert json.loads(calls[1][5])["owner"] == "redial-b"
    controller = ForwardingSidecarController.__new__(ForwardingSidecarController)

    slot = "/run/hyprial/slots/slot-00"

    def request(command, expected):
        assert command["v"] == 6 and expected == "peer-mapped"
        assert command["slot"] == slot and command["address"] == address
        return {"peer": command["peer"], "slot": slot}

    controller._request = request
    assert controller.map_peer("node-b", address, slot) == slot


def test_directory_fixture_peers_keep_distinct_keys_from_the_authority():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / "contract/e2e/run-orgfs-forwarding-redial.py"))
    records = []

    class Daemon:
        def command(self, *args):
            if args[:3] == ("org", "fs", "write"):
                records.append(json.loads(args[5]))

    for device in ("orgfs-redial-a", "000-pool-one", "001-pool-two"):
        runner["_plant_directory_device"](Daemon(), "space", device_id=device,
                                         owner="redial-a", address="tc-e2e-unix:/tmp/hr-fixture/a.sock")
    assert len({row[key] for row in records for key in ("serverPublic", "clientPublic")}) == 6
    assert records[0]["clientPublic"] == "nodekey:" + "02" * 32


def test_outer_deadline_escapes_probe_retry():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / "contract/e2e/run-orgfs-forwarding-redial.py"))

    def expired():
        raise runner["ScenarioTimeout"]("outer deadline")

    with pytest.raises(runner["ScenarioTimeout"], match="outer deadline"):
        runner["_wait"]("probe", expired, lambda _value: False, timeout=1)


def test_failed_probe_cannot_satisfy_a_truthy_success_predicate():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / "contract/e2e/run-orgfs-forwarding-redial.py"))

    def failed():
        raise AssertionError("temporarily unavailable")

    with pytest.raises(AssertionError, match="did not complete"):
        runner["_wait"]("probe", failed, bool, timeout=0.01)


def test_interrupted_graceful_cleanup_still_stops_both_owned_pids(monkeypatch):
    from types import SimpleNamespace

    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / "contract/e2e/run-orgfs-forwarding-redial.py"))
    namespace = runner["_cleanup"].__globals__
    stopped = []
    monkeypatch.setitem(namespace, "_daemon_pid", lambda _daemon: 100)
    monkeypatch.setitem(namespace, "_pid_from", lambda _path: 200)
    monkeypatch.setitem(namespace, "_alive", lambda _pid: True)
    monkeypatch.setitem(namespace, "_stop_pid", lambda pid, **_kwargs: stopped.append(pid))

    def interrupted(*_args, **_kwargs):
        raise runner["ScenarioTimeout"]("cleanup interrupted")

    with pytest.raises(runner["ScenarioTimeout"], match="cleanup interrupted"):
        runner["_cleanup"](SimpleNamespace(node_id="owned", command=interrupted), Path("owned-sidecar.pid"))
    assert stopped == [100, 200]


def test_deadline_during_term_grace_still_kills_and_reaps_the_owned_child():
    import signal
    import subprocess
    import sys

    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / "contract/e2e/run-orgfs-forwarding-redial.py"))

    def expired(_signum, _frame):
        raise runner["ScenarioTimeout"]("cleanup deadline")

    with subprocess.Popen(
        [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True,
    ) as process:
        assert process.stdout.readline().strip() == "ready"
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 0.15)
        try:
            with pytest.raises(runner["ScenarioTimeout"], match="cleanup deadline"):
                runner["_stop_pid"](process.pid, label="owned ignore-TERM child")
            assert process.wait(timeout=1) == -signal.SIGKILL
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _hello_capabilities(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        event = keywords.get("event")
        capabilities = keywords.get("capabilities")
        if (
            isinstance(event, ast.Constant)
            and event.value == "hello"
            and capabilities is not None
        ):
            value = ast.literal_eval(capabilities)
            assert isinstance(value, list)
            return set(value)
    raise AssertionError(f"no literal hello capabilities in {path}")


def test_all_python_forwarding_stdio_stubs_pin_real_v6_capabilities():
    """Every active Python stdio double mirrors the v6 features it implements."""

    root = Path(__file__).resolve().parents[3]
    golden_hello = json.loads(
        (root / "contract/sidecar-tailcat/golden/events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    real = set(golden_hello["capabilities"])
    discovered = {
        path.relative_to(root).as_posix()
        for base in (root / "contract", root / "tests/fixtures", root / "sidecar")
        for path in base.rglob("*.py")
        if 'event="hello"' in path.read_text(encoding="utf-8")
        and "capabilities=[" in path.read_text(encoding="utf-8")
    }
    expected = {
        "contract/e2e/run-orgfs-forwarding-redial.py": {
            "map-peer",
            "unmap-peer",
            "tailnet-membership-v1",
            "tailnet-control-v1",
            "unix-inbound-v1",
            "forwarding-request-id-v1",
            "peer-slots-v1",
            "client-key-derivation-v1",
        },
        "tests/fixtures/network/fake_forwarding_sidecar.py": real
        - {"service-connect-v1", "derp-map-v1"},
    }
    assert discovered == set(expected)
    for relative, wanted in expected.items():
        advertised = _hello_capabilities(root / relative)
        fixture_only = {"derive"} if relative.startswith("tests/") else set()
        assert advertised - fixture_only == wanted
        assert advertised - fixture_only <= real
        if "client-key-derivation-v1" in advertised:
            assert "forwarding-request-id-v1" in advertised


@pytest.mark.parametrize("entrypoint,device_id,client", [
    ("run-orgfs-forwarding-redial.py", "orgfs-redial-a", "02" * 32),
    ("run-peer-restart-relearn.py", "restart-b", "ff" * 32),
    ("run-peer-restart-relearn.py", "orgfs-redial-a-case", "02" * 32),
])
def test_stdio_v6_private_unix_readiness_and_retired_allow_rejection(tmp_path, entrypoint, device_id, client):
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[3]
    runner = root / "contract/e2e" / entrypoint
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    up = {
        "v": 6, "op": "up", "requestId": "up", "owner": "redial-a",
        "deviceId": device_id, "membershipStore": str(private / "membership.json"),
        "keyFile": str(private / "key"), "addressFile": str(private / "address"),
        "inboundTarget": "unix:" + str(private / "absent.sock"), "slots": [],
    }
    commands = [up, {"v": 6, "op": "membership-status", "requestId": "members", "params": {}},
                {"v": 6, "op": "allow", "requestId": "old", "keys": [], "allowAny": True},
                {"v": 5, "op": "status", "requestId": "v5"},
                up | {"requestId": "tcp", "inboundTarget": "tcp:127.0.0.1:7447"},
                up | {"requestId": "allow-up", "allowAny": True},
                {"v": 6, "op": "membership-status", "requestId": "unknown", "params": {}, "extra": True},
                {"v": 6, "op": "membership-status", "requestId": "x" * 129, "params": {}},
                {"v": 6, "op": "down"}]
    process = subprocess.run(
        [sys.executable, str(runner), "--sidecar", str(tmp_path / "receipt"), str(tmp_path / "pid")],
        input="\n".join(json.dumps(row) for row in commands) + "\n",
        capture_output=True, text=True, timeout=10, env={**os.environ, "NO_PROXY": "127.0.0.1,localhost,::1"},
    )
    assert process.returncode == 0, process.stderr
    events = [json.loads(line) for line in process.stdout.splitlines()]
    golden = [json.loads(line) for line in (root / "contract/sidecar-tailcat/golden/events.jsonl").read_text().splitlines()]
    assert events[0]["tailcat"] == golden[0]["tailcat"]
    assert next(row for row in events if row.get("state") == "Starting") == golden[1]
    status = next(row for row in events if row.get("requestId") == "members")
    assert status == next(row for row in golden if row.get("requestId") == "members")
    running = next(row for row in events if row.get("state") == "Running")
    assert running["v"] == 6 and running["requestId"] == "up"
    assert running["clientPublic"] == "nodekey:" + client
    assert running["readiness"] == {"sidecar": True, "control": True, "member": False}
    assert not (private / "absent.sock").exists()
    assert next(row for row in events if row.get("requestId") == "old")["code"] == "BAD_REQUEST"
    assert next(row for row in events if row.get("requestId") == "v5")["code"] == "SIDECAR_PROTOCOL_MISMATCH"
    for request in ("tcp", "allow-up", "unknown", "x" * 129):
        assert next(row for row in events if row.get("requestId") == request)["code"] == "BAD_REQUEST"


@pytest.mark.parametrize("entrypoint,node_id", [
    ("run-orgfs-forwarding-redial.py", "orgfs-redial-a"),
    ("run-peer-restart-relearn.py", "restart-b"),
    ("run-peer-restart-relearn.py", "orgfs-redial-a-case"),
])
def test_private_fixture_configuration_matches_the_real_stdio_identity(entrypoint, node_id):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace
    from hyprial.identity import read_device_record

    root = Path(__file__).resolve().parents[3]
    runner = runpy.run_path(str(root / "contract/e2e" / entrypoint))
    from _orgfs_redial_fixture import scenario_root

    with scenario_root() as private:
        daemon = SimpleNamespace(root=private, node_id=node_id,
                                 env={"HYPRIAL_HOME": str(private / "home"), "HYPRIAL_OWNER": "fixture-owner"})
        runner["_configure_forwarding"](daemon, listen=f"unixsock-stream/{private}/zenoh.sock",
                                      receipt=private / "receipt", pid_path=private / "pid")
        up = json.loads(daemon.env["HYPRIAL_FORWARDING_UP"]) | {"requestId": "configured-up"}
        process = subprocess.run(
            json.loads(daemon.env["HYPRIAL_FORWARDING_COMMAND"]),
            input=json.dumps(up) + '\n{"v":6,"op":"down"}\n',
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost,::1"},
        )
        assert process.returncode == 0, process.stderr
        running = next(json.loads(line) for line in process.stdout.splitlines() if '"state":"Running"' in line)
        record = read_device_record(private / "home")
        assert running["clientPublic"] == record.client_public
        assert running["serverPublic"] == record.server_public
        assert running["readiness"] == {"sidecar": True, "control": True, "member": False}
        assert json.loads(daemon.env["HYPRIAL_FORWARDING_COMMAND"])[0] == sys.executable


def test_leased_roster_expiry_does_not_renew_on_status(tmp_path):
    import time
    import pytest

    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / "contract/e2e/run-orgfs-forwarding-redial.py"))
    source = tmp_path / "roster-fixture.json"
    rows = [runner["_fixture_member"]("redial-a", "node-a", "tc-e2e-unix:/tmp/hr-a.sock", local=True),
            runner["_fixture_member"]("redial-b", "node-b", "tc-e2e-unix:/tmp/hr-b.sock")]
    roster = {"tailnetId": "space", "authority": {key: rows[1][key] for key in (
        "owner", "deviceId", "masterPublic", "serverPublic", "address"
    )} | {"controlPort": 7448}, "revision": 1, "members": rows, "leaseSeconds": 300}
    source.write_text(json.dumps({"roster": roster, "startedMonotonic": time.monotonic() - 301}))
    fixture = runner["LeasedUnixFixture"](source)
    fixture.local = "node-a"
    try:
        for _ in range(2):
            status = fixture.status({})
            assert status["readiness"]["member"] is False
            assert status["tailnets"][0]["leaseRemainingSeconds"] == 0
            assert status["tailnets"][0]["code"] == "MEMBERSHIP_LEASE_EXPIRED"
        with pytest.raises(ValueError, match="unleased"):
            fixture.map_peer("node-b", rows[1]["address"], str(tmp_path / "slot"))
        source.write_text(json.dumps({"roster": roster, "startedMonotonic": time.monotonic()}))
        assert fixture.status({})["readiness"]["member"] is True
        restarted = runner["LeasedUnixFixture"](source)
        restarted.local = "node-a"
        try:
            assert restarted.status({})["readiness"]["member"] is False
            source.write_text(json.dumps({"roster": roster, "startedMonotonic": time.monotonic()}))
            assert restarted.status({})["readiness"]["member"] is True
            # Authority-local membership needs no remote lease, even after restart.
            restarted.local = "node-b"
            restarted.deadline = 0
            assert restarted.ready() is True
            assert restarted.status({})["tailnets"][0]["leaseRemainingSeconds"] is None
        finally:
            restarted.close()
    finally:
        fixture.close()
