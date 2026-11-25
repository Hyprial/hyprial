"""M2 onboarding contracts stay declarative until the common line registers them.

Decision: gui-m2-onboarding-contracts-20261105.  These tests build an isolated
MethodRegistry with explicitly unimplemented handlers, so the contract is
reviewable without claiming a BootstrapRuntime or registering production
handlers.  The same specs and examples are the only source for catalog,
OpenAPI and Mock projections.
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon import daemon_catalog
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY, MethodRegistry
from hyprial.kernel import (
    DaemonRequestError,
    MethodSpecError,
    PhaseCallerSpec,
    export_catalog,
    export_openapi,
    ipc_errors,
    validate_method_example_sets,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    ONBOARDING_EXAMPLES,
    ONBOARDING_SPECS,
)
from hyprial.daemon.impl.request_catalog.onboarding.schemas import (
    LOGOUT_FAILURE_REASONS,
    LOGOUT_PARAMS,
    LOGOUT_REMOVED_LABELS,
    LOGOUT_RESULT,
    LOGOUT_UNKNOWN_REASONS,
    RELOGIN_RESULT,
)
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    DEVICE_ENSURE_FAILED,
    REGISTRATION_UNAVAILABLE,
)

NAMES = frozenset(
    {
        "onboarding.status",
        "auth.login.begin",
        "auth.operation.get",
        "auth.operation.cancel",
        "onboarding.device.ensure",
        "onboarding.initialize",
        "auth.logout",
        "auth.relogin",
        "onboarding.firstRun.begin",
        "onboarding.firstRun.bind",
        "onboarding.firstRun.confirm",
        "onboarding.firstRun.cancel",
        "onboarding.firstRun.skip",
    }
)
WRITES = frozenset(
    {
        "auth.login.begin",
        "auth.operation.cancel",
        "onboarding.device.ensure",
        "onboarding.initialize",
        "auth.logout",
        "auth.relogin",
        "onboarding.firstRun.begin",
        "onboarding.firstRun.bind",
        "onboarding.firstRun.confirm",
        "onboarding.firstRun.cancel",
        "onboarding.firstRun.skip",
    }
)
BOOTSTRAP_ONLY = frozenset({"onboarding.device.ensure", "onboarding.initialize"})
SERVING_ONLY = frozenset(
    {
        "onboarding.firstRun.begin",
        "onboarding.firstRun.bind",
        "onboarding.firstRun.confirm",
        "onboarding.firstRun.cancel",
        "onboarding.firstRun.skip",
    }
)

# Plan section 3.1 error groups.  These are asserted as exact sets: an
# undeclared internal code can never travel on the wire, and a code the plan
# requires cannot silently disappear from a declaration.
_READ_ERRORS = frozenset(
    {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.STALE_DAEMON_GENERATION,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_RESTORING,
        ipc_errors.DAEMON_ERROR,
    }
)
_OPERATION_ERRORS = frozenset(
    {
        ipc_errors.INVALID_ARGUMENT,
        AUTH_OPERATION_NOT_FOUND,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.STALE_DAEMON_GENERATION,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
    }
)
_BEGIN_ERRORS = frozenset(
    {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.IDENTITY_CONFLICT,
        ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
        REGISTRATION_UNAVAILABLE,
        AUTH_OPERATION_CONFLICT,
        ipc_errors.OPERATION_IN_PROGRESS,
        ipc_errors.STALE_DAEMON_GENERATION,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
    }
)
_DEVICE_ERRORS = frozenset(
    {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.IDENTITY_UNBOUND,
        ipc_errors.IDENTITY_CONFLICT,
        DEVICE_ENSURE_FAILED,
        ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
        ipc_errors.OPERATION_IN_PROGRESS,
        ipc_errors.STALE_DAEMON_GENERATION,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
        AUTH_OPERATION_CONFLICT,
    }
)
_INITIALIZE_ERRORS = frozenset(
    {
        DEVICE_ENSURE_FAILED,
        ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.IDENTITY_UNBOUND,
        ipc_errors.IDENTITY_CONFLICT,
        ipc_errors.OPERATION_IN_PROGRESS,
        ipc_errors.STALE_DAEMON_GENERATION,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
        AUTH_OPERATION_CONFLICT,
    }
)
_LOGOUT_ERRORS = frozenset(
    {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        AUTH_OPERATION_CONFLICT,
        ipc_errors.OPERATION_IN_PROGRESS,
        ipc_errors.IDENTITY_CONFLICT,
        ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
        ipc_errors.STALE_DAEMON_GENERATION,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
        ipc_errors.DAEMON_ERROR,
    }
)
_RELOGIN_ERRORS = _BEGIN_ERRORS - {REGISTRATION_UNAVAILABLE}
_FIRST_RUN_ERRORS = frozenset(
    {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.IDENTITY_UNBOUND,
        "FIRST_RUN_SUBJECT_REQUIRED",
        "FIRST_RUN_CONFIGURATION_CHANGED",
        "FIRST_RUN_REPLY_UNVERIFIED",
        "FIRST_RUN_ATTEMPT_CONFLICT",
        "FIRST_RUN_ATTEMPT_STALE",
        "FIRST_RUN_ATTEMPT_NOT_FOUND",
        "FIRST_RUN_ATTEMPT_NOT_BOUND",
        "FIRST_RUN_REVISION_CONFLICT",
        "FIRST_RUN_OPERATION_KEY_CONFLICT",
        "FIRST_RUN_STORE_UNAVAILABLE",
        "FIRST_RUN_STORE_CORRUPT",
        "FIRST_RUN_STORE_WRITE_FAILED",
        "FIRST_RUN_STORE_COMMIT_UNCONFIRMED",
        ipc_errors.STALE_DAEMON_GENERATION,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
    }
)

ERROR_GROUPS = {
    "onboarding.status": _READ_ERRORS,
    "auth.login.begin": _BEGIN_ERRORS,
    "auth.operation.get": _OPERATION_ERRORS,
    "auth.operation.cancel": _OPERATION_ERRORS,
    "onboarding.device.ensure": _DEVICE_ERRORS,
    "onboarding.initialize": _INITIALIZE_ERRORS,
    "auth.logout": _LOGOUT_ERRORS,
    "auth.relogin": _RELOGIN_ERRORS,
    "onboarding.firstRun.begin": _FIRST_RUN_ERRORS,
    "onboarding.firstRun.bind": _FIRST_RUN_ERRORS,
    "onboarding.firstRun.confirm": _FIRST_RUN_ERRORS,
    "onboarding.firstRun.cancel": _FIRST_RUN_ERRORS,
    "onboarding.firstRun.skip": _FIRST_RUN_ERRORS,
}

PAIRS = {
    "onboarding.status": (
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
    "auth.login.begin": (
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
    "auth.operation.get": (
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
    "auth.operation.cancel": (
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
    "onboarding.device.ensure": (("bootstrap", "local-bootstrap"),),
    "onboarding.initialize": (("bootstrap", "local-bootstrap"),),
    "auth.logout": (
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
    "auth.relogin": (
        ("bootstrap", "local-bootstrap"),
        ("serving", "local-operator"),
        ("serving", "local-gui"),
    ),
    **{
        name: (
            ("serving", "local-operator"),
            ("serving", "local-gui"),
        )
        for name in SERVING_ONLY
    },
}


def _spec(name: str):
    return next(spec for spec in ONBOARDING_SPECS if spec.name == name)
FORBIDDEN_PARAMS = frozenset(
    {
        "owner",
        "home",
        "homeBinding",
        "view",
        "viewSessionId",
        "hostControlSessionId",
        "transportGeneration",
        "generation",
        "phase",
        "caller",
    }
)


def _unimplemented(_app, _params, *, trusted_message_origin=None):
    raise DaemonRequestError(
        ipc_errors.DAEMON_NOT_READY, "onboarding runtime is not assembled"
    )


@pytest.fixture(scope="module")
def registry() -> MethodRegistry:
    return MethodRegistry.from_bindings(
        (spec, _unimplemented) for spec in ONBOARDING_SPECS
    )


def test_declares_exactly_the_thirteen_onboarding_methods(registry: MethodRegistry) -> None:
    assert set(registry.names) == NAMES
    assert [spec.name for spec in ONBOARDING_SPECS] == [
        "onboarding.status",
        "auth.login.begin",
        "auth.operation.get",
        "auth.operation.cancel",
        "onboarding.device.ensure",
        "onboarding.initialize",
        "auth.logout",
        "auth.relogin",
        "onboarding.firstRun.begin",
        "onboarding.firstRun.bind",
        "onboarding.firstRun.confirm",
        "onboarding.firstRun.cancel",
        "onboarding.firstRun.skip",
    ]


def test_examples_validate_against_the_same_declarations() -> None:
    validate_method_example_sets(ONBOARDING_SPECS, ONBOARDING_EXAMPLES)
    assert {item.method for item in ONBOARDING_EXAMPLES} == NAMES


def test_declarations_use_the_phase_and_retry_profile(
    registry: MethodRegistry,
) -> None:
    for spec in ONBOARDING_SPECS:
        assert spec.phase_caller is not None
        assert spec.audience == ("local-operator",)
        assert spec.http_exposed is True
        assert spec.http_binding is not None
        assert spec.http_binding.identity.mode == "none"
        if spec.name in WRITES:
            assert spec.mutates is True
            assert spec.retry == "never"
            assert spec.idempotency is not None
            assert spec.idempotency.mode == "never"
        else:
            assert spec.mutates is False
            assert spec.retry == "safe"
        assert spec.phase_caller.pairs == PAIRS[spec.name]
        assert not spec.phase_caller.allows("serving", "local-bootstrap")
        assert not spec.phase_caller.allows("bootstrap", "local-gui")
        if spec.name in SERVING_ONLY:
            assert not spec.phase_caller.allows("bootstrap", "local-bootstrap")


def test_phase_caller_is_an_explicit_pair_set_not_a_cartesian_product() -> None:
    for spec in ONBOARDING_SPECS:
        constraint = spec.phase_caller
        assert constraint is not None
        assert not constraint.allows("serving", "local-bootstrap")
        assert not constraint.allows("bootstrap", "local-gui")
        if spec.name in SERVING_ONLY:
            assert not constraint.allows("bootstrap", "local-bootstrap")
            assert constraint.allows("serving", "local-gui")
            assert constraint.allows("serving", "local-operator")
            continue
        assert constraint.allows("bootstrap", "local-bootstrap")
        if spec.name not in BOOTSTRAP_ONLY:
            assert constraint.allows("serving", "local-gui")
            assert constraint.allows("serving", "local-operator")


def test_only_device_and_initialize_stay_bootstrap_only() -> None:
    single_pair = {
        spec.name for spec in ONBOARDING_SPECS if len(spec.phase_caller.pairs) == 1
    }
    assert single_pair == BOOTSTRAP_ONLY


def test_error_groups_match_the_plan_exactly() -> None:
    for name, expected in ERROR_GROUPS.items():
        assert {error.code for error in _spec(name).errors} == expected, name


def test_logout_no_longer_declares_the_retired_stop_unproven_code() -> None:
    assert "DAEMON_STOP_UNPROVEN" not in {
        error.code for error in _spec("auth.logout").errors
    }
    exported = json.dumps(export_openapi(ONBOARDING_SPECS, example_sets=ONBOARDING_EXAMPLES))
    assert "DAEMON_STOP_UNPROVEN" not in exported


def test_onboarding_local_codes_are_registered_wire_codes() -> None:
    for code, registered in (
        (AUTH_OPERATION_CONFLICT, ipc_errors.AUTH_OPERATION_CONFLICT),
        (AUTH_OPERATION_NOT_FOUND, ipc_errors.AUTH_OPERATION_NOT_FOUND),
        (DEVICE_ENSURE_FAILED, ipc_errors.DEVICE_ENSURE_FAILED),
        (REGISTRATION_UNAVAILABLE, ipc_errors.REGISTRATION_UNAVAILABLE),
    ):
        assert code is registered
        assert code in ipc_errors.ALL_CODES


def test_device_and_initialize_declare_the_operation_conflict() -> None:
    for name in ("onboarding.device.ensure", "onboarding.initialize"):
        assert AUTH_OPERATION_CONFLICT in {
            error.code for error in _spec(name).errors
        }


def test_relogin_never_declares_registration_unavailable() -> None:
    assert REGISTRATION_UNAVAILABLE not in {
        error.code for error in _spec("auth.relogin").errors
    }


def test_register_intent_still_returns_registration_unavailable() -> None:
    assert REGISTRATION_UNAVAILABLE in {
        error.code for error in _spec("auth.login.begin").errors
    }
    begin_examples = next(
        item for item in ONBOARDING_EXAMPLES if item.method == "auth.login.begin"
    )
    unavailable = [
        case for case in begin_examples.cases if case.kind == "domain-error"
    ]
    assert [case.error.code for case in unavailable] == [REGISTRATION_UNAVAILABLE]
    assert unavailable[0].params["intent"] == "register"


def test_submit_outcome_unknown_carries_strict_logout_data() -> None:
    spec = _spec("auth.logout")
    declared = {
        error.code: error.data_schema for error in spec.errors
    }
    assert declared[ipc_errors.SUBMIT_OUTCOME_UNKNOWN] is not None
    validator = Draft202012Validator(declared[ipc_errors.SUBMIT_OUTCOME_UNKNOWN])
    assert validator.is_valid(
        {
            "operationId": "op-0123456789abcdef",
            "receiptId": "rcpt-0123456789abcdef",
            "identityRemoved": "unknown",
            "daemonStopped": "unknown",
            "reason": "reply-lost",
        }
    )
    assert not validator.is_valid(
        {
            "operationId": "op-0123456789abcdef",
            "receiptId": None,
            "identityRemoved": "unknown",
            "daemonStopped": "unknown",
            "reason": "reply-lost",
            "refreshToken": "never-on-the-wire",
        }
    )
    assert not validator.is_valid(
        {
            "operationId": "op-0123456789abcdef",
            "receiptId": "rcpt-0123456789abcdef",
            "identityRemoved": "unknown",
            "daemonStopped": "unknown",
            "reason": "stop-unproven",
        }
    )


def test_logout_failure_reasons_no_longer_include_stop_unproven() -> None:
    assert "stop-unproven" not in LOGOUT_FAILURE_REASONS
    assert "stop-unproven" not in LOGOUT_UNKNOWN_REASONS
    exported = json.dumps(export_openapi(ONBOARDING_SPECS, example_sets=ONBOARDING_EXAMPLES))
    assert "stop-unproven" not in exported


def test_logout_params_require_the_explicit_confirmation_literal() -> None:
    validator = Draft202012Validator(LOGOUT_PARAMS)
    key = "opkey-0123456789abcdef"
    assert validator.is_valid({"operationKey": key, "confirm": True})
    assert not validator.is_valid({"operationKey": key})
    assert not validator.is_valid({"operationKey": key, "confirm": False})
    assert not validator.is_valid(
        {"operationKey": key, "confirm": True, "owner": "user:forged@test"}
    )


def test_logout_result_encodes_the_lifecycle_boundary() -> None:
    validator = Draft202012Validator(LOGOUT_RESULT)
    base = {
        "ok": True,
        "operationId": "op-0123456789abcdef",
        "receiptId": "rcpt-0123456789abcdef",
        "issuerRevocation": "not-attempted",
    }
    accepted = {**base, "state": "accepted", "loggedOut": False,
                "removed": [], "nextAction": "poll"}
    completed = {**base, "state": "completed", "loggedOut": True,
                 "removed": ["secrets/login.json"], "nextAction": "login"}
    unknown = {**base, "state": "unknown", "loggedOut": False,
               "removed": [], "nextAction": "inspect"}
    failed = {**base, "state": "failed", "loggedOut": False,
              "removed": ["secrets/login.json"], "nextAction": "inspect"}
    for case in (accepted, completed, unknown, failed):
        assert validator.is_valid(case), case["state"]
    # a definite failure is not success and not a completed logout, even when
    # it truthfully reports the artifacts already removed
    assert not validator.is_valid({**failed, "loggedOut": True})
    assert not validator.is_valid({**failed, "nextAction": "login"})
    assert not validator.is_valid({**failed, "nextAction": "poll"})
    # accepted must not claim removal or completion
    assert not validator.is_valid(
        {**accepted, "loggedOut": True, "removed": ["secrets/login.json"],
         "nextAction": "login"}
    )
    assert not validator.is_valid({**accepted, "removed": ["secrets/login.json"]})
    assert not validator.is_valid({**accepted, "nextAction": "login"})
    # unknown must not look like success or a rollback
    assert not validator.is_valid({**unknown, "loggedOut": True})
    assert not validator.is_valid({**unknown, "nextAction": "login"})
    # nothing but the bounded allowlist may appear in removed
    assert not validator.is_valid({**completed, "removed": ["~/.ssh/id_ed25519"]})
    # issuer revocation status is not a caller-controlled field
    assert not validator.is_valid({**completed, "issuerRevocation": "revoked"})
    assert not validator.is_valid({**completed, "refreshToken": "nope"})


def test_logout_removal_labels_are_bounded_and_login_owned() -> None:
    assert LOGOUT_REMOVED_LABELS[0] == "secrets/login.json"
    assert all(
        label == "secrets/login.json" or label.startswith("settings.")
        for label in LOGOUT_REMOVED_LABELS
    )
    assert len(set(LOGOUT_REMOVED_LABELS)) == len(LOGOUT_REMOVED_LABELS)
    assert not any("device" in label or "agent" in label for label in LOGOUT_REMOVED_LABELS)


def test_relogin_result_is_a_login_begun_result_with_intent_login() -> None:
    validator = Draft202012Validator(RELOGIN_RESULT)
    common = {
        "ok": True,
        "operationId": "op-0123456789abcdef",
        "state": "pending",
        "verification": {
            "verificationUri": "https://auth.example.test/device",
            "userCode": "ABCD-EFGH",
            "expiresAt": "2026-11-05T06:00:00Z",
            "intervalSeconds": 5,
        },
        "owner": None,
    }
    assert validator.is_valid({**common, "intent": "login"})
    assert not validator.is_valid({**common, "intent": "register"})


@pytest.mark.parametrize("name", sorted(NAMES))
def test_business_params_never_accept_ownership_fields(
    registry: MethodRegistry, name: str
) -> None:
    binding = registry.get(name)
    assert binding is not None
    properties = set(binding.spec.params_schema["properties"])
    assert not properties & FORBIDDEN_PARAMS
    with pytest.raises(DaemonRequestError) as caught:
        registry.invoke(None, name, {"owner": "user:forged@example.test"})
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT


def test_valid_call_reaches_the_declared_not_ready_error(
    registry: MethodRegistry,
) -> None:
    with pytest.raises(DaemonRequestError) as caught:
        registry.invoke(None, "onboarding.status", {})
    assert caught.value.code == ipc_errors.DAEMON_NOT_READY


def test_isolated_catalog_and_openapi_expose_phase_caller_and_examples() -> None:
    expected = PAIRS
    catalog = export_catalog(ONBOARDING_SPECS)
    entries = {entry["name"]: entry for entry in catalog["methods"]}
    for name, pairs in expected.items():
        projection = entries[name]["phaseCaller"]
        assert projection == {
            "pairs": [
                {"phase": phase, "caller": caller} for phase, caller in pairs
            ],
        }
        assert "phases" not in projection
        assert "callers" not in projection
    document = export_openapi(ONBOARDING_SPECS, example_sets=ONBOARDING_EXAMPLES)
    for name, pairs in expected.items():
        operation = document["paths"][f"/daemon/request/{name}"]["post"]
        assert operation["x-hyprial-phase-caller"] == {
            "pairs": [
                {"phase": phase, "caller": caller} for phase, caller in pairs
            ],
        }
        assert operation["x-hyprial-examples"]


def test_production_registry_and_catalog_register_the_eight_onboarding_methods() -> None:
    """Registration is now required, not forbidden.

    The declarations were reviewable before the common line existed ("The common
    line registers them after the structural packages merge" in specs.py).  That
    line has landed, so every declared onboarding name must be registered and
    projected by the production catalog.
    """

    registered = set(METHOD_REGISTRY.names)
    assert NAMES <= registered
    production = {entry["name"] for entry in daemon_catalog()["methods"]}
    assert NAMES <= production


def test_existing_declarations_without_the_optional_field_are_unchanged() -> None:
    binding = METHOD_REGISTRY.get("adapter.list")
    assert binding is not None
    entry = export_catalog([binding.spec])["methods"][0]
    assert "phaseCaller" not in entry


@pytest.mark.parametrize(
    "pairs",
    [
        (),
        (("bootstrap",),),
        (("restoring", "local-bootstrap"),),
        (("bootstrap", "remote-owner"),),
        (("bootstrap", "local-owner"),),
        (("bootstrap", "local-bootstrap"), ("bootstrap", "local-bootstrap")),
        (("", "local-bootstrap"),),
        (("bootstrap", ""),),
    ],
)
def test_phase_caller_rejects_empty_unknown_old_or_duplicate_pairs(pairs) -> None:
    with pytest.raises(MethodSpecError):
        PhaseCallerSpec(pairs=pairs)
