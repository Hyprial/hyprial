"""Executable C run-state contract, called by the isolated lifecycle runner."""
import json
from pathlib import Path

from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.daemon.impl.cells.observability.policy import RunPolicies
from hyprial.daemon.impl.cells.observability.projection import RunProjection


def _validate(value, schema):
    """Validate the finite JSON-schema vocabulary used by this corpus."""
    names = {dict: "object", list: "array", str: "string", int: "integer", bool: "boolean", type(None): "null"}
    expected = schema["type"]
    assert names.get(type(value)) in ([expected] if isinstance(expected, str) else expected)
    if "enum" in schema:
        assert value in schema["enum"]
    if value is None:
        return
    if "minimum" in schema:
        assert value >= schema["minimum"]
    if isinstance(value, dict):
        assert set(schema.get("required", ())) <= value.keys()
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert value.keys() <= properties.keys()
        for key, item in value.items():
            _validate(item, properties[key])
    if isinstance(value, list):
        for item in value:
            _validate(item, schema["items"])


def exercise_run_state() -> int:
    vectors = json.loads(Path(__file__).with_name("run-state-vectors.json").read_text())
    inputs = {case["name"]: ("cell", RunFacts(**case["facts"]), RunPolicies.from_payload({"defaults": case.get("policy", {})}).default)
              for case in vectors}
    rows = RunProjection().publish(inputs, now_ms=1000, monotonic_ms=1000)
    schema = json.loads(Path(__file__).with_name("run-state.schema.json").read_text())
    assert len({row["revision"] for row in rows.values()}) == 1
    assert len({row["epoch"] for row in rows.values()}) == 1
    required = {"state", "baseState", "reason", "certainty", "scope", "sinceMs", "observedAtMs",
                "turnAgeMs", "lastActivityAtMs", "coverage", "observationHealth", "suspicions",
                "policyRevision", "providerBlocked", "runtimeIncarnation", "revision", "epoch",
                "lastKnown", "thresholds"}
    for case in vectors:
        row = rows[case["name"]]
        _validate(row, schema)
        assert row["state"] == case["state"], case["name"]
        assert row["reason"] == case["reason"], case["name"]
        assert set(row) == required, case["name"]
        assert row["certainty"] in {"observed", "inferred", "unknown"}
        assert all(type(value) is bool for value in row["coverage"].values())
        assert isinstance(row["runtimeIncarnation"], str)
        assert row["thresholds"]["permissionLongAfterMs"] == 300_000
        assert not ({"entityToken", "channelLease", "pidBirth", "sessionRef"} & row.keys())
        json.dumps(row, allow_nan=False)
    return len(vectors)


def exercise_run_state_display() -> int:
    """The same human diagnostics on list/top, without rewriting JSON facts."""
    from hyprial.shell.impl.cli.output.status import format_run_state

    corpus = json.loads(Path(__file__).with_name("run-state-display-vectors.json").read_text())
    for case in corpus["cases"]:
        run = None if case["runState"] is None else {**corpus["defaults"], **case["runState"]}
        before = json.dumps(run, sort_keys=True)
        assert format_run_state(run) == case["display"], case["name"]
        assert json.dumps(run, sort_keys=True) == before, case["name"]
    return len(corpus["cases"])
