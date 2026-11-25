"""Pure ownership, unknown-outcome and no-replay rules for M2 onboarding.

Decision: gui-m2-onboarding-contracts-20261105.  These tests exercise the state
module, not a runtime.  They never authenticate a real user, read a home or
open a socket.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_LOGOUT_SPEC,
    AUTH_RELOGIN_SPEC,
    DEVICE_ENSURE_SPEC,
    INITIALIZE_SPEC,
    LOGOUT_STATES,
    ONBOARDING_SPECS,
    STATUS_SPEC,
    CallContext,
    OnboardingStateError,
    FirstRunSnapshot,
    OperationStore,
    Surface,
    authorize_call,
    onboarding_status,
)
from hyprial.daemon.impl.request_catalog.onboarding.schemas import (
    CANCEL_RESULT,
    INITIALIZE_RESULT,
    STATUS_RESULT,
)
from hyprial.kernel import ipc_errors

OWNER = "user:alice@example.test"
OTHER_OWNER = "user:bob@example.test"
KEY = "opkey-0123456789abcdef"
KEY_2 = "opkey-fedcba9876543210"
OP = "op-0123456789abcdef"
OP_2 = "op-fedcba9876543210"
RCPT = "rcpt-0123456789abcdef"


def surface(generation: str = "gen-1") -> Surface:
    return Surface(
        home_binding="home-1",
        host_control_session_id="host-1",
        view_session_id="view-1",
        transport_generation=generation,
    )


def context(
    *,
    generation: str = "gen-1",
    phase: str = "bootstrap",
    caller: str = "local-bootstrap",
    owner: str | None = OWNER,
) -> CallContext:
    return CallContext(
        surface=surface(generation),
        phase=phase,
        caller=caller,
        owner=owner,
    )


def bootstrap_context(owner: str | None = None) -> CallContext:
    return context(phase="bootstrap", caller="local-bootstrap", owner=owner)


def serving_context(owner: str | None = OWNER) -> CallContext:
    return context(phase="serving", caller="local-gui", owner=owner)


def test_declared_phase_and_caller_pairs_are_enforced() -> None:
    authorize_call(STATUS_SPEC, bootstrap_context())
    authorize_call(STATUS_SPEC, serving_context())
    with pytest.raises(OnboardingStateError) as wrong_phase:
        authorize_call(INITIALIZE_SPEC, serving_context())
    assert wrong_phase.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    with pytest.raises(OnboardingStateError) as wrong_caller:
        authorize_call(
            STATUS_SPEC,
            context(phase="bootstrap", caller="local-gui"),
        )
    assert wrong_caller.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_the_daemon_local_operator_is_a_serving_caller() -> None:
    """The daemon's own operator surface is a real caller, not a rewrite."""

    context = CallContext(
        surface=surface(),
        phase="serving",
        caller="local-operator",
        owner=OWNER,
    )
    assert context.caller == "local-operator"
    authorize_call(STATUS_SPEC, context)
    authorize_call(AUTH_RELOGIN_SPEC, context)
    with pytest.raises(OnboardingStateError) as bootstrap_refused:
        authorize_call(
            STATUS_SPEC,
            CallContext(
                surface=surface(),
                phase="bootstrap",
                caller="local-operator",
                owner=None,
            ),
        )
    assert bootstrap_refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_logout_and_relogin_require_the_admitted_caller_in_both_phases() -> None:
    for spec in (AUTH_RELOGIN_SPEC, AUTH_LOGOUT_SPEC):
        authorize_call(spec, bootstrap_context())
        authorize_call(spec, serving_context())
        with pytest.raises(OnboardingStateError) as refused:
            authorize_call(
                spec,
                CallContext(
                    surface=surface(),
                    phase="bootstrap",
                    caller="local-gui",
                ),
            )
        assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_serving_phase_rejects_every_local_bootstrap_call() -> None:
    for spec in ONBOARDING_SPECS:
        with pytest.raises(OnboardingStateError) as caught:
            authorize_call(
                spec,
                context(phase="serving", caller="local-bootstrap"),
            )
        assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_bootstrap_only_methods_reject_serving_even_with_local_gui() -> None:
    for spec in (DEVICE_ENSURE_SPEC, INITIALIZE_SPEC):
        with pytest.raises(OnboardingStateError) as caught:
            authorize_call(spec, serving_context())
        assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_login_begin_is_idempotent_and_blocks_a_second_intent() -> None:
    store = OperationStore()
    first = store.begin_auth(bootstrap_context(), "login", KEY, OP)
    replay = store.begin_auth(bootstrap_context(), "login", KEY, OP_2)
    assert replay.operation_id == first.operation_id
    with pytest.raises(OnboardingStateError) as conflict:
        store.begin_auth(bootstrap_context(), "register", KEY, OP_2)
    assert conflict.value.code == "AUTH_OPERATION_CONFLICT"
    with pytest.raises(OnboardingStateError) as pending:
        store.begin_auth(bootstrap_context(), "login", KEY_2, OP_2)
    assert pending.value.code == ipc_errors.OPERATION_IN_PROGRESS


