"""implBc: ``agent.create`` / ``agent.stop`` / ``agent.start`` declarations vs the real handlers.

The three lifecycle names join the exact-name partial ``agent`` migration with
the write profile (never-retry, H-03).  These tests pin the wiring (registry
bindings, family staying open), the equivalence between the legacy paths and
the registry path on a real application, the typed not-ready refusal of
``agent.start``, and HTTP admission of all three.

Round 2 (revBc REJECT @ a598bd911a): AGENT_ALREADY_RUNNING survives with its
real data through registry and IPC; the create binding reuses the single
legacy agent.* exception translation for AgentError, DomainCommandError and
AgentHomeError; the create contract declares real nested entity schemas
(status row, identity snapshots, providerDefault branches), the known
capabilities/harnessArgs params, and rejects malformed nested fields.

Round 3 (revBc round 2 REJECT @ 17c39514cc, Bc-r2-1): the complete
``_ipc_down`` → ``_run_lifecycle_operation`` error set is declared —
SUBMIT_OUTCOME_UNKNOWN keeps its ``operationId`` reconciliation data,
DAEMON_START_FAILED survives an admission refusal, and every fixed code a
non-completed lifecycle result can carry (LIFECYCLE_OPERATION_TIMEOUT,
PORT_CLOSING, PORT_OVERLOADED, LIFECYCLE_DOMAIN_FAILED) passes through the
registry and the IPC router instead of being erased into DAEMON_ERROR.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon import daemon_catalog
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_FAMILIES
from hyprial.daemon.impl.request_catalog.agent import lifecycle as lifecycle_catalog
from hyprial.daemon.impl.request_catalog.agent.examples import REQUEST_EXAMPLES
from hyprial.identity import (
    RUNTIME_HEADLESS,
    AgentAlreadyRunning,
    AgentBinding,
    AgentError,
    AgentHomeError,
)
from hyprial.kernel import (
    DaemonRequestError,
    MethodSpec,
    PortAdmission,
    ipc_errors,
    validate_method_example_sets,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from hyprial.shell.impl.request_http.catalog import _unsupported_reason

OWNER = "lifecycle-owner"
NODE = "lifecycle-node"


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


class _Unused:
    pass


def _application(root: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=root,
        socket_path=root / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        **daemon_dependencies(),
    )
    app._inbox = _Unused()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Unused()  # type: ignore[assignment]
    return app


@pytest.fixture
def app() -> Iterator[DaemonApplication]:
    with tempfile.TemporaryDirectory(prefix="hyprial-implbc-") as directory:
        application = _application(Path(directory))
        try:
            yield application
        finally:
            application._close()
            application._close_home_lease()


def _validate(spec: MethodSpec, result: Any) -> None:
    errors = list(Draft202012Validator(spec.result_schema).iter_errors(result))
    assert not errors, (spec.name, errors, result)


def _without_run_state(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "runState"}


def test_the_registered_lifecycle_names_and_the_family_stays_open() -> None:
    for name in lifecycle_catalog.AGENT_LIFECYCLE_METHODS:
        assert name in METHOD_REGISTRY
    assert lifecycle_catalog.AGENT_LIFECYCLE_METHODS == frozenset(
        {"agent.create", "agent.stop", "agent.start"}
    )
    assert "agent.start" in METHOD_REGISTRY
    assert "agent" not in MIGRATED_FAMILIES
    assert "agent.get" not in METHOD_REGISTRY
    for name in METHOD_REGISTRY.names:
        if name in lifecycle_catalog.AGENT_LIFECYCLE_METHODS:
            binding = METHOD_REGISTRY.get(name)
            assert binding is not None and binding.handler is not None


def test_the_declarations_carry_the_write_profile() -> None:
    for spec in (
        lifecycle_catalog.AGENT_CREATE_SPEC,
        lifecycle_catalog.AGENT_STOP_SPEC,
        lifecycle_catalog.AGENT_START_SPEC,
    ):
        assert spec.mode == "unary"
        assert spec.mutates is True
        assert spec.retry == "never"
        assert spec.confirmation == "none"
        assert "local-operator" in spec.audience
        assert spec.http_exposed is True
        assert spec.http_binding is not None
        assert spec.http_binding.identity.mode == "none"
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "never"
        assert spec.params_schema["type"] == "object"
        assert spec.result_schema["type"] == "object"
        assert spec.result_schema["additionalProperties"] is False
        assert ipc_errors.INVALID_ARGUMENT in {e.code for e in spec.errors}


def test_agent_create_declares_the_existing_surface() -> None:
    spec = lifecycle_catalog.AGENT_CREATE_SPEC
    assert set(spec.params_schema["required"]) == {"name"}
    assert spec.owner == "daemon.agent"
    exists = {e.code: e for e in spec.errors}[ipc_errors.AGENT_EXISTS]
    assert exists.data_schema is not None
    assert set(exists.data_schema["required"]) == {"actor", "agent"}


def test_agent_create_registry_path_matches_the_legacy_handler(
    app: DaemonApplication,
) -> None:
    routed = METHOD_REGISTRY.invoke(app, "agent.create", {"name": "alpha"})
    _validate(lifecycle_catalog.AGENT_CREATE_SPEC, routed)
    assert routed["created"] is True
    assert routed["agent"]["actor"] == "alpha"
    assert "providerDefault" in routed

    legacy = app._handle_agent_create_with_default({"name": "beta"})
    _validate(lifecycle_catalog.AGENT_CREATE_SPEC, legacy)
    assert legacy.keys() == routed.keys()
    assert legacy["created"] == routed["created"]
    assert legacy["providerDefault"] == routed["providerDefault"]

    reused = METHOD_REGISTRY.invoke(
        app, "agent.create", {"name": "alpha", "existing": "reuse"}
    )
    _validate(lifecycle_catalog.AGENT_CREATE_SPEC, reused)
    assert reused["created"] is False
    assert _without_run_state(reused["agent"]) == _without_run_state(routed["agent"])

    with pytest.raises(DaemonRequestError) as taken:
        METHOD_REGISTRY.invoke(app, "agent.create", {"name": "alpha"})
    assert taken.value.code == ipc_errors.AGENT_EXISTS
    assert taken.value.data == {
        "actor": routed["agent"]["uri"],
        "agent": "alpha",
    }
    with pytest.raises(DaemonRequestError) as legacy_taken:
        app._handle_agent_create_with_default({"name": "alpha"})
    assert legacy_taken.value.code == ipc_errors.AGENT_EXISTS


def test_new_agent_without_personal_org_is_unassigned_without_affiliation_intent(
    app: DaemonApplication,
) -> None:
    created = METHOD_REGISTRY.invoke(app, "agent.create", {"name": "affiliated"})
    agent_id = created["agent"]["agentId"]
    assert "orgMembership" not in created
    pending = app.state_dir / "agent-org-membership" / f"{agent_id}.json"
    assert not pending.exists()
    live = AgentBinding(
        actor=created["agent"]["uri"],
        harness="pi",
        runtime=RUNTIME_HEADLESS,
        session_id=None,
        bound_at_ms=1760000000000,
        revision=3,
    )
    app._agent_liveness.live_binding = lambda actor: live  # type: ignore[method-assign]
    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(
            app, "agent.create",
            {"name": "affiliated", "existing": "reuse", "harness": "codex"},
        )
    assert refused.value.code == AgentAlreadyRunning.code


def test_reusing_agent_does_not_contact_org_service(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = METHOD_REGISTRY.invoke(app, "agent.create", {"name": "resuming"})
    resumed = METHOD_REGISTRY.invoke(
        app, "agent.create", {"name": "resuming", "existing": "reuse"}
    )
    assert "orgMembership" not in created
    assert "orgMembership" not in resumed


def test_create_rejects_invalid_target_org_before_persisting_agent(
    app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(app, "agent.create", {"name": "bad-target", "org": "not-an-org"})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app.agents.get("bad-target") is None


def test_create_requires_separate_org_join_even_for_valid_target(
    app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(
            app, "agent.create", {"name": "explicit-target", "org": "group-team"}
        )
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app.agents.get("explicit-target") is None


def test_agent_create_schema_rejects_a_missing_name(
    app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(app, "agent.create", {})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(refused.value) == "name must be a non-empty string"


def test_agent_stop_wraps_down_for_exactly_one_agent(
    app: DaemonApplication,
) -> None:
    app.agents.create("alpha")
    legacy = app._ipc_down({"target": "alpha", "all": False, "agentsOnly": True})
    routed = METHOD_REGISTRY.invoke(app, "agent.stop", {"name": "alpha"})
    _validate(lifecycle_catalog.AGENT_STOP_SPEC, routed)
    assert routed == legacy == {"ok": True, "removed": []}

    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(app, "agent.stop", {})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(refused.value) == "name must be a non-empty string"

    with pytest.raises(DaemonRequestError) as refused_type:
        METHOD_REGISTRY.invoke(app, "agent.stop", {"name": 5})
    assert refused_type.value.code == ipc_errors.INVALID_ARGUMENT


def test_agent_stop_deactivates_one_desired_harness(
    app: DaemonApplication,
) -> None:
    from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
        LifecycleResult,
        LifecycleState,
    )

    class _SettlingManager:
        crashed = False

        def submit(self, operation: Any) -> Any:
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float) -> Any:
            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.COMPLETED,
                completed_effects=(),
                compensated_effects=(),
                error=None,
                error_code=None,
            )

        def drain(self, _timeout: float) -> bool:
            return True

    app.agents.create("alpha")
    from hyprial.kernel import HarnessLaunchSpec

    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )
    app._lifecycle_manager = _SettlingManager()
    routed = METHOD_REGISTRY.invoke(app, "agent.stop", {"name": "alpha"})
    _validate(lifecycle_catalog.AGENT_STOP_SPEC, routed)
    assert routed["removed"] == ["claude:alpha"]


def test_agent_start_is_registered_with_its_real_handler(
    app: DaemonApplication,
) -> None:
    binding = METHOD_REGISTRY.get("agent.start")
    assert binding is not None and binding.handler is not None
    document = daemon_catalog()
    names = {entry["name"] for entry in document["methods"]}
    assert "agent.start" in names
    assert "agent.list" in names
    from hyprial.identity import AgentAlreadyRunning

    assert {e.code for e in binding.spec.errors} >= {AgentAlreadyRunning.code}
    assert ipc_errors.DAEMON_NOT_READY not in {e.code for e in binding.spec.errors}


def test_the_contract_examples_validate_against_the_declarations() -> None:
    sets = {item.method for item in REQUEST_EXAMPLES}
    assert sets == {
        "agent.list",
        "agent.secret.list",
        "agent.resolve",
        "agent.create",
        "agent.destroy.preview",
        "agent.destroy",
        "agent.stop",
        "agent.start",
        "agent.home-census",
        "routine.list",
        "routine.status",
        "workflow.list",
        "agent.provider.default",
        "agent.provider.auth.list",
        "agent.provider.login",
        "agent.provider.logout",
        "agent.rename",
    }
    validated = validate_method_example_sets(METHOD_REGISTRY.specs, REQUEST_EXAMPLES)
    assert {item.method for item in validated} == sets


def test_the_catalog_lists_the_registered_lifecycle_methods() -> None:
    document = daemon_catalog()
    names = {entry["name"] for entry in document["methods"]}
    assert lifecycle_catalog.AGENT_LIFECYCLE_METHODS <= names


def test_agent_start_declares_its_real_error_set() -> None:
    spec = lifecycle_catalog.AGENT_START_SPEC
    declared = {e.code: e for e in spec.errors}
    assert ipc_errors.DAEMON_NOT_READY not in declared
    assert {ipc_errors.USE_ADAPTER_COMMAND, ipc_errors.TRANSFER_STARTS_HELD} <= set(declared)
    unknown = declared[ipc_errors.SUBMIT_OUTCOME_UNKNOWN]
    assert unknown.data_schema is not None
    assert set(unknown.data_schema["required"]) == {"operationId"}
    assert "lastError" in unknown.data_schema["properties"]
    binding = spec.http_binding
    assert binding is not None and binding.readiness is None


def _admission_entry(name: str, *, state: str = "available") -> dict[str, Any]:
    entry = next(
        item for item in daemon_catalog()["methods"] if item["name"] == name
    )
    item = dict(entry)
    item["declared"] = True
    item["state"] = state
    item["callable"] = state == "available"
    item["reason"] = None if state == "available" else "SERVICE_NOT_READY"
    return item


@pytest.mark.parametrize(
    "name", sorted(lifecycle_catalog.AGENT_LIFECYCLE_METHODS)
)
def test_http_admission_admits_the_registered_write_methods(name: str) -> None:
    entry = _admission_entry(name)
    assert _unsupported_reason(entry) is None


def test_http_admission_refuses_a_mutating_safe_retry_profile() -> None:
    entry = _admission_entry("agent.stop")
    entry["retry"] = "safe"
    assert _unsupported_reason(entry) == "HTTP_CALL_PROFILE_UNSUPPORTED"


# --- round 2 ---------------------------------------------------------------


def test_agent_create_declares_already_running_with_its_real_data() -> None:
    spec = lifecycle_catalog.AGENT_CREATE_SPEC
    declared = {error.code: error for error in spec.errors}
    already = declared[AgentAlreadyRunning.code]
    schema = already.data_schema
    assert schema is not None
    assert set(schema["required"]) == {
        "actor",
        "existing",
        "requestedHarness",
        "requestedRuntime",
    }
    existing = schema["properties"]["existing"]
    assert existing["additionalProperties"] is False
    assert set(existing["required"]) == {
        "actor",
        "harness",
        "runtime",
        "sessionId",
        "boundAtMs",
    }


def test_agent_create_survives_already_running_through_registry_and_ipc(
    app: DaemonApplication,
) -> None:
    created = METHOD_REGISTRY.invoke(app, "agent.create", {"name": "alpha"})
    uri = created["agent"]["uri"]
    live = AgentBinding(
        actor=uri,
        harness="pi",
        runtime=RUNTIME_HEADLESS,
        session_id=None,
        bound_at_ms=1760000000000,
        revision=3,
    )
    app._agent_liveness.live_binding = lambda actor: live  # type: ignore[method-assign]
    params = {"name": "alpha", "existing": "reuse", "harness": "claude"}
    with pytest.raises(DaemonRequestError) as routed:
        METHOD_REGISTRY.invoke(app, "agent.create", params)
    assert routed.value.code == AgentAlreadyRunning.code
    assert routed.value.data == {
        "actor": uri,
        "existing": {
            "actor": uri,
            "harness": "pi",
            "runtime": RUNTIME_HEADLESS,
            "sessionId": None,
            "boundAtMs": 1760000000000,
        },
        "requestedHarness": "claude",
        "requestedRuntime": RUNTIME_HEADLESS,
    }
    with pytest.raises(DaemonRequestError) as ipc:
        app.handle("agent.create", params)
    assert ipc.value.code == AgentAlreadyRunning.code
    assert ipc.value.data == routed.value.data


@pytest.mark.parametrize(
    "raised,expected_code",
    [
        (AgentError("registry unavailable"), AgentError.code),
        (AgentHomeError("home-provision-failed", "agent:x:y:alpha", "provision"), AgentError.code),
    ],
)
def test_agent_create_preserves_the_single_legacy_translation(
    app: DaemonApplication,
    raised: Exception,
    expected_code: str,
) -> None:
    def refusing(_params: Any) -> Any:
        raise raised

    app._handle_agent_create_with_default = refusing  # type: ignore[method-assign]
    with pytest.raises(DaemonRequestError) as routed:
        METHOD_REGISTRY.invoke(app, "agent.create", {"name": "alpha"})
    assert routed.value.code == expected_code
    assert routed.value.data is None
    with pytest.raises(DaemonRequestError) as ipc:
        app.handle("agent.create", {"name": "alpha"})
    assert ipc.value.code == expected_code
    assert ipc.value.data is None


def test_agent_create_translates_a_real_agent_error_through_the_registry(
    app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(
            app,
            "agent.create",
            {"name": "gamma", "capabilities": {"x": float("nan")}},
        )
    assert refused.value.code == AgentError.code


def test_agent_create_declares_real_nested_entity_schemas() -> None:
    result = lifecycle_catalog.AGENT_CREATE_SPEC.result_schema
    agent_row = result["properties"]["agent"]
    assert agent_row["additionalProperties"] is False
    for field in (
        "uri",
        "actor",
        "owner",
        "machine",
        "config",
        "capabilities",
        "harnessArgs",
        "status",
        "harness",
        "runtime",
        "sessionId",
        "heartbeatAgeSeconds",
    ):
        assert field in agent_row["properties"], field
    assert "activityHints" not in agent_row["properties"]
    snapshot = result["properties"]["resumeSnapshot"]
    assert snapshot["additionalProperties"] is False
    assert set(snapshot["required"]) >= {
        "uri",
        "actor",
        "entityToken",
        "capabilities",
        "harnessArgs",
        "createdAtMs",
    }
    previous = result["properties"]["resumePreviousSnapshot"]
    assert {"type": "null"} in previous["oneOf"]
    snapshot_branch = next(
        item for item in previous["oneOf"] if item.get("type") == "object"
    )
    assert snapshot_branch["required"] == snapshot["required"]


def test_agent_create_declares_the_real_provider_default_branches() -> None:
    default = lifecycle_catalog.AGENT_CREATE_SPEC.result_schema["properties"][
        "providerDefault"
    ]
    kinds = sorted(
        branch["type"] if isinstance(branch["type"], str) else "object"
        for branch in default["oneOf"]
    )
    assert kinds == ["null", "object", "string"]
    captured = next(
        branch for branch in default["oneOf"] if branch.get("type") == "object"
    )
    assert captured["additionalProperties"] is False
    assert set(captured["required"]) == {
        "provider",
        "credentialSource",
        "defaultCredential",
    }
    credential = captured["properties"]["defaultCredential"]
    assert set(credential["required"]) == {
        "ownerActor",
        "ownerEntityToken",
        "entryId",
        "fieldName",
        "kind",
    }


def test_agent_create_declares_the_known_structured_params() -> None:
    params = lifecycle_catalog.AGENT_CREATE_SPEC.params_schema["properties"]
    assert tuple(params["capabilities"]["type"]) == ("object", "null")
    assert params["capabilities"]["additionalProperties"] is not False
    assert tuple(params["harnessArgs"]["type"]) == ("object", "null")
    assert params["harnessArgs"]["additionalProperties"] == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_agent_create_params_reject_malformed_nested_fields(
    app: DaemonApplication,
) -> None:
    with pytest.raises(DaemonRequestError) as harness_args:
        METHOD_REGISTRY.invoke(
            app, "agent.create", {"name": "x", "harnessArgs": {"claude": "--flag"}}
        )
    assert harness_args.value.code == ipc_errors.INVALID_ARGUMENT
    with pytest.raises(DaemonRequestError) as capabilities:
        METHOD_REGISTRY.invoke(
            app, "agent.create", {"name": "x", "capabilities": "not-an-object"}
        )
    assert capabilities.value.code == ipc_errors.INVALID_ARGUMENT


def test_agent_create_result_schema_rejects_malformed_nested_fields(
    app: DaemonApplication,
) -> None:
    valid = METHOD_REGISTRY.invoke(app, "agent.create", {"name": "alpha"})
    _validate(lifecycle_catalog.AGENT_CREATE_SPEC, valid)
    validator = Draft202012Validator(
        lifecycle_catalog.AGENT_CREATE_SPEC.result_schema
    )

    def corrupt(mutate: Any) -> bool:
        broken = deepcopy(valid)
        mutate(broken)
        return list(validator.iter_errors(broken)) != []

    assert corrupt(lambda result: result.__setitem__("agent", {"uri": "agent:x:y:z"}))
    assert corrupt(
        lambda result: result["agent"].__setitem__("runState", "blocked")
    )
    assert corrupt(
        lambda result: result.__setitem__(
            "providerDefault", {"provider": "claude"}
        )
    )
    assert corrupt(
        lambda result: result.__setitem__("resumeSnapshot", {"actor": "alpha"})
    )


def test_agent_create_resume_snapshots_match_the_declared_identity_shape(
    app: DaemonApplication,
) -> None:
    METHOD_REGISTRY.invoke(app, "agent.create", {"name": "alpha"})
    routed = METHOD_REGISTRY.invoke(
        app,
        "agent.create",
        {
            "name": "alpha",
            "existing": "reuse",
            "harness": "claude",
            "includeResumeSnapshot": True,
        },
    )
    _validate(lifecycle_catalog.AGENT_CREATE_SPEC, routed)
    assert routed["resumePreviousSnapshot"]["actor"] == "alpha"
    assert routed["resumeSnapshot"]["entityToken"] == (
        routed["resumePreviousSnapshot"]["entityToken"]
    )


# --- round 3 (Bc-r2-1: the real stop lifecycle error set) --------------------


_STOP_LIFECYCLE_CODES = (
    "LIFECYCLE_OPERATION_TIMEOUT",
    "PORT_CLOSING",
    "PORT_OVERLOADED",
    "LIFECYCLE_DOMAIN_FAILED",
)


def _desired_harness(app: DaemonApplication) -> None:
    from hyprial.kernel import HarnessLaunchSpec

    app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )


def test_agent_stop_declares_the_real_lifecycle_error_set() -> None:
    spec = lifecycle_catalog.AGENT_STOP_SPEC
    declared = {error.code: error for error in spec.errors}
    assert {
        ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE,
        ipc_errors.LIFECYCLE_OPERATION_UNSETTLED,
        ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
        ipc_errors.DAEMON_START_FAILED,
        *_STOP_LIFECYCLE_CODES,
    } <= set(declared)
    unknown = declared[ipc_errors.SUBMIT_OUTCOME_UNKNOWN]
    assert unknown.data_schema is not None
    assert set(unknown.data_schema["required"]) == {"operationId"}
    assert unknown.data_schema["additionalProperties"] is False
    for code in (ipc_errors.DAEMON_START_FAILED, *_STOP_LIFECYCLE_CODES):
        assert declared[code].data_schema is None, code


def test_agent_stop_survives_reservation_outcome_unknown(
    app: DaemonApplication,
) -> None:
    from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import (
        LifecycleReservationOutcomeUnknown,
    )

    class _UnknownManager:
        crashed = False

        def submit(self, operation: Any) -> Any:
            raise LifecycleReservationOutcomeUnknown(operation.operation_id)

        def wait(self, _operation_id: str, _timeout: float) -> Any:
            raise AssertionError("never waited; the reservation never admitted")

        def drain(self, _timeout: float) -> bool:
            return True

    _desired_harness(app)
    app._lifecycle_manager = _UnknownManager()
    with pytest.raises(DaemonRequestError) as legacy:
        app._ipc_down({"target": "alpha", "all": False, "agentsOnly": True})
    assert legacy.value.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert list(legacy.value.data) == ["operationId"]
    operation_id = legacy.value.data["operationId"]
    assert operation_id.startswith("lifecycle-down:")

    app._lifecycle_manager = _UnknownManager()
    with pytest.raises(DaemonRequestError) as routed:
        METHOD_REGISTRY.invoke(app, "agent.stop", {"name": "alpha"})
    assert routed.value.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert set(routed.value.data) == {"operationId"}
    assert routed.value.data["operationId"].startswith("lifecycle-down:")

    app._lifecycle_manager = _UnknownManager()
    with pytest.raises(DaemonRequestError) as ipc:
        app.handle("agent.stop", {"name": "alpha"})
    assert ipc.value.code == routed.value.code
    assert set(ipc.value.data) == {"operationId"}


@pytest.mark.parametrize(
    ("crashed", "expected_code"),
    [
        (False, "DAEMON_START_FAILED"),
        (True, ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE),
    ],
)
def test_agent_stop_survives_an_admission_refusal(
    app: DaemonApplication,
    crashed: bool,
    expected_code: str,
) -> None:
    class _RefusingManager:
        def submit(self, _operation: Any) -> Any:
            return PortAdmission.CLOSING

        def wait(self, _operation_id: str, _timeout: float) -> Any:
            raise AssertionError("never waited; admission was refused")

        def drain(self, _timeout: float) -> bool:
            return True

    _desired_harness(app)
    manager = _RefusingManager()
    manager.crashed = crashed
    app._lifecycle_manager = manager
    with pytest.raises(DaemonRequestError) as routed:
        METHOD_REGISTRY.invoke(app, "agent.stop", {"name": "alpha"})
    assert routed.value.code == expected_code
    assert routed.value.data is None
    manager2 = _RefusingManager()
    manager2.crashed = crashed
    app._lifecycle_manager = manager2
    with pytest.raises(DaemonRequestError) as ipc:
        app.handle("agent.stop", {"name": "alpha"})
    assert ipc.value.code == expected_code
    assert ipc.value.data is None


@pytest.mark.parametrize("error_code", _STOP_LIFECYCLE_CODES)
def test_agent_stop_survives_a_terminal_lifecycle_failure(
    app: DaemonApplication,
    error_code: str,
) -> None:
    from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
        LifecycleResult,
        LifecycleState,
    )

    state = (
        LifecycleState.COMPENSATED
        if error_code == "LIFECYCLE_OPERATION_TIMEOUT"
        else LifecycleState.FAILED
    )

    class _FailingManager:
        crashed = False

        def submit(self, _operation: Any) -> Any:
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float) -> Any:
            return LifecycleResult(
                operation_id=operation_id,
                state=state,
                completed_effects=(),
                compensated_effects=(),
                error=f"the stop ended in {state.value}: {error_code}",
                error_code=error_code,
            )

        def drain(self, _timeout: float) -> bool:
            return True

    _desired_harness(app)
    app._lifecycle_manager = _FailingManager()
    with pytest.raises(DaemonRequestError) as routed:
        METHOD_REGISTRY.invoke(app, "agent.stop", {"name": "alpha"})
    assert routed.value.code == error_code
    assert routed.value.data is None
    app._lifecycle_manager = _FailingManager()
    with pytest.raises(DaemonRequestError) as ipc:
        app.handle("agent.stop", {"name": "alpha"})
    assert ipc.value.code == error_code
    assert ipc.value.data is None


def test_http_exposure_is_explicit() -> None:
    exposed = {
        spec.name for spec in METHOD_REGISTRY.specs if spec.http_exposed
    }
    assert exposed == {
        "adapter.identities.list",
        "adapter.identities.sync",
        "adapter.register",
        "adapter.authorization.status",
        "adapter.authorization.begin",
        "adapter.authorization.operation",
        "adapter.authorization.cancel",
        "adapter.onboard.begin",
        "adapter.onboard.operation",
        "adapter.onboard.cancel",
        "adapter.routes.list",
        "adapter.routes.candidates",
        "adapter.routes.add",
        "adapter.routes.remove",
        "adapter.list",
        "adapter.status",
        "management.adapter.remove",
        "management.adapter.remove.preview",
        "adapter.start",
        "adapter.stop",
        "adapter.reload",
        "agent.cell.get",
        "agent.cell.update",
        "agent.home-census",
        "agent.list",
        "agent.provider.auth.list",
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
        "agent.provider.allow",
        "agent.provider.revoke",
        "agent.provider.status",
        "agent.secret.list",
        "agent.secret.grant",
        "agent.secret.provider-write",
        "agent.secret.provider-list",
        "agent.secret.provider-configure",
        "agent.secret.provider-test",
        "agent.secret.revoke",
        "agent.create",
        "agent.destroy.preview",
        "agent.destroy",
        "agent.stop",
        "agent.start",
        "agent.rename",
        "auth.login.begin",
        "auth.logout",
        "auth.operation.cancel",
        "auth.operation.get",
        "auth.relogin",
        "autoupdate.status",
        "daemon.status",
        "routine.list",
        "routine.status",
        "workflow.list",
        "hosts",
        "identity.users.list",
        "log.page",
        "identity.users.add",
        "message.send.lookup",
        "message.send",
        "message.send.key_lookup",
        "message.history",
        "message.query",
        "progress.list",
        "targets",
        "onboarding.device.ensure",
        "onboarding.firstRun.begin",
        "onboarding.firstRun.bind",
        "onboarding.firstRun.cancel",
        "onboarding.firstRun.confirm",
        "onboarding.firstRun.skip",
        "onboarding.initialize",
        "onboarding.status",
        "org.create",
        "org.delete",
        "org.execute",
        "org.fetch",
        "org.invite",
        "org.join",
        "org.leave",
        "org.list",
        "org.invites.list",
        "org.network",
        "org.publish",
        "org.remove",
        "org.repair",
        "orgfs.checkout",
        "orgfs.create",
        "orgfs.export",
        "orgfs.import",
        "orgfs.invite",
        "orgfs.join",
        "orgfs.ls",
        "orgfs.members",
        "orgfs.mkdir",
        "orgfs.move",
        "orgfs.read",
        "orgfs.remove",
        "orgfs.remove_member",
        "orgfs.resolve",
        "orgfs.restore",
        "orgfs.space_purge",
        "orgfs.spaces",
        "orgfs.stat",
        "orgfs.status",
        "orgfs.trash",
        "orgfs.watch",
        "orgfs.write",
        "top.snapshot",
        "upgrade.steps.status",
        "user.provider.list",
        "user.provider.login",
        "user.provider.logout",
        "user.provider.rotate",
        # cell-model phase B first slice: HTTP-exposed conversation reads.
        "conversations.history",
        "conversations.list",
        "conversations.create",
        "conversations.get",
        "conversations.members.add",
        "conversations.members.remove",
        "conversations.post",
    }
    # Negative coverage: internal/owner-gated methods stay out of HTTP.
    assert {
        "daemon.catalog",
        "org.acl.grant",
        "org.acl.ls",
        "org.acl.revoke",
        "session.turn.refresh",
    }.isdisjoint(exposed)
