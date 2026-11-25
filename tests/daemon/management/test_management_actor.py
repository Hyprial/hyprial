import threading

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


def test_file_failure_compensates_on_owned_lane_and_releases_lease(actor_host):
    harnesses = Harnesses()
    owner = RegistryManagementAuthority(
        harnesses, actor_host=actor_host
    )
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


def test_timed_out_begin_is_retired_after_snapshot_finishes(actor_host):
    entered, release = threading.Event(), threading.Event()
    harnesses = Harnesses()
    original = harnesses.snapshot_adapter_registration

    def snapshot(name):
        entered.set()
        assert release.wait(3)
        return original(name)

    harnesses.snapshot_adapter_registration = snapshot
    owner = RegistryManagementAuthority(
        harnesses,
        timeout=0.01,
        actor_host=actor_host,
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


def test_close_waits_for_an_active_file_compensation_lease(actor_host):
    owner = RegistryManagementAuthority(
        Harnesses(), actor_host=actor_host
    )
    with owner.adapter_removal("adapter"):
        assert not owner.close(0.01)
    assert owner.close(1)


def test_timed_out_failed_begin_drops_abandoned_custody_even_if_wake_is_full(
    actor_host,
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
        harnesses,
        timeout=0.01,
        actor_host=actor_host,
    )

    class _RefusingAbandonCell:
        __slots__ = ("_cell",)

        def __init__(self, cell):
            self._cell = cell

        def tell(self, command):
            if isinstance(command, AbandonAdapterRemoval):
                return AdmissionResult.OVERLOADED
            return self._cell.tell(command)

        def stop(self, timeout):
            return self._cell.stop(timeout)

    owner._cell = _RefusingAbandonCell(owner._cell)
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
