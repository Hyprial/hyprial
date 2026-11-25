"""Harness-argument request declarations, authority, and behavior."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog import agent as agent_catalog
from hyprial.kernel import DaemonRequestError, export_openapi, ipc_errors

from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
    application,
)

NAMES = (
    "agent.harness-args.clear",
    "agent.harness-args.get",
    "agent.harness-args.set",
    "agent.harness-args.validate",
)


@pytest.fixture
def app(tmp_path) -> Iterator[DaemonApplication]:  # type: ignore[no-untyped-def]
    yield application(tmp_path)


def test_four_methods_are_registered_with_the_required_profiles() -> None:
    assert all(name in METHOD_REGISTRY for name in NAMES)
    assert set(NAMES) <= set(agent_catalog.PARTIAL_MIGRATED_METHODS)

    for name in NAMES:
        spec = METHOD_REGISTRY.get(name).spec  # type: ignore[union-attr]
        assert spec.http_exposed is False
        assert spec.confirmation == "none"
        assert spec.mode == "unary"
        assert spec.params_schema["additionalProperties"] is False
        assert spec.result_schema["additionalProperties"] is False
        if name.endswith((".get", ".validate")):
            assert spec.mutates is False
            assert spec.retry == "safe"
            assert spec.audience == ("local-operator", "bound-agent")
            assert spec.idempotency.mode == "safe"
        else:
            assert spec.mutates is True
            assert spec.retry == "never"
            assert spec.audience == ("local-operator",)
            assert spec.idempotency.mode == "never"

    document = export_openapi(
        tuple(METHOD_REGISTRY.get(name).spec for name in NAMES)  # type: ignore[union-attr]
    )
    assert document["paths"] == {}


def test_write_errors_and_dangerous_data_are_declared() -> None:
    set_spec = METHOD_REGISTRY.get("agent.harness-args.set").spec  # type: ignore[union-attr]
    clear_spec = METHOD_REGISTRY.get("agent.harness-args.clear").spec  # type: ignore[union-attr]
    set_errors = {error.code: error for error in set_spec.errors}
    clear_errors = {error.code: error for error in clear_spec.errors}

    assert {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.CALLER_NOT_AUTHORIZED,
        ipc_errors.AGENT_NOT_FOUND,
        ipc_errors.HARNESS_ARGS_CONFLICT,
        ipc_errors.HARNESS_ARGS_INVALID,
        ipc_errors.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED,
        ipc_errors.USE_ADAPTER_COMMAND,
        ipc_errors.UNSUPPORTED_CAPABILITY,
        "AGENT_ERROR",
        ipc_errors.DAEMON_ERROR,
        ipc_errors.USER_AGENT_IMMUTABLE,
    } == set(set_errors)
    assert set(clear_errors) == set(set_errors) - {
        ipc_errors.HARNESS_ARGS_INVALID,
        ipc_errors.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED,
        ipc_errors.USE_ADAPTER_COMMAND,
        ipc_errors.UNSUPPORTED_CAPABILITY,
    }
    assert set_errors[
        ipc_errors.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED
    ].data_schema is not None
    assert set_errors[ipc_errors.HARNESS_ARGS_CONFLICT].data_schema is not None


def test_validate_reports_semantic_issues_and_always_lists_dangerous(
    app: DaemonApplication,
) -> None:
    safe = app.handle(
        "agent.harness-args.validate",
        {"harness": "claude", "args": ["--model", "sonnet"]},
    )
    dangerous = app.handle(
        "agent.harness-args.validate",
        {"harness": "claude", "args": ["--settings", "{}"]},
    )
    invalid = app.handle(
        "agent.harness-args.validate",
        {"harness": "claude", "args": ["--json"]},
    )

    assert safe == {
        "ok": True, "harness": "claude", "args": ["--model", "sonnet"],
        "valid": True, "issues": [], "warnings": [], "dangerous": [],
    }
    assert dangerous["valid"] is True
    assert dangerous["dangerous"][0]["category"] == "code-execution"
    assert invalid["valid"] is False
    assert invalid["issues"]
    assert invalid["dangerous"] == []


def test_get_returns_sorted_previews_and_never_fails_on_historical_args(
    app: DaemonApplication,
) -> None:
    app.agents.create(
        "foo",
        harness_args={
            "legacy": ("--token=private",),
            "claude": ("--settings", "{}"),
        },
    )

    result = app.handle("agent.harness-args.get", {"name": "foo"})
    spec = METHOD_REGISTRY.get("agent.harness-args.get").spec  # type: ignore[union-attr]
    assert not list(Draft202012Validator(spec.result_schema).iter_errors(result))
    assert list(result["harnessArgs"]) == ["claude", "legacy"]
    assert [row["harness"] for row in result["harnesses"]] == [
        "claude",
        "legacy",
    ]
    assert result["harnesses"][0]["dangerous"][0]["category"] == "code-execution"
    assert result["harnesses"][1]["valid"] is False


def test_set_requires_dangerous_confirmation_then_writes_through_command(
    app: DaemonApplication,
) -> None:
    app.agents.create("foo")
    params = {
        "name": "foo",
        "harness": "claude",
        "args": ["--settings", "{}"],
        "expectedHarnessArgs": {},
    }

    with pytest.raises(DaemonRequestError) as refused:
        app.handle("agent.harness-args.set", params)
    assert refused.value.code == (
        ipc_errors.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED
    )
    assert refused.value.data["harness"] == "claude"
    assert refused.value.data["dangerous"][0]["category"] == "code-execution"

    result = app.handle(
        "agent.harness-args.set", {**params, "confirmDangerous": True}
    )
    assert result["ok"] is True and result["changed"] is True
    assert result["auditId"] == "agent_harness_args_audit:1"
    assert result["harnessArgs"] == {"claude": ["--settings", "{}"]}


def test_confirm_dangerous_does_not_bypass_semantic_validation(
    app: DaemonApplication,
) -> None:
    app.agents.create("foo")
    with pytest.raises(DaemonRequestError) as invalid:
        app.handle(
            "agent.harness-args.set",
            {
                "name": "foo", "harness": "claude", "args": ["--json"],
                "expectedHarnessArgs": {}, "confirmDangerous": True,
            },
        )
    assert invalid.value.code == ipc_errors.HARNESS_ARGS_INVALID
    assert invalid.value.data["issues"]


def test_conflict_error_tells_the_client_to_reread(
    app: DaemonApplication,
) -> None:
    app.agents.create("foo", harness_args={"pi": ("--offline",)})

    with pytest.raises(DaemonRequestError) as conflict:
        app.handle(
            "agent.harness-args.set",
            {
                "name": "foo", "harness": "claude",
                "args": ["--model", "sonnet"], "expectedHarnessArgs": {},
            },
        )

    assert conflict.value.code == ipc_errors.HARNESS_ARGS_CONFLICT
    assert conflict.value.data == {
        "action": "re-read",
        "method": "agent.harness-args.get",
    }


def test_clear_all_requires_confirmation_and_clears_historical_groups(
    app: DaemonApplication,
) -> None:
    historical = {"legacy": ("--unknown",), "pi": ("--offline",)}
    app.agents.create("foo", harness_args=historical)
    params = {
        "name": "foo", "all": True, "expectedHarnessArgs": {
            key: list(value) for key, value in historical.items()
        },
    }

    with pytest.raises(DaemonRequestError) as refused:
        app.handle("agent.harness-args.clear", params)
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert "confirmAll" in str(refused.value)

    cleared = app.handle(
        "agent.harness-args.clear", {**params, "confirmAll": True}
    )
    assert cleared["changed"] is True
    assert cleared["harnessArgs"] == {}
    assert cleared["auditId"] == "agent_harness_args_audit:1"


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "agent.harness-args.set",
            {
                "name": "foo", "harness": "pi", "args": ["--offline"],
                "expectedHarnessArgs": {},
            },
        ),
        (
            "agent.harness-args.clear",
            {
                "name": "foo", "harness": "pi",
                "expectedHarnessArgs": {},
            },
        ),
    ],
)
def test_bound_agent_cannot_call_write_methods(
    app: DaemonApplication, method: str, params: dict[str, object],
) -> None:
    app.agents.create("foo")
    with pytest.raises(DaemonRequestError) as refused:
        app.handle(method, params, _trusted_message_origin="worker")
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


@pytest.mark.parametrize(
    ("harness", "code"),
    [
        ("lark", ipc_errors.USE_ADAPTER_COMMAND),
        ("jev", "UNSUPPORTED_CAPABILITY"),
        ("user-proxy", ipc_errors.INVALID_ARGUMENT),
        ("typo", ipc_errors.INVALID_ARGUMENT),
    ],
)
def test_harness_support_errors_are_actionable(
    app: DaemonApplication, harness: str, code: str,
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "agent.harness-args.validate",
            {"harness": harness, "args": []},
        )
    assert refused.value.code == code
    assert str(refused.value)


@pytest.mark.parametrize(
    "args",
    [
        ["bad\x00arg"],
        ["--verbose"] * 257,
        ["x" * 65_537],
    ],
)
def test_structure_limits_fail_before_policy(
    app: DaemonApplication, args: list[str],
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "agent.harness-args.validate",
            {"harness": "claude", "args": args},
        )
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_old_client_missing_expected_map_gets_an_update_instruction(
    app: DaemonApplication,
) -> None:
    app.agents.create("foo")
    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "agent.harness-args.set",
            {"name": "foo", "harness": "pi", "args": ["--offline"]},
        )
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert "agent.harness-args.get" in str(refused.value)


@pytest.mark.parametrize(
    "target",
    [{}, {"harness": "pi", "all": True, "confirmAll": True}],
)
def test_clear_requires_exactly_one_target(
    app: DaemonApplication, target: dict[str, object],
) -> None:
    app.agents.create("foo")
    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "agent.harness-args.clear",
            {"name": "foo", "expectedHarnessArgs": {}, **target},
        )
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
