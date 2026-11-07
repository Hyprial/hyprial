"""TC-005/013: durable preparation, replay, expiry and competing custody.

These exercise the internal ledger on real SQLite, not the public CLI or SSH.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Reason
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore, ADMISSION_TTL_MS


def test_replay_survives_store_recreation_without_renewal(ledger, binding):
    store, now, db = ledger
    first = store.prepare("op", binding)
    now[0] += 10
    restarted = AdmissionStore(db, node="source", owner="test-owner", clock_ms=lambda: now[0])
    assert restarted.prepare("op", binding) == first
    assert first.expires_at_ms == first.created_at_ms + ADMISSION_TTL_MS
    assert store.get("unknown") is None
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM transfer_p2_admissions").fetchone()[0] == 1


@pytest.mark.parametrize("change", [
    {"plan_digest": "b" * 64}, {"target_node": "other"},
    {"target_actor": "other"}, {"source_entity_token": "other"},
])
def test_operation_id_cannot_be_rebound(ledger, binding, change):
    store, _, _ = ledger
    before = store.prepare("op", binding)
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("op", replace(binding, **change))
    assert error.value.reason is Reason.REPLAY_MISMATCH
    assert store.get("op") == before


@pytest.mark.parametrize("change", [
    {"owner": "foreign"}, {"source_node": "foreign"}, {"target_node": "source"},
])
def test_wrong_identity_or_node_never_creates_admission(ledger, binding, change):
    store, _, _ = ledger
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("op", replace(binding, **change))
    assert error.value.reason is Reason.BINDING_MISMATCH
    assert store.get("op") is None


def test_expiry_is_visible_but_does_not_release_custody(ledger, binding):
    store, now, _ = ledger
    first = store.prepare("op", binding)
    now[0] = first.expires_at_ms
    with pytest.raises(AdmissionConflict) as error:
        store.require_live("op")
    assert error.value.reason is Reason.EXPIRED
    assert store.prepare("op", binding) == first
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("competing", replace(binding, target_node="third"))
    assert error.value.reason is Reason.RESERVED


def test_renewal_is_explicit_cas_and_idempotent(ledger, binding):
    store, now, _ = ledger
    first = store.prepare("op", binding)
    now[0] += 100
    new_expiry = now[0] + ADMISSION_TTL_MS
    renewed = store.renew("op", expected_expiry=first.expires_at_ms, expires_at_ms=new_expiry)
    assert store.renew("op", expected_expiry=first.expires_at_ms, expires_at_ms=new_expiry) == renewed
    assert renewed.created_at_ms == first.created_at_ms
    # Keep the second request inside the TTL limit: only the stale CAS must
    # reject it, otherwise a broken CAS is masked by the independent bound.
    now[0] += 1
    with pytest.raises(AdmissionConflict) as error:
        store.renew("op", expected_expiry=first.expires_at_ms, expires_at_ms=new_expiry + 1)
    assert error.value.reason is Reason.RENEWAL_CONFLICT


@pytest.mark.parametrize("lateness", [0, 1])
@pytest.mark.parametrize("extension", [0, ADMISSION_TTL_MS])
def test_expired_admission_cannot_be_rearmed_even_by_renewal_replay(
    ledger, binding, lateness, extension,
):
    store, now, _ = ledger
    first = store.prepare("op", binding)
    now[0] = first.expires_at_ms + lateness
    with pytest.raises(AdmissionConflict) as error:
        store.renew("op", expected_expiry=first.expires_at_ms,
                    expires_at_ms=first.expires_at_ms + extension)
    assert error.value.reason is Reason.EXPIRED
    assert store.get("op") == first
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("new-operation", binding)
    assert error.value.reason is Reason.RESERVED


def test_revocation_cannot_be_undone_by_replay_or_renewal(ledger, binding):
    store, now, _ = ledger
    first = store.prepare("op", binding)
    now[0] += 10
    revoked = store.revoke("op")
    now[0] += 10
    assert store.revoke("op") == revoked
    assert store.prepare("op", binding) == revoked
    for action in (
        lambda: store.require_live("op"),
        lambda: store.renew("op", expected_expiry=first.expires_at_ms,
                           expires_at_ms=now[0] + ADMISSION_TTL_MS),
    ):
        with pytest.raises(AdmissionConflict) as error:
            action()
        assert error.value.reason is Reason.REVOKED
    with pytest.raises(AdmissionConflict):
        store.prepare("competing", binding)


def test_explicit_prepared_cancellation_releases_only_that_reservation(ledger, binding):
    store, _, _ = ledger
    store.prepare("op", binding)
    cancelled = store.cancel_prepared("op")
    assert store.cancel_prepared("op") == cancelled
    new = store.prepare("next", binding)
    assert store.prepare("op", binding) == cancelled
    with pytest.raises(AdmissionConflict) as error:
        store.require_live("op")
    assert error.value.reason is Reason.CANCELLED
    assert store.get("next") == new


def test_two_concurrent_transfers_of_same_source_have_one_winner(ledger, binding):
    _, now, db = ledger
    barrier = threading.Barrier(2)

    def race(index):
        store = AdmissionStore(db, node="source", owner="test-owner", clock_ms=lambda: now[0])
        barrier.wait(timeout=3)
        try:
            return store.prepare(f"op-{index}", replace(binding, target_node=f"target-{index}"))
        except AdmissionConflict as error:
            return error.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, (1, 2)))
    assert sum(result is Reason.RESERVED for result in results) == 1
    assert sum(not isinstance(result, Reason) for result in results) == 1


def test_target_name_is_reserved_across_different_sources(ledger, target_binding):
    _, now, db = ledger
    target = AdmissionStore(db, node="target", owner="test-owner", clock_ms=lambda: now[0])
    target.prepare("one", target_binding)
    other = replace(target_binding, source_node="other-source", source_agent_id="a.other")
    with pytest.raises(AdmissionConflict) as error:
        target.prepare("two", other)
    assert error.value.reason is Reason.RESERVED


def test_unrelated_shared_database_rows_are_untouched(ledger, binding):
    store, _, db = ledger
    with db.transaction("CREATE TABLE unrelated (value TEXT);") as conn:
        conn.execute("INSERT INTO unrelated VALUES ('keep')")
    store.prepare("op", binding)
    store.cancel_prepared("op")
    with db.read() as conn:
        assert conn.execute("SELECT value FROM unrelated").fetchall() == [("keep",)]


@pytest.mark.parametrize("offset", [0, -1, ADMISSION_TTL_MS + 1])
def test_renewal_cannot_shorten_or_exceed_its_budget(ledger, binding, offset):
    store, now, _ = ledger
    first = store.prepare("op", binding)
    with pytest.raises(AdmissionConflict) as error:
        store.renew("op", expected_expiry=first.expires_at_ms, expires_at_ms=now[0] + offset)
    assert error.value.reason is Reason.RENEWAL_CONFLICT
    assert store.get("op") == first


@pytest.mark.parametrize("method", ["require_live", "revoke", "cancel_prepared"])
def test_unknown_operations_do_not_create_records(ledger, method):
    store, _, _ = ledger
    with pytest.raises(AdmissionConflict) as error:
        getattr(store, method)("absent")
    assert error.value.reason is Reason.UNKNOWN
    assert store.get("absent") is None


def test_a_successor_reaps_only_the_preparation_an_audit_released(ledger, binding):
    """A3: one failed transfer used to reserve the worker forever.

    The rollback's audited override is the operator's explicit release act, but
    it cannot delete the reservation.  The successor therefore reaps it -- and
    *only* it: a step nobody audited, or no step at all, keeps its reservation.
    """

    store, _, _ = ledger
    store.prepare("op", binding)
    # Nothing audited yet: the reservation still answers for the slot.
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("successor", binding)
    assert error.value.reason is Reason.RESERVED

    step = store.journal.begin("op", step="quiesce", fingerprint="fp", actor="worker").state
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("successor", binding)
    assert error.value.reason is Reason.RESERVED

    store.journal.attention("op", expected=step, event_id="ev-1",
                            principal="test-owner", action="transfer-custody-override",
                            reason="transfer rollback")
    successor = store.prepare("successor", binding)
    assert successor.operation_id == "successor"
    # The audit trail survives: the reservation is cancelled, never deleted.
    released = store.get("op")
    assert released is not None and released.cancelled_at_ms is not None
    assert store.journal.get("op").needs_attention
    assert [event.reason for event in store.journal.events("op")] == ["transfer rollback"]
    # And the freed slot really is free: no second reservation competes.
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("third", binding)
    assert error.value.reason is Reason.RESERVED


def test_a_step_less_preparation_is_never_reaped(ledger, binding):
    """A3 scope: no step means no audit, so C1's rule still stands."""

    store, now, _ = ledger
    first = store.prepare("op", binding)
    now[0] = first.expires_at_ms + 1
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("successor", binding)
    assert error.value.reason is Reason.RESERVED
    assert store.get("op").cancelled_at_ms is None


def test_source_custody_names_the_preparation_holding_an_incarnation(ledger, binding):
    store, _, _ = ledger
    store.prepare("op", binding)
    held = store.source_custody(owner="test-owner", node="source",
                                agent_id="a.synthetic", entity_token="source-incarnation")
    assert held is not None and held.operation_id == "op"
    assert store.source_custody(owner="test-owner", node="source",
                                agent_id="a.synthetic", entity_token="another") is None
