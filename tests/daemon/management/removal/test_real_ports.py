"""Recovery observes real Harness persistence errors through the composition port."""

import errno

import pytest

from hyprial.daemon.impl.composition import CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.operations.management import ManagementError
from hyprial.daemon.impl.operations.management_actor import (
    EndAdapterRemoval, RegistryManagementAuthority, RollbackAdapterRemoval,
)
from tests.daemon.management.removal.test_adapter_removal_recovery import preimage


@pytest.fixture
def harness_port(tmp_path, actor_host):
    class NoProcessLauncher:
        def start(self, *args, **kwargs):
            raise AssertionError("registry recovery must not start a process")

    store = DesiredStateStore(tmp_path / "desired.json")
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(
        NoProcessLauncher(), desired_state=store, event_sink=events.publish,
        actor_host=actor_host,
    )
    yield HarnessPortClient(actor, events, timeout=3), store
    assert actor.close_runtime(3)


def test_restore_preimage_through_the_real_actor_port(actor_host, harness_port):
    harnesses, store = harness_port
    owner = RegistryManagementAuthority(harnesses, actor_host=actor_host)
    snapshot = preimage()
    try:
        with owner.adapter_removal("app", recovery_snapshot=snapshot) as lease:
            lease.rollback()
        assert harnesses.snapshot_adapter_registration("app") == snapshot.harness
        assert store.load().adapters == (snapshot.harness.spec,)
    finally:
        assert owner.close()


def test_persistence_failure_does_not_report_recovery_settled(actor_host, harness_port, monkeypatch):
    harnesses, store = harness_port
    owner = RegistryManagementAuthority(harnesses, actor_host=actor_host)
    restore = store.restore_adapter_registration
    token = None

    def failed(*args, **kwargs):
        raise PermissionError(errno.EACCES, "synthetic read-only store")

    monkeypatch.setattr(store, "restore_adapter_registration", failed)
    try:
        with pytest.raises(ManagementError, match="unsettled"):
            with owner.adapter_removal("app", recovery_snapshot=preimage()) as lease:
                token = lease._token
                lease.rollback()
        assert owner.status()["leaseActive"]
        assert store.load().adapters == ()
        monkeypatch.setattr(store, "restore_adapter_registration", restore)
        owner._call(RollbackAdapterRemoval(token))
        owner._call(EndAdapterRemoval(token))
        token = None
        assert len(store.load().adapters) == 1
    finally:
        if token is not None and owner.status()["leaseActive"]:
            monkeypatch.setattr(store, "restore_adapter_registration", restore)
            owner._call(RollbackAdapterRemoval(token))
            owner._call(EndAdapterRemoval(token))
        assert owner.close()


@pytest.mark.parametrize("operation", ["snapshot", "remove", "restore"])
def test_composition_port_preserves_immutable_error_metadata(harness_port, monkeypatch, operation):
    harnesses, store = harness_port

    def failed(*args, **kwargs):
        raise PermissionError(errno.EACCES, "synthetic read-only store", "fixture.json")

    method = {
        "snapshot": "snapshot_adapter_registration", "remove": "remove_adapter_registration",
        "restore": "restore_adapter_registration",
    }[operation]
    with monkeypatch.context() as patch:
        patch.setattr(store, "load" if operation == "snapshot" else method, failed)
        with pytest.raises(PermissionError) as caught:
            getattr(harnesses, method)("app" if operation == "snapshot" else preimage().harness)
    assert caught.value.errno == errno.EACCES
    assert caught.value.filename == "fixture.json"
