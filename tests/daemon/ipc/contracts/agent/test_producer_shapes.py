"""Closed Agent schemas track producer keys, including conditional status overlays."""
import ast
from dataclasses import replace
import inspect
import textwrap
from types import SimpleNamespace
from typing import get_args

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.cells.observability.model import (
    BaseRunState, ObservationHealth, RunCertainty, RunFacts, RunScope, RunState,
)
from hyprial.daemon.impl.cells.observability.policy import RunPolicy
from hyprial.daemon.impl.cells.observability.reducer import derive_run_state
from hyprial.daemon.impl.request_catalog.agent import AGENT_LIST_SPEC
from hyprial.daemon.impl.user_agent.admin import user_agent_row_fields
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleState, _LifecycleStore
from hyprial.identity import HOSTED_BY_VALUES
from hyprial.kernel import ipc_errors
from tests.daemon.ipc.contracts.agent.test_read_specs import app as app
from tests.daemon.lifecycle.process.manager.support import _spec


def _literal_dict_keys(function):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return {
        key.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
        for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def test_closed_agent_row_equals_producer_keys(app, monkeypatch):
    agent = app.agents.create("shape-worker", preferred_harness="codex")
    row = AGENT_LIST_SPEC.result_schema["properties"]["agents"]["items"]
    assert "startupFailure" not in app._handle_agent("agent.list", {})["agents"][0]
    store = _LifecycleStore(app.state_db)
    target = replace(_spec(agent.actor), actor=agent.uri)
    target = replace(target, harness=replace(target.harness, cwd=agent.cwd))
    failed = replace(LifecycleOperation.create("shape-start-failed", target),
                     observed_entity_token=agent.entity_token)
    store.reserve(failed)
    store.set_state(failed.operation_id, LifecycleState.COMPENSATED,
                    "Harness did not become ready", ipc_errors.HARNESS_START_FAILED)
    with monkeypatch.context() as patch:
        patch.setattr(app, "_lifecycle_manager", store)
        observed = app._handle_agent("agent.list", {})["agents"][0]
        assert observed["startupFailure"]["operationId"] == failed.operation_id
        assert set(observed["startupFailure"]) == set(row["properties"]["startupFailure"]["properties"])
        Draft202012Validator(row).validate(observed)
        store.reserve(LifecycleOperation.create("shape-start-retry", target))
        assert "startupFailure" not in app._handle_agent("agent.list", {})["agents"][0]
    produced = set(agent.to_json()) - {"entityToken"}
    produced |= set(app._agent_liveness.snapshot(agent.actor))
    produced |= _literal_dict_keys(app._agent_status_json)
    produced |= _literal_dict_keys(app._restore_agent_projection)
    produced |= set(app._cell_run_state_field(agent.uri))
    produced |= set(observed)
    produced |= set(user_agent_row_fields(
        SimpleNamespace(capabilities={"template": "user"}, owner=agent.owner)
    ))
    assert produced == set(row["properties"])
    assert set(row["properties"]["hostedBy"]["enum"]) == {*HOSTED_BY_VALUES, None}


def test_run_state_shape_and_enum_sources(app):
    app.agents.create("shape-run")
    result = app._handle_agent("agent.list", {})["agents"][0]["runState"]
    schema = AGENT_LIST_SPEC.result_schema["properties"]["agents"]["items"]["properties"]["runState"]
    assert set(result) == set(schema["properties"])
    for key, source in (("state", RunState), ("baseState", BaseRunState),
                        ("certainty", RunCertainty), ("scope", RunScope),
                        ("observationHealth", ObservationHealth)):
        assert set(schema["properties"][key]["enum"]) == set(get_args(source))
    projected = derive_run_state(RunFacts(), RunPolicy(), now_ms=100, monotonic_ms=100)
    assert set(projected) == set(schema["properties"]) - {"lastKnown", "epoch", "revision", "runtimeIncarnation"}
    for key in ("coverage", "thresholds"):
        assert set(projected[key]) == set(schema["properties"][key]["properties"])