def test_authenticated_identity_cannot_be_cancelled_or_replayed() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    authenticated = store.mark_authenticated(bootstrap_context(), OP, OWNER)
    assert authenticated.state == "authenticated"
    outcome = store.cancel(context(), OP)
    assert outcome.cancelled is False
    assert outcome.reason == "identity-committed"
    assert store.get(context(), OP).state == "authenticated"
    with pytest.raises(OnboardingStateError) as conflict:
        store.mark_authenticated(bootstrap_context(), OP, OTHER_OWNER)
    assert conflict.value.code == ipc_errors.IDENTITY_CONFLICT


def test_wrong_home_or_view_cannot_enumerate_an_operation() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    for changed in (
        Surface("home-2", "host-1", "view-1", "gen-1"),
        Surface("home-1", "host-2", "view-1", "gen-1"),
        Surface("home-1", "host-1", "view-2", "gen-1"),
    ):
        probe = CallContext(changed, "bootstrap", "local-bootstrap", None)
        with pytest.raises(OnboardingStateError) as caught:
            store.get(probe, OP)
        assert caught.value.code == "AUTH_OPERATION_NOT_FOUND"


def test_wrong_owner_is_refused_after_the_owner_binds() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    store.mark_authenticated(bootstrap_context(), OP, OWNER)
    with pytest.raises(OnboardingStateError) as caught:
        store.get(context(owner=OTHER_OWNER), OP)
    assert caught.value.code == ipc_errors.IDENTITY_CONFLICT


def test_unbound_pending_auth_is_not_visible_to_a_bound_owner() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    other = context(owner=OTHER_OWNER)
    with pytest.raises(OnboardingStateError) as hidden_get:
        store.get(other, OP)
    assert hidden_get.value.code == ipc_errors.IDENTITY_CONFLICT
    with pytest.raises(OnboardingStateError) as hidden_cancel:
        store.cancel(other, OP)
    assert hidden_cancel.value.code == ipc_errors.IDENTITY_CONFLICT
    assert store.visible(other) == ()
    status = onboarding_status(store, other)
    assert status["authenticated"] is True
    assert status["operations"] == []
    assert store.get(bootstrap_context(), OP).state == "pending"
    assert store.cancel(bootstrap_context(), OP).cancelled is True


def test_owner_bound_pending_auth_is_not_visible_to_another_owner_or_anonymous() -> None:
    store = OperationStore()
    store.begin_auth(context(owner=OWNER), "login", KEY, OP)
    assert store.get(context(owner=OWNER), OP).account_owner == OWNER
    for probe in (context(owner=OTHER_OWNER), bootstrap_context()):
        with pytest.raises(OnboardingStateError) as hidden_get:
            store.get(probe, OP)
        assert hidden_get.value.code == ipc_errors.IDENTITY_CONFLICT
        with pytest.raises(OnboardingStateError) as hidden_cancel:
            store.cancel(probe, OP)
        assert hidden_cancel.value.code == ipc_errors.IDENTITY_CONFLICT
        assert store.visible(probe) == ()
        assert onboarding_status(store, probe)["operations"] == []
    assert [item.operation_id for item in store.visible(context(owner=OWNER))] == [OP]


