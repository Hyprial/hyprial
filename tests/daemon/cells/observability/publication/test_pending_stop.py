"""Independent C review: accepted unresolved stop must be stopping."""
import json
import threading

import pytest

from tests.daemon.cells.observability.mount.test_mount import composed as composed, settle
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleState
from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.kernel import PortAdmission


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / 'review-home'
    home.mkdir(mode=0o700)
    (home / 'settings.json').write_text(json.dumps({'version': 1, 'owner': 'review'}))
    monkeypatch.setenv('HYPRIAL_HOME', str(home))
    monkeypatch.setenv('HARNESS_STATE_DIR', str(home / 'state'))
    monkeypatch.setenv('HYPRIAL_NETWORK_ISOLATED', '1')
    monkeypatch.setenv('HYPRIAL_OWNER', 'review')


def test_unresolved_stop_is_stopping(composed, monkeypatch):
    app, launcher, harness, *_, spec = composed
    settle(app, LifecycleOperation.create('review-start', spec))
    process = launcher.processes[-1]
    process.read_run_facts = lambda: RunFacts(source_epoch='turn', known=True, running=True)
    harness._records['pi:worker'].process.observe_facts()
    entered, release = threading.Event(), threading.Event()

    def stop():
        entered.set()
        assert release.wait(20)
        process.running = False

    process.stop = stop
    operation = LifecycleOperation.deactivate('review-stop', spec)
    assert app._lifecycle_manager.submit(operation) is PortAdmission.ACCEPTED
    try:
        assert entered.wait(10)
        row = app._cell_run_state_rows()[spec.actor]
        print('accepted stop awaiting native completion:', row)
        assert row['state'] == 'stopping', row
        assert len(app._lifecycle_manager.read_run_operations()) == 1
        # A replacement native instance cannot inherit an old accepted stop.
        with monkeypatch.context() as patch:
            patch.setattr(harness.projection, 'read_run_instance', lambda *args: ('new', 'process'))
            assert app._cell_run_state_rows()[spec.actor]['state'] != 'stopping'
        assert app._cell_run_state_rows()[spec.actor]['state'] == 'stopping'
    finally:
        release.set()
        assert app._lifecycle_manager.wait(operation.operation_id, 20).state is LifecycleState.COMPLETED
    assert app._lifecycle_manager.read_run_operations() == ()


def test_unresolved_start_is_starting(composed):
    app, launcher, harness, *_, spec = composed
    entered, release = threading.Event(), threading.Event()
    start = launcher.start

    def delayed_start(launch_spec):
        entered.set()
        assert release.wait(20)
        return start(launch_spec)

    launcher.start = delayed_start
    operation = LifecycleOperation.create('pending-native-start', spec)
    assert app._lifecycle_manager.submit(operation) is PortAdmission.ACCEPTED
    try:
        assert entered.wait(10)
        assert app._cell_run_state_rows()[spec.actor]['state'] == 'starting'
        assert launcher.starts == 0
    finally:
        release.set()
        assert app._lifecycle_manager.wait(operation.operation_id, 20).state is LifecycleState.COMPLETED
    assert app._lifecycle_manager.read_run_operations() == ()
