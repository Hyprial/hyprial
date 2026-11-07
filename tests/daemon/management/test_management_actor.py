import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
from hyprial.daemon.impl.operations.management_actor import RegistryManagementAuthority


class Harnesses:
    def __init__(self):
        self.events = []

    def snapshot_adapter_registration(self, name):
        return HarnessAdapterRegistrationProjection(name, None, None)

    def remove_adapter_registration(self, snapshot):
        self.events.append(("remove", threading.get_ident()))

    def restore_adapter_registration(self, snapshot):
        self.events.append(("restore", threading.get_ident()))


def test_file_failure_compensates_on_owned_lane_and_releases_lease():
    harnesses = Harnesses()
    owner = RegistryManagementAuthority(harnesses, SimpleNamespace(pins=lambda: {}))
    try:
        with pytest.raises(ValueError, match="file failure"):
            with owner.adapter_removal("adapter") as lease:
                lease.commit()
                raise ValueError("file failure")
        assert not owner.status()["leaseActive"]
        assert [kind for kind, thread in harnesses.events] == ["remove", "restore"]
        assert len({thread for kind, thread in harnesses.events}) == 1
        assert harnesses.events[0][1] != threading.get_ident()
    finally:
        assert owner.close()


def test_timed_out_begin_is_retired_after_snapshot_finishes():
    entered, release = threading.Event(), threading.Event()
    harnesses = Harnesses()
    original = harnesses.snapshot_adapter_registration

    def snapshot(name):
        entered.set()
        assert release.wait(3)
        return original(name)

    harnesses.snapshot_adapter_registration = snapshot
    owner = RegistryManagementAuthority(
        harnesses, SimpleNamespace(pins=lambda: {}), timeout=0.01
    )
    try:
        with pytest.raises(TimeoutError):
            with owner.adapter_removal("adapter"):
                raise AssertionError("timed-out context must not enter file mutation")
        assert entered.is_set()
        assert not owner.close(0.01)
    finally:
        release.set()
        assert owner.close(2)
    assert harnesses.events == []
    assert not owner.status()["leaseActive"]


def test_close_waits_for_an_active_file_compensation_lease():
    owner = RegistryManagementAuthority(Harnesses(), SimpleNamespace(pins=lambda: {}))
    with owner.adapter_removal("adapter"):
        assert not owner.close(0.01)
    assert owner.close(1)


def test_timed_out_failed_begin_drops_abandoned_custody_even_if_wake_is_full(
    monkeypatch,
):
    from hyprial.kernel.impl.actor_runtime import AdmissionResult
    from hyprial.daemon.impl.operations.management_actor import AbandonAdapterRemoval

    release = threading.Event()
    harnesses = Harnesses()

    def snapshot(name):
        assert release.wait(3)
        raise OSError("synthetic snapshot failure")

    harnesses.snapshot_adapter_registration = snapshot
    owner = RegistryManagementAuthority(
        harnesses, SimpleNamespace(pins=lambda: {}), timeout=0.01
    )
    original = owner._runtime.tell

    def tell(handle, command):
        if isinstance(command, AbandonAdapterRemoval):
            return AdmissionResult.OVERLOADED
        return original(handle, command)

    monkeypatch.setattr(owner._runtime, "tell", tell)
    try:
        with pytest.raises(TimeoutError):
            with owner.adapter_removal("adapter"):
                raise AssertionError("must not enter file mutation")
        release.set()
        assert owner.close(2)
        assert not owner._abandoned
        assert not owner._cleanup_leases
    finally:
        release.set()
        assert owner.close(2)