def test_auth_binding_moves_visibility_from_anonymous_to_the_exact_owner() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    assert [item.operation_id for item in store.visible(bootstrap_context())] == [OP]
    store.mark_authenticated(bootstrap_context(), OP, OWNER)
    assert store.visible(bootstrap_context()) == ()
    assert [item.operation_id for item in store.visible(context(owner=OWNER))] == [OP]
    with pytest.raises(OnboardingStateError) as other_owner:
        store.get(context(owner=OTHER_OWNER), OP)
    assert other_owner.value.code == ipc_errors.IDENTITY_CONFLICT


def test_cross_generation_calls_without_recovery_proof_are_rejected() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    store.mark_authenticated(bootstrap_context(), OP, OWNER)
    newer = context(generation="gen-2", owner=OWNER)
    with pytest.raises(OnboardingStateError) as stale_get:
        store.get(newer, OP)
    assert stale_get.value.code == ipc_errors.STALE_DAEMON_GENERATION
    with pytest.raises(OnboardingStateError) as stale_cancel:
        store.cancel(newer, OP)
    assert stale_cancel.value.code == ipc_errors.STALE_DAEMON_GENERATION
    status = onboarding_status(store, newer)
    assert status["operations"] == []
    assert status["authenticated"] is True


@pytest.mark.parametrize("state", ["accepted", "promoting", "unknown"])
def test_a_new_generation_cannot_silently_resume_an_old_receipt(state) -> None:
    store = OperationStore()
    store.begin_initialize(context(), KEY, OP, RCPT)
    if state != "accepted":
        store.claim_promotion(context(), OP)
        store.mark_initialize(context(), OP, state)
    with pytest.raises(OnboardingStateError) as stale:
        store.begin_initialize(
            context(generation="gen-2"),
            KEY_2,
            OP_2,
            RCPT,
        )
    assert stale.value.code == ipc_errors.STALE_DAEMON_GENERATION
    assert store.get(context(), OP).state == state


def test_owner_is_a_verified_condition_not_a_caller_kind() -> None:
    store = OperationStore()
    anonymous = serving_context(owner=None)
    authorize_call(STATUS_SPEC, anonymous)
    status = onboarding_status(store, anonymous)
    assert status["authenticated"] is False
    assert status["owner"] is None
    with pytest.raises(OnboardingStateError) as unbound:
        store.begin_initialize(bootstrap_context(owner=None), KEY, OP, RCPT)
    assert unbound.value.code == ipc_errors.IDENTITY_UNBOUND


def test_status_snapshot_only_exposes_the_current_home_view_and_generation() -> None:
    store = OperationStore()
    store.begin_initialize(context(), KEY, OP, RCPT)
    store.claim_promotion(context(), OP)
    store.mark_initialize(context(), OP, "completed")
    own = onboarding_status(store, context())
    assert own["authenticated"] is True
    assert own["initialized"] is True
    assert [item["operationId"] for item in own["operations"]] == [OP]
    anonymous = onboarding_status(store, bootstrap_context(owner=None))
    assert anonymous["authenticated"] is False
    assert anonymous["owner"] is None
    assert anonymous["initialized"] is False
    assert anonymous["operations"] == []
    newer = onboarding_status(store, context(generation="gen-2"))
    assert newer["operations"] == []
    assert newer["initialized"] is False
    other_home = CallContext(
        Surface("home-2", "host-1", "view-1", "gen-1"),
        "bootstrap",
        "local-bootstrap",
        OWNER,
    )
    assert onboarding_status(store, other_home)["operations"] == []


