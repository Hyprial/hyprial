"""Adapter removal custody is harness-registration only (I6-4).

The agent-side pin custody (reserve/unpin/restore with reservation tokens)
retired with the pins table: adapter bindings live in agent cell documents,
which a removal never touches, so the transaction only snapshots, removes and
restores the harness registration preimage.
"""

import threading

import pytest

from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
from hyprial.daemon.impl.operations.management import (
    AdapterRegistrySnapshot,
    AdapterRemovalTransaction,
    ManagementError,
    RegistryManagementHandler,
)


class Harnesses:
    def __init__(self):
        self.events = []

    def snapshot_adapter_registration(self, name):
        return HarnessAdapterRegistrationProjection(name, None, None)

    def remove_adapter_registration(self, snapshot):
        self.events.append("remove")

    def restore_adapter_registration(self, snapshot):
        self.events.append("restore")


def _snapshot(name="app"):
    return AdapterRegistrySnapshot(
        HarnessAdapterRegistrationProjection(name, None, None), None
    )


def test_commit_removes_registration_once_and_rollback_restores() -> None:
    harnesses = Harnesses()
    transaction = AdapterRemovalTransaction(harnesses, _snapshot())
    transaction.commit()
    transaction.commit()
    assert harnesses.events == ["remove"]
    transaction.rollback()
    transaction.rollback()
    assert harnesses.events == ["remove", "restore"]
    assert transaction.committed is False
    assert transaction.unsettled is False


def test_recovery_transaction_is_compensation_only() -> None:
    transaction = AdapterRemovalTransaction(Harnesses(), _snapshot(), recovery=True)
    assert transaction.unsettled is True
    with pytest.raises(ManagementError, match="compensation-only"):
        transaction.commit()


def test_handler_serializes_snapshot_and_releases_the_lease_on_failure() -> None:
    harnesses = Harnesses()
    handler = RegistryManagementHandler(harnesses)
    with pytest.raises(ValueError, match="file failed"):
        with handler.adapter_removal("app") as transaction:
            assert transaction.snapshot.agent_pin is None
            transaction.commit()
            raise ValueError("file failed")
    assert harnesses.events == ["remove", "restore"]


def test_handler_runs_the_transaction_on_the_caller_thread() -> None:
    threads: list[int] = []

    class Threaded(Harnesses):
        def remove_adapter_registration(self, snapshot):
            threads.append(threading.get_ident())

    handler = RegistryManagementHandler(Threaded())
    with handler.adapter_removal("app") as transaction:
        transaction.commit()
    assert threads == [threading.get_ident()]
