"""Confirmed deletion settles files and registry against a durable preimage."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.operations.adapter_removal.journal import (
    AdapterRemovalJournal, RemovalPhase,
)
from hyprial.daemon.impl.state_db import StateDatabase
from tests.daemon.management.removal.test_journal import intent


@pytest.fixture
def removal(tmp_path):
    from hyprial.daemon.impl.operations.adapter_removal.transaction import RemovalTransaction

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "secrets").mkdir(mode=0o700)
    secret = home / "secrets" / "lark-app.json"
    secret.write_bytes(b'{"appSecret":"synthetic-private-value"}')
    secret.chmod(0o600)
    channels = home / "channels.json"
    channels.write_bytes(b'{"version":1,"gateways":[{"name":"app"}]}\n')
    channels.chmod(0o600)
    import hashlib

    record = replace(intent(), channels_before=channels.read_bytes(),
                     channels_after=b'{"version":1,"gateways":[]}\n',
                     credential_digest=hashlib.sha256(secret.read_bytes()).hexdigest())
    journal = AdapterRemovalJournal(StateDatabase(home / "state.sqlite3"))
    effects = []
    registry = SimpleNamespace(present=True)

    @contextmanager
    def lease(*, recovery=False):
        def commit():
            effects.append("commit")
            registry.present = False

        def rollback():
            effects.append("rollback")
            registry.present = True

        transaction = SimpleNamespace(commit=commit, rollback=rollback)
        try:
            yield transaction
        except BaseException:
            rollback()
            raise

    return SimpleNamespace(home=home, channels=channels, secret=secret, journal=journal,
                           intent=record, effects=effects, registry=registry, lease=lease,
                           transaction=RemovalTransaction(home, journal))


def test_success_removes_configuration_registration_and_owned_secret(removal):
    r = removal
    with r.lease() as lease:
        r.transaction.commit(r.intent, lease)
    r.transaction.finish(r.intent.operation_id)
    assert r.channels.read_bytes() == r.intent.channels_after
    assert not r.secret.exists()
    assert r.registry.present is False
    assert r.journal.get(r.intent.operation_id).phase is RemovalPhase.COMPLETED
    assert b"synthetic-private-value" not in r.journal._state_db.path.read_bytes()


def test_failed_registry_commit_restores_configuration_and_keeps_secret(removal):
    r = removal
    def fail():
        raise OSError("synthetic registry failure")
    with pytest.raises(OSError):
        with r.lease() as lease:
            lease.commit = fail
            r.transaction.commit(r.intent, lease)
    assert r.channels.read_bytes() == r.intent.channels_before
    assert r.secret.exists()
    assert r.registry.present is True
    assert r.journal.get(r.intent.operation_id).phase is RemovalPhase.COMPENSATED


def test_recovery_compensates_prepared_write_without_replaying_forward_commit(removal):
    r = removal
    r.journal.prepare(r.intent)
    r.channels.write_bytes(r.intent.channels_after)
    r.registry.present = False
    with r.lease(recovery=True) as lease:
        r.transaction.compensate(r.intent.operation_id, lease)
    assert r.channels.read_bytes() == r.intent.channels_before
    assert r.secret.exists()
    assert r.registry.present is True
    assert r.effects == ["rollback"]
    assert r.journal.get(r.intent.operation_id).phase is RemovalPhase.COMPENSATED


def test_committed_recovery_only_finishes_cleanup(removal, monkeypatch):
    r = removal
    with r.lease() as lease:
        r.transaction.commit(r.intent, lease)
    original = type(r.secret).unlink
    monkeypatch.setattr(type(r.secret), "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup interrupted")))
    with pytest.raises(OSError):
        r.transaction.finish(r.intent.operation_id)
    assert r.journal.get(r.intent.operation_id).phase is RemovalPhase.COMMITTED
    monkeypatch.setattr(type(r.secret), "unlink", original)
    r.transaction.finish(r.intent.operation_id)
    assert not r.secret.exists()
    assert r.effects == ["commit"]
    assert r.registry.present is False


def test_recovery_does_not_overwrite_a_new_configuration(removal):
    r = removal
    r.journal.prepare(r.intent)
    r.channels.write_bytes(b"concurrently replaced")
    with pytest.raises(Exception, match="changed"):
        with r.lease(recovery=True) as lease:
            r.transaction.compensate(r.intent.operation_id, lease)
    assert r.channels.read_bytes() == b"concurrently replaced"
    assert r.journal.pending()
    assert r.secret.exists()


def test_cleanup_does_not_delete_a_replaced_credential(removal):
    r = removal
    with r.lease() as lease:
        r.transaction.commit(r.intent, lease)
    r.secret.write_bytes(b"replacement")
    with pytest.raises(Exception, match="changed"):
        r.transaction.finish(r.intent.operation_id)
    assert r.secret.read_bytes() == b"replacement"
    assert r.journal.get(r.intent.operation_id).phase is RemovalPhase.COMMITTED


def test_external_or_shared_credential_is_retained(removal):
    r = removal
    with r.lease() as lease:
        r.transaction.commit(replace(r.intent, remove_credential=False), lease)
    r.transaction.finish(r.intent.operation_id)
    assert r.secret.exists()
    assert r.journal.pending() == ()


def test_runtime_settlement_failure_is_recoverable_after_credential_cleanup(removal):
    r = removal
    with r.lease() as lease:
        r.transaction.commit(r.intent, lease)

    def unavailable():
        raise RuntimeError("runtime settlement unavailable")

    with pytest.raises(RuntimeError, match="settlement unavailable"):
        r.transaction.finish(r.intent.operation_id, settle=unavailable)
    assert not r.secret.exists()
    assert r.journal.get(r.intent.operation_id).phase is RemovalPhase.COMMITTED
    assert r.effects == ["commit"]
    r.transaction.finish(r.intent.operation_id)
    assert r.journal.pending() == ()
    assert r.effects == ["commit"]