def test_operation_ids_cannot_be_reused_across_begin_paths() -> None:
    other_view = CallContext(
        Surface("home-1", "host-1", "view-2", "gen-1"),
        "bootstrap",
        "local-bootstrap",
        OWNER,
    )
    auth_store = OperationStore()
    auth_store.begin_auth(bootstrap_context(), "login", KEY, OP)
    with pytest.raises(OnboardingStateError) as auth_conflict:
        auth_store.begin_auth(other_view, "login", KEY_2, OP)
    assert auth_conflict.value.code == "AUTH_OPERATION_CONFLICT"
    assert auth_store.get(bootstrap_context(), OP).kind == "login"
    initialize_store = OperationStore()
    initialize_store.begin_initialize(context(), KEY, OP, RCPT)
    with pytest.raises(OnboardingStateError) as initialize_conflict:
        initialize_store.begin_initialize(other_view, KEY_2, OP, RCPT)
    assert initialize_conflict.value.code == "AUTH_OPERATION_CONFLICT"
    assert initialize_store.get(context(), OP).state == "accepted"
    device_store = OperationStore()
    device_store.begin_device(context(), KEY, OP, RCPT)
    with pytest.raises(OnboardingStateError) as device_conflict:
        device_store.begin_device(other_view, KEY_2, OP, RCPT)
    assert device_conflict.value.code == "AUTH_OPERATION_CONFLICT"
    assert device_store.get(context(), OP).state == "pending"


def test_operation_keys_are_scoped_by_home_host_and_view() -> None:
    store = OperationStore()
    first = store.begin_auth(bootstrap_context(), "login", KEY, OP)
    other_home = CallContext(
        Surface("home-2", "host-1", "view-1", "gen-1"),
        "bootstrap",
        "local-bootstrap",
        None,
    )
    second = store.begin_auth(other_home, "login", KEY, OP_2)
    assert second.operation_id == OP_2
    assert second.operation_id != first.operation_id
    with pytest.raises(OnboardingStateError) as hidden:
        store.get(bootstrap_context(), OP_2)
    assert hidden.value.code == "AUTH_OPERATION_NOT_FOUND"


def test_unknown_auth_outcome_is_preserved_and_not_cancelled() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    unknown = store.mark_auth_unknown(bootstrap_context(), OP)
    assert unknown.state == "unknown"
    outcome = store.cancel(bootstrap_context(), OP)
    assert outcome.cancelled is False
    assert outcome.reason == "outcome-unknown"
    assert store.get(bootstrap_context(), OP).state == "unknown"
    with pytest.raises(OnboardingStateError) as conflict:
        store.mark_authenticated(bootstrap_context(), OP, OWNER)
    assert conflict.value.code == "AUTH_OPERATION_CONFLICT"
    status = onboarding_status(store, context(owner=None))
    assert status["operations"][0]["state"] == "unknown"


def test_auth_outcomes_end_once_and_never_reopen() -> None:
    store = OperationStore()
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    expired = store.mark_auth_ended(bootstrap_context(), OP, "expired")
    assert expired.state == "expired"
    assert expired.cancelable is False
    assert store.mark_auth_ended(bootstrap_context(), OP, "expired") == expired
    with pytest.raises(OnboardingStateError) as reopen:
        store.mark_auth_ended(bootstrap_context(), OP, "denied")
    assert reopen.value.code == "AUTH_OPERATION_CONFLICT"
    with pytest.raises(OnboardingStateError) as invalid:
        store.mark_auth_ended(bootstrap_context(), OP, "failed")
    assert invalid.value.code == "AUTH_OPERATION_CONFLICT"
    assert onboarding_status(store, context(owner=None))["operations"][0]["state"] == "expired"


def test_only_a_pending_auth_operation_can_end() -> None:
    store = OperationStore()
    store.begin_initialize(context(), KEY, OP, RCPT)
    with pytest.raises(OnboardingStateError) as wrong_kind:
        store.mark_auth_ended(context(), OP, "unknown")
    assert wrong_kind.value.code == "AUTH_OPERATION_CONFLICT"


