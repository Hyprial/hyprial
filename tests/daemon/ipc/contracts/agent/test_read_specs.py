"""M-01: ``agent.list`` and ``agent.secret.list`` declarations vs the real handlers.

The two read methods moved into the MethodSpec catalog without changing what the
daemon answers.  These tests pin the wiring (exact-name partial registration, the
``agent`` family staying open to legacy routing) and the equivalence between the
legacy ``_handle_agent`` path and the registry path on a real application.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_FAMILIES
from hyprial.daemon.impl.request_catalog import agent as agent_catalog
from hyprial.identity import SecretSource
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

OWNER = "spec-owner"
NODE = "spec-node"


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
    with tempfile.TemporaryDirectory(prefix="hyprial-m01-") as directory:
        application = _application(Path(directory))
        try:
            yield application
        finally:
            application._close()
            application._close_home_lease()


def _validate(spec: Any, result: Any) -> None:
    errors = list(Draft202012Validator(spec.result_schema).iter_errors(result))
    assert not errors, (spec.name, errors, result)


def _without_run_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "runState"} for row in rows]


def _routed(app: DaemonApplication, method: str, params: dict[str, Any]) -> Any:
    return METHOD_REGISTRY.invoke(app, method, params)


def test_the_two_reads_are_declared_by_exact_name_and_the_family_stays_open() -> None:
    assert "agent.list" in METHOD_REGISTRY
    assert "agent.secret.list" in METHOD_REGISTRY
    assert agent_catalog.PARTIAL_MIGRATED_METHODS == frozenset(
        {"agent.list", "agent.secret.list"}
    )
    assert "agent" not in MIGRATED_FAMILIES
    assert "link" in MIGRATED_FAMILIES
    assert {
        name for name in METHOD_REGISTRY.names if name.startswith("agent.")
    } == set(agent_catalog.PARTIAL_MIGRATED_METHODS) | {
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
    }
    assert METHOD_REGISTRY.get("agent.get") is None
    assert METHOD_REGISTRY.get("agent.secret.grant") is None


def test_the_declarations_are_read_only_and_http_projectable() -> None:
    for name, owner in (
        ("agent.list", "daemon.agent"),
        ("agent.secret.list", "daemon.agent.secret"),
    ):
        spec = METHOD_REGISTRY.get(name).spec  # type: ignore[union-attr]
        assert spec.owner == owner
        assert spec.mode == "unary"
        assert spec.mutates is False
        assert spec.retry == "safe"
        assert spec.confirmation == "none"
        assert spec.http_exposed is True
        assert spec.audience == ("local-operator", "bound-agent")
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "safe"
        assert spec.params_schema["type"] == "object"
        assert spec.result_schema["type"] == "object"
        assert spec.result_schema["additionalProperties"] is False
        assert set(spec.params_schema["additionalProperties"]["type"]) == {
            "string",
            "number",
            "boolean",
            "null",
            "array",
            "object",
        }
        assert {error.code for error in spec.errors} <= {
            ipc_errors.INVALID_ARGUMENT,
            ipc_errors.AGENT_NOT_FOUND,
            "AGENT_ERROR",
        }
        assert ipc_errors.INVALID_ARGUMENT in {error.code for error in spec.errors}


def test_agent_secret_list_targets_one_agent_and_never_a_caller_identity() -> None:
    spec = agent_catalog.AGENT_SECRET_LIST_SPEC
    assert set(spec.params_schema["properties"]) == {"actor"}
    assert spec.params_schema.get("required", []) == []
    assert set(agent_catalog.AGENT_LIST_SPEC.params_schema["properties"]) == {
        "inactiveSince",
        "excludeWf",
    }


def test_the_real_legacy_and_registry_results_agree(app: DaemonApplication) -> None:
    app.agents.create("alpha")
    app.agents.create(
        "beta", provider="deepseek", model="deepseek-flash", preferred_harness="pi"
    )
    app.agents.create("wf-worker")
    app.agents.grant_secret(
        "alpha",
        grant_id="routes",
        source=SecretSource.AGENT_PRIVATE,
        entry_id="cloudflare-routes",
        field_name="CLOUDFLARE_API_TOKEN",
        environment_names=("CLOUDFLARE_API_TOKEN",),
        revision=1,
    )

    for params in ({}, {"excludeWf": True}, {"inactiveSince": "1h"}):
        legacy = app._handle_agent("agent.list", dict(params))
        routed = _routed(app, "agent.list", dict(params))
        _validate(agent_catalog.AGENT_LIST_SPEC, legacy)
        _validate(agent_catalog.AGENT_LIST_SPEC, routed)
        assert legacy.keys() == routed.keys()
        assert legacy["agentsWithoutRoutine"] == routed["agentsWithoutRoutine"]
        assert _without_run_state(legacy["agents"]) == _without_run_state(routed["agents"])

    for params in ({}, {"actor": None}, {"actor": "alpha"}, {"actor": "beta"}):
        legacy = app._handle_agent("agent.secret.list", dict(params))
        routed = _routed(app, "agent.secret.list", dict(params))
        _validate(agent_catalog.AGENT_SECRET_LIST_SPEC, legacy)
        assert routed == legacy

    listed = _routed(app, "agent.secret.list", {"actor": "alpha"})
    assert [grant["grantId"] for grant in listed["grants"]] == ["routes"]
    assert listed["grants"][0]["ownerActor"] is None
    assert _routed(app, "agent.secret.list", {"actor": "beta"})["grants"] == []
    assert len(_routed(app, "agent.secret.list", {})["grants"]) == 1


def test_the_legacy_error_semantics_survive_the_registry_path(app: DaemonApplication) -> None:
    app.agents.create("alpha")

    with pytest.raises(DaemonRequestError) as bad_duration:
        _routed(app, "agent.list", {"inactiveSince": "not-a-duration"})
    assert bad_duration.value.code == ipc_errors.INVALID_ARGUMENT
    with pytest.raises(DaemonRequestError) as legacy_duration:
        app._handle_agent("agent.list", {"inactiveSince": "not-a-duration"})
    assert str(bad_duration.value) == str(legacy_duration.value)

    for params in ({"actor": ""}, {"actor": 5}):
        with pytest.raises(DaemonRequestError) as bad_actor:
            _routed(app, "agent.secret.list", params)
        assert bad_actor.value.code == ipc_errors.INVALID_ARGUMENT
        assert str(bad_actor.value) == "actor must be a non-empty string"

    with pytest.raises(DaemonRequestError) as missing:
        _routed(app, "agent.secret.list", {"actor": "ghost"})
    assert missing.value.code == ipc_errors.AGENT_NOT_FOUND
    with pytest.raises(Exception) as legacy_missing:
        app._handle_agent("agent.secret.list", {"actor": "ghost"})
    assert getattr(legacy_missing.value, "code", None) == ipc_errors.AGENT_NOT_FOUND


def test_the_declaration_preserves_the_legacy_param_admission(
    app: DaemonApplication,
) -> None:
    app.agents.create("alpha")

    accepted = (
        {},
        {"inactiveSince": None},
        {"excludeWf": None},
        {"excludeWf": "yes"},
        {"excludeWf": 0},
        {"unknown": True},
        {"inactiveSince": 5},
        {"inactiveSince": "30m"},
    )
    for params in accepted:
        legacy = app._handle_agent("agent.list", dict(params))
        routed = _routed(app, "agent.list", dict(params))
        assert _without_run_state(routed["agents"]) == _without_run_state(
            legacy["agents"]
        ), params

    for params in (
        {"actor": None},
        {"actor": "alpha"},
        {"actor": "alpha", "service": "x"},
        {"unknown": 1},
    ):
        assert _routed(app, "agent.secret.list", dict(params)) == app._handle_agent(
            "agent.secret.list", dict(params)
        )

    for params in ({"inactiveSince": True}, {"inactiveSince": [1]}, {"inactiveSince": {}}):
        with pytest.raises(DaemonRequestError) as routed_error:
            _routed(app, "agent.list", dict(params))
        assert routed_error.value.code == ipc_errors.INVALID_ARGUMENT
        with pytest.raises(DaemonRequestError) as legacy_error:
            app._handle_agent("agent.list", dict(params))
        assert str(routed_error.value) == str(legacy_error.value)

    for params in ({"actor": ""}, {"actor": 5}, {"actor": False}, {"actor": []}):
        with pytest.raises(DaemonRequestError) as routed_error:
            _routed(app, "agent.secret.list", dict(params))
        assert routed_error.value.code == ipc_errors.INVALID_ARGUMENT
        assert str(routed_error.value) == "actor must be a non-empty string"


def test_blocked_dormant_and_shared_credential_rows_match_the_declaration(
    app: DaemonApplication,
) -> None:
    app.agents.create("blocked")
    app.agents.create("dormant")
    config_root = Path(app.state_dir) / "shared-config"
    app.agents.create(
        "shared",
        config={
            "sources": [{"path": str(config_root), "required": True}],
            "discovery": "explicit-only",
            "sharedCredentials": {"claude": str(config_root / "target.json")},
        },
    )
    app.agents.block_agent("blocked", reason="provider-quota")
    app.agents.suppress_restore(
        "dormant",
        desired_generation="desired-1",
        last_active_at_ms=None,
        idle_age_ms=90_000,
        restore_threshold_ms=3_600_000,
    )

    routed = _routed(app, "agent.list", {})
    _validate(agent_catalog.AGENT_LIST_SPEC, routed)
    rows = {row["actor"]: row for row in routed["agents"]}
    assert rows["blocked"]["workerState"] == "blocked"
    assert rows["blocked"]["restoreStatus"] == "blocked"
    assert rows["blocked"]["blockReason"] == "provider-quota"
    assert rows["dormant"]["workerState"] == "dormant"
    assert rows["dormant"]["idleAgeMs"] == 90_000
    assert rows["shared"]["sharedCredentials"][0]["harness"] == "claude"
    assert rows["shared"]["sharedCredentials"][0]["status"] in {
        "ready",
        "warning",
        "error",
    }


def test_the_declared_audience_matches_the_legacy_router_gate(
    app: DaemonApplication,
) -> None:
    app.agents.create("alpha")

    assert app.handle("agent.list", {}, _trusted_message_origin="worker")["ok"] is True
    assert (
        app.handle(
            "agent.secret.list", {"actor": "alpha"}, _trusted_message_origin="worker"
        )["ok"]
        is True
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle("agent.config.rebind", {}, _trusted_message_origin="worker")
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_an_unmigrated_agent_method_still_routes_through_the_legacy_branch(
    app: DaemonApplication,
) -> None:
    app.agents.create("alpha")
    routed = app.handle("agent.get", {"name": "alpha"})
    assert routed["agent"]["actor"] == "alpha"
