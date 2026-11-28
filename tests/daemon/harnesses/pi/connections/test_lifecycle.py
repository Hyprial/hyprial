"""Actual Pi processes through daemon custody, supersession and stop paths."""

from dataclasses import replace
import json
import threading
import time

import pytest

from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.harnesses.pi.configuration.test_pi_controlled_loader import _real_p22_context
from .support import reservation, sdk_root
from .test_binding import binding


def wait_for(predicate):
    end = time.monotonic() + 15
    while not predicate():
        assert time.monotonic() < end
        time.sleep(0.01)


@pytest.fixture
def launch_context(tmp_path):
    registry, _, cwd, context, channel, complete = _real_p22_context(tmp_path)
    try:
        yield HarnessLaunchSpec("pi", "worker", True, args=("--approve",), cwd=str(cwd)), context, channel, complete
    finally:
        registry.close()


def make_process(spec, channel, complete, actor_host, *, shared_session=None):
    args = dict(command=(str(sdk_root() / "dist" / "cli.js"),), worker_channel=channel,
                complete_launch=complete, model_binding=binding())
    factory = None if shared_session is None else lambda: PiRpcClient(spec, shared_session, **args)
    return PiRpcProcess(spec, **args, actor_host=actor_host, client_factory=factory)


def test_daemon_process_stop_releases_actual_rpc_claim(launch_context, actor_host):
    spec, _, channel, complete = launch_context
    process = make_process(spec, channel, complete, actor_host)
    try:
        assert process.wait_ready(timeout=20), process.last_error
        client = process._last_client
        payload = json.loads(client.command[3])
        child = client._process
        assert reservation(payload).is_dir()
        assert "synthetic-pi-test-key" not in json.dumps(payload)
    finally:
        process.stop()
    assert child.returncode is not None, child.pid
    assert not reservation(payload).exists()


def test_bound_resume_never_falls_back_to_a_fresh_session(launch_context, actor_host):
    spec, context, channel, complete = launch_context
    spec = replace(spec, session_ref="missing-session")
    process = make_process(spec, channel, complete, actor_host)
    try:
        assert not process.wait_ready(timeout=20)
        wait_for(lambda: "MODEL_RESUME_MISMATCH" in str(process.last_error))
        assert process._session_ref == "missing-session"
        assert not list(context.roots.session_root.glob(".hyprial-loader-receipts/*.json"))
    finally:
        process.stop()


def test_no_binding_in_legacy_client_without_context():
    with pytest.raises(ValueError, match="trusted P2"):
        PiRpcClient(HarnessLaunchSpec("pi", "worker", True), "session", model_binding=binding())


@pytest.mark.parametrize("same_resource", [False, True])
def test_concurrent_start_terminal_results_and_stale_child_cleanup(launch_context, actor_host, tmp_path, same_resource):
    spec, context, channel, complete = launch_context
    entered, release = threading.Event(), threading.Event()
    processes, clients, outcomes = [], [], []

    class Launcher:
        def start(self, selected):
            process = make_process(selected, channel, complete, actor_host, shared_session="shared-session")
            processes.append(process)
            if not process.wait_ready(timeout=20):
                error = process.last_failure
                process.stop()
                raise error
            with process._lock:
                clients.append(process._client._process)
            if same_resource and len(processes) == 1:
                entered.set()
                assert release.wait(20)
            return process

    runtime = ManagedHarnessRuntime(Launcher(), actor_host=actor_host, orphan_state_path=tmp_path / "orphans.json")
    def start(selected):
        try:
            outcomes.append(runtime.start(selected))
        except Exception as error:
            outcomes.append(error)
    first = threading.Thread(target=start, args=(spec,))
    try:
        first.start()
        if same_resource:
            assert entered.wait(20)
        else:
            first.join(20)
            assert not first.is_alive() and outcomes == [True]
        start(spec if same_resource else replace(spec, name="second-resource", member="second-resource"))
        release.set()
        first.join(20)
        assert not first.is_alive()
        codes = {getattr(value, "code", None) for value in outcomes if isinstance(value, Exception)}
        assert "MODEL_SESSION_BUSY" in codes, [(type(v).__name__, str(v)) for v in outcomes]
        if same_resource:
            assert "HARNESS_START_SUPERSEDED" in codes
            wait_for(lambda: all(not process.running for process in processes))
        else:
            assert processes[0].running
    finally:
        release.set()
        first.join(20)
        runtime.stop(15)
        for process in processes:
            process.stop()
    assert clients and all(child.returncode is not None for child in clients)
    assert not list(context.roots.session_root.glob(".hyprial-session-reservations/*"))


@pytest.mark.parametrize("path", ["restore", "reconcile"])
def test_automatic_paths_respect_existing_session_writer(launch_context, actor_host, tmp_path, path):
    spec, context, channel, complete = launch_context
    incumbent = None
    attempts = []

    class Launcher:
        def start(self, selected):
            child = make_process(selected, channel, complete, actor_host, shared_session="shared-session")
            attempts.append(child)
            if not child.wait_ready(timeout=20):
                error = child.last_failure
                child.stop()
                raise error
            return child

    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(spec)
    runtime = ManagedHarnessRuntime(Launcher(), actor_host=actor_host, orphan_state_path=tmp_path / "restore-orphans.json",
                                    desired_state=store)
    try:
        if path == "reconcile":
            assert runtime.restore((spec,)).restored == 1
            attempts[0].stop()
        incumbent = make_process(spec, channel, complete, actor_host, shared_session="shared-session")
        assert incumbent.wait_ready(timeout=20), incumbent.last_error
        if path == "restore":
            result = runtime.restore((spec,))
            assert (result.attempted, result.restored, result.failed) == (1, 0, 1)
        else:
            def settled():
                runtime.reconcile()
                return bool(runtime._failed)
            wait_for(settled)
        expected = 1 if path == "restore" else 2
        assert len(attempts) == expected and all(not child.running for child in attempts)
        assert "MODEL_SESSION_BUSY" in runtime.last_errors["pi:worker"]
        for _ in range(3):
            assert runtime.reconcile() == 0
        assert len(attempts) == expected and incumbent.running
    finally:
        runtime.stop(15)
        for child in attempts:
            child.stop()
        if incumbent:
            incumbent.stop()
    assert not list(context.roots.session_root.glob(".hyprial-session-reservations/*"))