def test_initialize_requires_owner_and_never_replays() -> None:
    store = OperationStore()
    with pytest.raises(OnboardingStateError) as unbound:
        store.begin_initialize(context(owner=None), KEY, OP, RCPT)
    assert unbound.value.code == ipc_errors.IDENTITY_UNBOUND
    accepted = store.begin_initialize(context(), KEY, OP, RCPT)
    replay = store.begin_initialize(context(), KEY_2, OP_2, RCPT)
    assert replay.operation_id == accepted.operation_id
    assert replay.receipt_id == RCPT
    store.claim_promotion(context(), OP)
    completed = store.mark_initialize(context(), OP, "completed")
    assert completed.state == "completed"
    assert completed.cancelable is False
    still_completed = store.begin_initialize(context(), KEY_2, OP_2, RCPT)
    assert still_completed.state == "completed"
    with pytest.raises(OnboardingStateError) as replay_conflict:
        store.mark_initialize(context(), OP, "accepted")
    assert replay_conflict.value.code == "AUTH_OPERATION_CONFLICT"


def test_initialize_cancel_before_promotion_is_real_and_idempotent() -> None:
    store = OperationStore()
    store.begin_initialize(context(), KEY, OP, RCPT)
    first = store.cancel(context(), OP)
    assert first.state == "cancelled"
    assert first.cancelled is True
    assert first.reason == "cancelled"
    second = store.cancel(context(), OP)
    assert second.state == "cancelled"
    assert second.cancelled is True
    assert second.reason == "already-cancelled"
    claim = store.claim_promotion(context(), OP)
    assert claim.started is False
    assert claim.record.state == "cancelled"
    with pytest.raises(OnboardingStateError) as terminal:
        store.mark_initialize(context(), OP, "completed")
    assert terminal.value.code == "AUTH_OPERATION_CONFLICT"


def test_initialize_requires_claim_before_the_promotion_boundary() -> None:
    store = OperationStore()
    store.begin_initialize(context(), KEY, OP, RCPT)
    with pytest.raises(OnboardingStateError) as skipped:
        store.mark_initialize(context(), OP, "completed")
    assert skipped.value.code == "AUTH_OPERATION_CONFLICT"
    claim = store.claim_promotion(context(), OP)
    assert claim.started is True
    assert claim.record.state == "promoting"
    assert claim.record.cancelable is False
    refused = store.cancel(context(), OP)
    assert refused.state == "promoting"
    assert refused.cancelled is False
    assert refused.reason == "not-cancellable"
    completed = store.mark_initialize(context(), OP, "completed")
    assert completed.state == "completed"
    committed = store.cancel(context(), OP)
    assert committed.state == "completed"
    assert committed.cancelled is False
    assert committed.reason == "identity-committed"


def test_initialize_unknown_can_resolve_to_completed_without_replay() -> None:
    store = OperationStore()
    store.begin_initialize(context(), KEY, OP, RCPT)
    store.claim_promotion(context(), OP)
    unknown = store.mark_initialize(context(), OP, "unknown")
    assert unknown.state == "unknown"
    refused = store.cancel(context(), OP)
    assert refused.state == "unknown"
    assert refused.cancelled is False
    assert refused.reason == "outcome-unknown"
    resumed = store.begin_initialize(context(), KEY_2, OP_2, RCPT)
    assert resumed.operation_id == OP
    assert resumed.state == "unknown"
    completed = store.mark_initialize(context(), OP, "completed")
    assert completed.state == "completed"
    assert completed.cancelable is False


def test_failed_initialize_cannot_be_cancelled_or_replayed() -> None:
    store = OperationStore()
    store.begin_initialize(context(), KEY, OP, RCPT)
    store.claim_promotion(context(), OP)
    failed = store.mark_initialize(context(), OP, "failed")
    assert failed.state == "failed"
    refused = store.cancel(context(), OP)
    assert refused.state == "failed"
    assert refused.cancelled is False
    assert refused.reason == "not-cancellable"
    resumed = store.begin_initialize(context(), KEY_2, OP_2, RCPT)
    assert resumed.operation_id == OP
    assert resumed.state == "failed"
    with pytest.raises(OnboardingStateError) as terminal:
        store.mark_initialize(context(), OP, "completed")
    assert terminal.value.code == "AUTH_OPERATION_CONFLICT"


