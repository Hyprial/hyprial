"""The production gate must ignore supervised adapter churn.

Measured on the frozen pin: inside a ~50s crash-recovery window an unrelated
Lark adapter flipped ``online``/``stale`` (and its worker ``pid``) while the
production daemon ``pid``/``epoch`` never moved. Comparing whole snapshots
reported ``productionUnchanged=false`` in the report even though the daemon was
untouched, so two otherwise-green runs looked like product failures.
"""

from __future__ import annotations

from tests.fixtures.smolvm.smolvm_daemon import production_identity, production_unchanged

# Synthetic adapter ids.  The measured capture came from two real Lark adapters,
# but naming a production adapter in tests/ trips
# scripts/verify/check_hyprial_merge_residuals.py (allowances are given by specific
# name, never by shape, and nothing else in tests/ names these two).  This file
# only needs two distinct ids whose status/online/pid can move.

PID = 88544
EPOCH = "5f3bdd854d114308a9478fbd7d92601a"


def _adapter(adapter_id, online=True, status="online", pid=89017):
    return {
        "id": adapter_id,
        "provider": "lark",
        "name": adapter_id.split(":", 1)[-1],
        "status": status,
        "online": online,
        "configured": True,
        "desired": True,
        "processRunning": online,
        "pid": pid,
    }


def _snapshot(pid=PID, epoch=EPOCH, adapters=None):
    return {"pid": pid, "epoch": epoch, "adapters": adapters or []}


def test_adapter_reconnect_is_observed_instead_of_failing_the_run():
    before = _snapshot(
        adapters=[
            _adapter("lark:at08-review-bot"),
            _adapter("lark:at08-monitor-bot", pid=89051),
        ]
    )
    after = _snapshot(
        adapters=[
            _adapter(
                "lark:at08-review-bot",
                online=False,
                status="stale",
                pid=99001,
            ),
            _adapter("lark:at08-monitor-bot", pid=89051),
        ]
    )

    identity = production_identity(before, after)

    assert identity["identity_unchanged"] is True
    assert production_unchanged(before, after) is True
    assert identity["adapters_changed"] is True
    assert identity["adapter_changes"] == [
        {
            "id": "lark:at08-review-bot",
            "field": "status",
            "before": "online",
            "after": "stale",
        },
        {
            "id": "lark:at08-review-bot",
            "field": "online",
            "before": True,
            "after": False,
        },
    ]
    assert identity["adapters_reconnected"] == ["lark:at08-review-bot"]
    assert identity["adapters_added"] == [] and identity["adapters_removed"] == []
    assert identity["configuration_unchanged"] is True
    assert production_unchanged(before, after) is True


def test_daemon_restart_is_still_a_failure():
    restarted = _snapshot(pid=PID + 1, epoch=EPOCH)
    before = _snapshot(adapters=[_adapter("lark:at08-monitor-bot")])

    identity = production_identity(before, restarted)

    assert identity["identity_unchanged"] is False
    assert production_unchanged(before, restarted) is False
    assert identity["pid_after"] == PID + 1


def test_same_pid_with_new_epoch_is_still_a_failure():
    # A restart in place can reuse the pid; the epoch is what makes it visible.
    before = _snapshot(epoch=EPOCH)
    after = _snapshot(epoch="0" * 32)

    assert production_unchanged(before, after) is False


def test_added_and_removed_adapters_are_recorded_and_still_gate():
    """An adapter appearing or disappearing is a configuration change, not churn.

    Review 2026-09-29 (cc-sw-review): `status`/`online`/`pid` flips are
    observations; the adapter *set* must keep gating the run, because a
    production adapter added or removed under a test means the test touched
    production configuration.
    """
    before = _snapshot(adapters=[_adapter("lark:gone"), _adapter("lark:kept")])
    after = _snapshot(
        adapters=[
            _adapter("lark:kept"),
            _adapter("lark:new"),
        ]
    )

    identity = production_identity(before, after)

    assert identity["identity_unchanged"] is True
    assert identity["adapters_changed"] is True
    assert identity["adapters_added"] == ["lark:new"]
    assert identity["adapters_removed"] == ["lark:gone"]
    assert identity["adapter_changes"] == []
    assert identity["configuration_unchanged"] is False
    assert production_unchanged(before, after) is False


def test_adapter_intent_flip_is_a_configuration_change_not_observable_churn():
    """Flipping ``configured``/``desired`` is the operator's intent, so it gates.

    Follow-up to the #1024 review (cc-sw-review, 2026-09-29): those two fields
    used to sit in the same list as ``status``/``online``, so a test window that
    flipped an adapter's intent -- without adding or removing it -- could still
    report ``productionUnchanged=true``.  Intent is a configuration change, not
    supervised churn: the run must fail, and the flip must be reported on
    ``intent_changes`` instead of hiding among the observations.
    """
    before = _snapshot(adapters=[_adapter("lark:at08-review-bot")])
    after = _snapshot(adapters=[_adapter("lark:at08-review-bot")])
    # Only the operator's intent moves; the daemon and the adapter's process
    # (status/online/pid) are untouched.
    after["adapters"][0]["configured"] = False
    after["adapters"][0]["desired"] = False

    identity = production_identity(before, after)

    assert identity["identity_unchanged"] is True
    assert identity["adapters_added"] == [] and identity["adapters_removed"] == []
    assert identity["adapter_changes"] == []
    assert identity["intent_changes"] == [
        {
            "id": "lark:at08-review-bot",
            "field": "configured",
            "before": True,
            "after": False,
        },
        {
            "id": "lark:at08-review-bot",
            "field": "desired",
            "before": True,
            "after": False,
        },
    ]
    assert identity["adapters_changed"] is True
    assert identity["configuration_unchanged"] is False
    assert production_unchanged(before, after) is False


def test_intent_flip_next_to_reconnect_still_gates_the_run():
    """Churn and intent can move together; intent must still fail the run."""
    before = _snapshot(adapters=[_adapter("lark:at08-monitor-bot")])
    after = _snapshot(
        adapters=[_adapter("lark:at08-monitor-bot", online=False, status="stale")]
    )
    after["adapters"][0]["desired"] = False

    identity = production_identity(before, after)

    assert identity["adapter_changes"] == [
        {
            "id": "lark:at08-monitor-bot",
            "field": "status",
            "before": "online",
            "after": "stale",
        },
        {
            "id": "lark:at08-monitor-bot",
            "field": "online",
            "before": True,
            "after": False,
        },
    ]
    assert identity["intent_changes"] == [
        {
            "id": "lark:at08-monitor-bot",
            "field": "desired",
            "before": True,
            "after": False,
        }
    ]
    assert identity["configuration_unchanged"] is False
    assert production_unchanged(before, after) is False
