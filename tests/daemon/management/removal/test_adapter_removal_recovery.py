"""Recovery restores the durable harness preimage and is compensation-only."""

import pytest

from hyprial.daemon.impl.adapters.spec import AdapterSpec
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
from hyprial.daemon.impl.operations.management import AdapterRegistrySnapshot, ManagementError
from hyprial.daemon.impl.operations.management_actor import (
    EndAdapterRemoval, RegistryManagementAuthority, RollbackAdapterRemoval,
)


class Registrations:
    def __init__(self):
        self.current = HarnessAdapterRegistrationProjection("app", None, None)
        self.restores = 0
        self.fail_restore = False

    def snapshot_adapter_registration(self, name):
        assert name == self.current.name
        return self.current

    def remove_adapter_registration(self, snapshot):
        raise AssertionError("recovery must never repeat removal")

    def restore_adapter_registration(self, snapshot):
        self.restores += 1
        if self.fail_restore:
            raise OSError("synthetic restore failed")
        self.current = snapshot


def preimage(_agents=None, actor="one"):
    return AdapterRegistrySnapshot(
        HarnessAdapterRegistrationProjection("app", AdapterSpec("lark", "app"), None),
        None,
    )


def test_recovery_restores_the_preimage_once_and_never_repeats_removal(actor_host):
    snapshot = preimage()
    harnesses = Registrations()
    owner = RegistryManagementAuthority(harnesses, actor_host=actor_host)
    try:
        with owner.adapter_removal("app", recovery_snapshot=snapshot) as lease:
            assert lease.snapshot == snapshot
            with pytest.raises(ManagementError, match="compensation-only"):
                lease.commit()
            lease.rollback()
            lease.rollback()
            assert harnesses.restores == 1
            assert harnesses.current == snapshot.harness
            with pytest.raises(ManagementError, match="compensation-only"):
                lease.commit()
        assert not owner.status()["leaseActive"]
    finally:
        assert owner.close()


def test_failed_compensation_retains_custody_until_retry_settles(actor_host):
    harnesses = Registrations()
    harnesses.fail_restore = True
    owner = RegistryManagementAuthority(harnesses, actor_host=actor_host)
    token = None
    try:
        with pytest.raises(ManagementError, match="unsettled"):
            with owner.adapter_removal("app", recovery_snapshot=preimage()) as lease:
                token = lease._token
                lease.rollback()
        assert owner.status()["leaseActive"]
        harnesses.fail_restore = False
        owner._call(RollbackAdapterRemoval(token))
        owner._call(EndAdapterRemoval(token))
        token = None
        assert not owner.status()["leaseActive"]
    finally:
        if token is not None:
            harnesses.fail_restore = False
            owner._call(RollbackAdapterRemoval(token))
            owner._call(EndAdapterRemoval(token))
        assert owner.close()


def test_recovery_requires_explicit_settlement_before_releasing_custody(actor_host):
    owner = RegistryManagementAuthority(Registrations(), actor_host=actor_host)
    try:
        with pytest.raises(ManagementError, match="unsettled"):
            with owner.adapter_removal("app", recovery_snapshot=preimage()) as lease:
                token = lease._token
        assert owner.status()["leaseActive"]
        owner._call(RollbackAdapterRemoval(token))
        owner._call(EndAdapterRemoval(token))
    finally:
        assert owner.close()


def test_mismatched_adapter_preimage_is_refused_before_any_restore(actor_host):
    snapshot = preimage()
    owner = RegistryManagementAuthority(Registrations(), actor_host=actor_host)
    try:
        with pytest.raises(ManagementError, match="different adapter"):
            with owner.adapter_removal("another", recovery_snapshot=snapshot):
                raise AssertionError("mismatched adapter admitted")
        assert not owner.status()["leaseActive"]
    finally:
        assert owner.close()


def test_pre_upgrade_journal_row_with_a_legacy_pin_still_recovers(actor_host):
    """A removal journaled before F1b may carry a non-null ``legacyPin``; it still recovers."""

    snapshot = AdapterRegistrySnapshot(
        HarnessAdapterRegistrationProjection(
            "app", AdapterSpec("lark", "app"), "agent:alice:node:one"
        ),
        None,
    )
    harnesses = Registrations()
    owner = RegistryManagementAuthority(harnesses, actor_host=actor_host)
    try:
        with owner.adapter_removal("app", recovery_snapshot=snapshot) as lease:
            lease.rollback()
            assert harnesses.restores == 1
            assert harnesses.current == snapshot.harness
        assert not owner.status()["leaseActive"]
    finally:
        assert owner.close()