def test_cancel_and_initialize_schemas_describe_the_promotion_boundary() -> None:
    cancel = Draft202012Validator(CANCEL_RESULT)
    assert cancel.is_valid(
        {
            "ok": True,
            "operationId": OP,
            "state": "promoting",
            "cancelled": False,
            "reason": "not-cancellable",
        }
    )
    assert cancel.is_valid(
        {
            "ok": True,
            "operationId": OP,
            "state": "completed",
            "cancelled": False,
            "reason": "identity-committed",
        }
    )
    assert cancel.is_valid(
        {
            "ok": True,
            "operationId": OP,
            "state": "failed",
            "cancelled": False,
            "reason": "not-cancellable",
        }
    )
    assert not cancel.is_valid(
        {
            "ok": True,
            "operationId": OP,
            "state": "promoting",
            "cancelled": True,
            "reason": "cancelled",
        }
    )
    initialize = Draft202012Validator(INITIALIZE_RESULT)
    for state in ("promoting", "cancelled", "failed"):
        assert initialize.is_valid(
            {
                "ok": True,
                "operationId": OP,
                "receiptId": RCPT,
                "state": state,
                "initialized": False,
                "cancelable": False,
            }
        )
    assert not initialize.is_valid(
        {
            "ok": True,
            "operationId": OP,
            "receiptId": RCPT,
            "state": "accepted",
            "initialized": True,
            "cancelable": True,
        }
    )
    assert not initialize.is_valid(
        {
            "ok": True,
            "operationId": OP,
            "receiptId": RCPT,
            "state": "promoting",
            "initialized": False,
            "cancelable": True,
        }
    )


def test_device_failure_preserves_identity_and_allows_a_new_attempt() -> None:
    store = OperationStore()
    pending = store.begin_device(context(), KEY, OP, RCPT)
    assert pending.state == "pending"
    failed = store.mark_device(context(), OP, "failed")
    assert failed.state == "failed"
    visible = store.visible(context())
    assert [record.operation_id for record in visible] == [OP]
    retry = store.begin_device(context(), KEY_2, OP_2, RCPT)
    assert retry.operation_id == OP_2
    ready = store.mark_device(context(), OP_2, "ready")
    assert ready.state == "ready"
    snapshot = onboarding_status(store, context())
    assert snapshot["deviceReady"] is True


def test_operation_keys_are_namespaced_by_kind() -> None:
    store = OperationStore()
    initialize = store.begin_initialize(context(), KEY, OP, RCPT)
    device = store.begin_device(context(), KEY, OP_2, RCPT)
    assert initialize.kind == "initialize"
    assert device.kind == "device"
    assert initialize.operation_id != device.operation_id


def test_status_orders_actions_after_identity_device_and_initialize() -> None:
    store = OperationStore()
    fresh = onboarding_status(store, context(owner=None))
    assert fresh["nextActions"] == ["login", "register"]
    store.begin_auth(bootstrap_context(), "login", KEY, OP)
    store.mark_authenticated(bootstrap_context(), OP, OWNER)
    authenticated = onboarding_status(store, context())
    assert authenticated["nextActions"] == ["device.ensure"]
    store.begin_device(context(), KEY, OP_2, RCPT)
    store.mark_device(context(), OP_2, "ready")
    device = onboarding_status(store, context())
    assert device["nextActions"] == ["initialize"]
    store.begin_initialize(context(), KEY_2, "op-1111111111111111", RCPT)
    store.claim_promotion(context(), "op-1111111111111111")
    store.mark_initialize(context(), "op-1111111111111111", "completed")
    done = onboarding_status(store, context())
    assert done["nextActions"] == []
    assert done["initialized"] is True


def test_logout_is_accepted_once_and_is_never_cancellable() -> None:
    store = OperationStore()
    accepted = store.begin_logout(serving_context(), KEY, OP, RCPT)
    assert accepted.kind == "logout"
    assert accepted.state == "accepted"
    assert accepted.receipt_id == RCPT
    assert accepted.cancelable is False
    assert accepted.removed == ()
    refused = store.cancel(serving_context(), OP)
    assert refused.cancelled is False
    assert refused.reason == "not-cancellable"
    assert refused.state == "accepted"
    assert store.get(serving_context(), OP).state == "accepted"


