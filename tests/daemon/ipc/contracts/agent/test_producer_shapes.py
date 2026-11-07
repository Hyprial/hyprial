"""Closed Agent schemas track producer keys, including conditional status overlays."""
import ast
import inspect
import textwrap
from typing import get_args

from hyprial.daemon.impl.cells.observability.model import (
    BaseRunState, ObservationHealth, RunCertainty, RunFacts, RunScope, RunState,
)
from hyprial.daemon.impl.cells.observability.policy import RunPolicy
from hyprial.daemon.impl.cells.observability.reducer import derive_run_state
from hyprial.daemon.impl.request_catalog.agent import AGENT_LIST_SPEC
from hyprial.identity import HOSTED_BY_VALUES
from tests.daemon.ipc.contracts.agent.test_read_specs import app as app


def _literal_dict_keys(function):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return {
        key.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
        for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def test_closed_agent_row_equals_producer_keys(app):
    agent = app.agents.create("shape-worker")
    row = AGENT_LIST_SPEC.result_schema["properties"]["agents"]["items"]
    produced = set(agent.to_json()) - {"entityToken"}
    produced |= set(app._agent_liveness.snapshot(agent.actor))
    produced |= _literal_dict_keys(app._agent_status_json)
    produced |= _literal_dict_keys(app._restore_agent_projection)
    produced |= set(app._cell_run_state_field(agent.uri))
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
