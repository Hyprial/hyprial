"""UUID addressing §2.2 / ruling U2: ``agent.rename`` over the request catalog.

Rename changes only the display name.  The answer is the renamed agent row,
the target may be named by actor, display name, ``a.`` id or URI, and every
registry refusal keeps its own IPC code.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.agent.rename import AGENT_RENAME_SPEC
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.ipc.contracts.agent.test_read_specs import app as app


def _rename(app, **params: Any) -> dict[str, Any]:
    result = METHOD_REGISTRY.invoke(app, "agent.rename", params)
    errors = list(Draft202012Validator(AGENT_RENAME_SPEC.result_schema).iter_errors(result))
    assert not errors, errors
    return result


def _refused(app, **params: Any) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, "agent.rename", params)
    return caught.value


def test_rename_is_a_declared_mutating_method_with_its_error_codes() -> None:
    spec = METHOD_REGISTRY.get("agent.rename").spec  # type: ignore[union-attr]
    assert spec is AGENT_RENAME_SPEC
    assert spec.mutates is True
    assert spec.retry == "never"
    assert spec.audience == ("local-operator", "bound-agent")
    assert {error.code for error in spec.errors} == {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.INVALID_AGENT_NAME,
        ipc_errors.AGENT_NOT_FOUND,
        ipc_errors.AGENT_EXISTS,
        ipc_errors.USER_AGENT_IMMUTABLE,
        "AGENT_ERROR",
    }
    assert "agent.rename" in _IPC_STATS_METHODS


def test_rename_is_served_over_http_under_the_agent_write_profile() -> None:
    # Owner decision: agents may rename over HTTP; same profile as
    # agent.create/agent.stop (identity none, never retried).
    from hyprial.daemon.impl.request_catalog import daemon_catalog
    from hyprial.shell.impl.request_http.catalog import build_view

    spec = METHOD_REGISTRY.get("agent.rename").spec  # type: ignore[union-attr]
    create = METHOD_REGISTRY.get("agent.create").spec  # type: ignore[union-attr]
    assert spec.http_exposed is True
    assert spec.http_binding == create.http_binding
    assert spec.idempotency is not None and spec.idempotency.mode == "never"

    entries = [
        dict(row, declared=True, state="available", callable=True)
        for row in daemon_catalog()["methods"]
    ]
    view = build_view(
        {"version": "test", "nodeId": "node", "owner": "owner", "epoch": "test",
         "phase": "serving"},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "test",
         "packageVersion": "test", "methods": entries},
        socket_path="/tmp/rename-test.sock", local_entries=entries,
    )
    assert "agent.rename" not in view.skipped
    route = view.routes["agent.rename"]
    assert route.path == "/daemon/request/agent.rename"
    assert (route.mutates, route.retry, route.callable) == (True, "never", True)
    assert route.http_binding["identity"] == {"mode": "none", "param": None}


def test_rename_answers_the_renamed_row_and_keeps_the_actor_key(app) -> None:
    agent = app.agents.create("rename-me")
    row = _rename(app, name="rename-me", displayName="shown")["agent"]
    assert row["actor"] == "rename-me"
    assert row["uri"] == agent.uri
    assert row["agentId"] == agent.agent_id
    assert row["displayName"] == "shown"
    assert "entityToken" not in row
    again = app._handle_agent("agent.get", {"name": "shown"})["agent"]
    assert (again["actor"], again["displayName"]) == ("rename-me", "shown")


@pytest.mark.parametrize("spelling", ["actor", "display", "id", "uri"])
def test_rename_accepts_every_spelling_of_the_target(app, spelling) -> None:
    agent = app.agents.create("spelled")
    app.agents.rename("spelled", "first")
    target = {
        "actor": "spelled",
        "display": "first",
        "id": agent.agent_id,
        "uri": agent.uri,
    }[spelling]
    row = _rename(app, name=target, displayName="second")["agent"]
    assert (row["actor"], row["displayName"]) == ("spelled", "second")


def test_renaming_back_to_the_actor_clears_the_display_name(app) -> None:
    app.agents.create("homeward")
    app.agents.rename("homeward", "away")
    row = _rename(app, name="away", displayName="homeward")["agent"]
    assert row["displayName"] is None


def test_unknown_target_is_agent_not_found(app) -> None:
    error = _refused(app, name="nobody", displayName="someone")
    assert error.code == ipc_errors.AGENT_NOT_FOUND


def test_a_taken_name_is_agent_exists(app) -> None:
    app.agents.create("holder")
    app.agents.create("mover")
    error = _refused(app, name="mover", displayName="holder")
    assert error.code == ipc_errors.AGENT_EXISTS


@pytest.mark.parametrize("display", ["agent:op:node:x", "Bad Name"])
def test_an_invalid_display_name_is_invalid_agent_name(app, display) -> None:
    app.agents.create("plain")
    error = _refused(app, name="plain", displayName=display)
    assert error.code == "INVALID_AGENT_NAME"


def test_a_destroyed_agents_name_can_be_taken_by_rename(app) -> None:
    survivor = app.agents.create("survivor")
    app._agent_registry.create("departed")
    app._agent_registry.destroy("departed")
    renamed = app.handle("agent.rename", {"name": "survivor", "displayName": "departed"})
    assert renamed["agent"]["displayName"] == "departed"
    assert app.agents.require("survivor").agent_id == survivor.agent_id


@pytest.mark.parametrize(
    "params, message",
    [
        ({"displayName": "x"}, "name must be a non-empty string"),
        ({"name": "x"}, "displayName must be a non-empty string"),
        ({"name": "x", "displayName": ""}, "displayName must be a non-empty string"),
        ({"name": 5, "displayName": "x"}, "name must be a non-empty string"),
    ],
)
def test_malformed_params_are_invalid_argument(app, params, message) -> None:
    error = _refused(app, **params)
    assert error.code == ipc_errors.INVALID_ARGUMENT
    assert str(error) == message


@pytest.mark.parametrize(
    "code",
    [
        "DOMAIN_COMMAND_TIMEOUT",
        "DOMAIN_EVENTS_CLOSED",
        "DOMAIN_EVENT_CAPACITY",
        "DOMAIN_CORRELATION_CONFLICT",
    ],
)
def test_an_undeclared_domain_command_error_is_agent_error_with_its_cause(
    app, monkeypatch, code
) -> None:
    from hyprial.daemon.impl.composition import DomainCommandError

    def _unsettled(name: str, display: str) -> None:
        raise DomainCommandError(code, "domain command did not settle")

    app.agents.create("slow")
    monkeypatch.setattr(app.agents, "rename", _unsettled)
    error = _refused(app, name="slow", displayName="later")
    assert error.code == "AGENT_ERROR"
    assert code in str(error)
    assert "domain command did not settle" in str(error)
    assert error.data is None


def test_an_agent_error_with_an_undeclared_code_is_agent_error_with_its_cause(
    app, monkeypatch
) -> None:
    from hyprial.identity import AgentError

    class _OddAgentError(AgentError):
        code = "AGENT_SOMETHING_ELSE"

    def _odd(name: str, display: str) -> None:
        raise _OddAgentError("registry refused oddly")

    app.agents.create("odd")
    monkeypatch.setattr(app.agents, "rename", _odd)
    error = _refused(app, name="odd", displayName="even")
    assert error.code == "AGENT_ERROR"
    assert "registry refused oddly" in str(error)