def test_second_logout_returns_the_first_receipt_without_a_second_side_effect() -> None:
    store = OperationStore()
    first = store.begin_logout(serving_context(), KEY, OP, RCPT)
    again = store.begin_logout(serving_context(), KEY, OP, RCPT)
    other_key = store.begin_logout(serving_context(), KEY_2, OP_2, RCPT)
    assert again == first
    assert other_key.operation_id == first.operation_id
    assert [record.operation_id for record in store.visible(serving_context())] == [OP]


def test_logout_conflicts_with_a_pending_auth_operation() -> None:
    store = OperationStore()
    store.begin_auth(serving_context(), "login", KEY, OP)
    with pytest.raises(OnboardingStateError) as conflict:
        store.begin_logout(serving_context(), KEY_2, OP_2, RCPT)
    assert conflict.value.code == ipc_errors.OPERATION_IN_PROGRESS
    assert [record.kind for record in store.visible(serving_context())] == ["login"]


def test_logout_completion_states_are_monotonic_and_record_removal() -> None:
    store = OperationStore()
    store.begin_logout(serving_context(), KEY, OP, RCPT)
    unknown = store.mark_logout(serving_context(), OP, "unknown")
    assert unknown.state == "unknown"
    # An unknown outcome may never become ``failed`` (that would invent a
    # rollback), but a restarted coordinator may reconcile it to completed from
    # observed artifacts.
    with pytest.raises(OnboardingStateError) as refused:
        store.mark_logout(serving_context(), OP, "failed")
    assert refused.value.code == "AUTH_OPERATION_CONFLICT"
    reconciled = store.mark_logout(
        serving_context(), OP, "completed", removed=("secrets/login.json",)
    )
    assert reconciled.removed == ("secrets/login.json",)
    with pytest.raises(OnboardingStateError):
        store.mark_logout(serving_context(), OP, "unknown")
    store_two = OperationStore()
    store_two.begin_logout(serving_context(), KEY, OP, RCPT)
    completed = store_two.mark_logout(
        serving_context(), OP, "completed", removed=("secrets/login.json",)
    )
    assert completed.state == "completed"
    assert completed.removed == ("secrets/login.json",)
    assert completed.cancelable is False
    with pytest.raises(OnboardingStateError) as terminal:
        store_two.mark_logout(serving_context(), OP, "failed")
    assert terminal.value.code == "AUTH_OPERATION_CONFLICT"


def test_logout_removal_labels_are_allowlisted_ordered_and_unique() -> None:
    store = OperationStore()
    store.begin_logout(serving_context(), KEY, OP, RCPT)
    for bad in (
        ("~/.ssh/id_ed25519",),
        ("settings.owner", "secrets/login.json"),
        ("secrets/login.json", "secrets/login.json"),
        ("",),
    ):
        with pytest.raises(OnboardingStateError) as refused:
            store.mark_logout(serving_context(), OP, "completed", removed=bad)
        assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert store.get(serving_context(), OP).state == "accepted"
    ordered = store.mark_logout(
        serving_context(),
        OP,
        "completed",
        removed=("secrets/login.json", "settings.owner"),
    )
    assert ordered.removed == ("secrets/login.json", "settings.owner")


def test_bootstrap_ownerless_logout_completes_as_a_noop() -> None:
    store = OperationStore()
    anonymous = bootstrap_context(owner=None)
    accepted = store.begin_logout(anonymous, KEY, OP, RCPT)
    assert accepted.state == "accepted"
    completed = store.mark_logout(anonymous, OP, "completed", removed=())
    assert completed.state == "completed"
    assert completed.removed == ()
    snapshot = onboarding_status(store, anonymous)
    assert snapshot["phase"] == "bootstrap"
    assert snapshot["authenticated"] is False
    assert snapshot["nextActions"] == ["login", "register"]
    assert [item["kind"] for item in snapshot["operations"]] == ["logout"]
    assert snapshot["operations"][0]["state"] == "completed"


def test_status_operation_schema_names_logout_as_a_kind() -> None:
    validator = Draft202012Validator(STATUS_RESULT)
    assert validator.is_valid(
        {
            "ok": True,
            "phase": "serving",
            "authenticated": True,
            "owner": OWNER,
            "deviceReady": True,
            "initialized": False,
            "operations": [
                {
                    "operationId": OP,
                    "kind": "logout",
                    "state": "completed",
                    "receiptId": RCPT,
                }
            ],
            "nextActions": [],
        }
    )
    assert not validator.is_valid(
        {
            "ok": True,
            "phase": "serving",
            "authenticated": True,
            "owner": OWNER,
            "deviceReady": True,
            "initialized": False,
            "operations": [
                {
                    "operationId": OP,
                    "kind": "logout",
                    "state": "bogus",
                    "receiptId": RCPT,
                }
            ],
            "nextActions": [],
        }
    )


def test_logout_states_are_a_closed_set() -> None:
    assert set(LOGOUT_STATES) == {"accepted", "completed", "failed", "unknown"}
    store = OperationStore()
    store.begin_logout(serving_context(), KEY, OP, RCPT)
    with pytest.raises(OnboardingStateError) as refused:
        store.mark_logout(serving_context(), OP, "cancelled")
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_failed_logout_receipt_keeps_the_removed_labels_it_reports() -> None:
    """A partial removal must stay truthful after it is persisted.

    The contract allows ``removed`` on ``failed``; dropping the argument here
    would let the first response name the artifacts it really removed while the
    durable receipt claimed none.
    """

    store = OperationStore()
    store.begin_logout(serving_context(), KEY, OP, RCPT)
    failed = store.mark_logout(
        serving_context(),
        OP,
        "failed",
        removed=("secrets/login.json", "settings.owner"),
    )
    assert failed.state == "failed"
    assert failed.removed == ("secrets/login.json", "settings.owner")
    # The persisted receipt keeps them as well: a later read is not a rollback.
    assert store.get(serving_context(), OP).removed == (
        "secrets/login.json",
        "settings.owner",
    )
    # ``unknown`` still never invents labels.
    store_two = OperationStore()
    store_two.begin_logout(serving_context(), KEY, OP, RCPT)
    unknown = store_two.mark_logout(
        serving_context(), OP, "unknown", removed=("secrets/login.json",)
    )
    assert unknown.state == "unknown"
    assert unknown.removed == ()


def test_status_projects_first_run_receipt_without_changing_existing_fields() -> None:
    store = OperationStore()
    snapshot = FirstRunSnapshot(
        state="recorded",
        revision=2,
        confirmed=("configuration",),
        updated_at="2026-11-18T00:00:00Z",
    )
    status = onboarding_status(store, context(), first_run=snapshot)
    assert status["firstRun"] == {
        "state": "recorded",
        "revision": 2,
        "confirmed": ["configuration"],
        "updatedAt": "2026-11-18T00:00:00Z",
        "skipped": False,
    }
    assert status["initialized"] is False
    assert status["nextActions"] == ["device.ensure"]


def test_status_first_run_distinguishes_corrupt_from_no_history() -> None:
    store = OperationStore()
    no_history = onboarding_status(
        store, context(), first_run=FirstRunSnapshot(state="none")
    )
    corrupt = onboarding_status(
        store, context(), first_run=FirstRunSnapshot(state="corrupt")
    )
    assert no_history["firstRun"]["state"] == "none"
    assert corrupt["firstRun"]["state"] == "corrupt"
    assert no_history["firstRun"]["confirmed"] == []
    assert corrupt["firstRun"]["revision"] == 0


def test_status_result_accepts_first_run_extension() -> None:
    validator = Draft202012Validator(STATUS_RESULT)
    assert validator.is_valid(
        {
            "ok": True,
            "phase": "serving",
            "authenticated": True,
            "owner": OWNER,
            "deviceReady": True,
            "initialized": True,
            "operations": [],
            "nextActions": [],
            "firstRun": {
                "state": "recorded",
                "revision": 1,
                "confirmed": ["configuration"],
                "updatedAt": "2026-11-18T00:00:00Z",
            },
        }
    )
