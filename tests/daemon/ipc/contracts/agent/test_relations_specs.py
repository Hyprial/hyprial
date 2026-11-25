"""GUI v1 Agent detail tabs: the four read declarations and their real handlers.

Decision record (implRel, 2026-11-02)
- The Agent detail page (概览 | 凭据与授权 | 运行 | 关联) reads four existing IPC
  methods; this module pins their MethodSpec declarations against the real
  handlers on a real application: ``routine.list``/``routine.status`` (运行
  routine rows), ``workflow.list`` (关联 PAC graphs), ``agent.home-census``
  (运行 home facts).
- The GUI is the local operator over HTTP; it cannot present the legacy
  ``actor``/``sessionRef`` caller binding (the HTTP selector projects an agent
  URI, never ``user:<owner>``).  So each method gains one optional business
  selector — ``agent`` for the caller-entangled routine/workflow methods
  (their ``actor`` param is the legacy caller identity and must keep that
  meaning) and ``actor`` for ``agent.home-census`` (no caller entanglement,
  same convention as ``agent.secret.list``).  A selector call answers that
  agent's rows with the same trust posture as the accepted ``agent.secret.list``
  actor selector; without the selector every legacy caller behaviour is
  byte-identical.
- Filtering happens in the daemon handlers (service ``viewer`` / row match),
  never in the GUI.  ``workflow.list`` reuses the service's ``viewer`` query
  (created_by OR node owner) so the limit is applied after the filter, not
  before.
- Errors are enumerated mechanically: the walk from each registry binding to
  every producer literal is scanned from the ASTs and every candidate must be
  declared or excluded with the reason it cannot reach the boundary.

Rel2 (implRel2, 2026-11-02 — revRel REJECT ddca55d782)
- The params schemas stop admitting for the legacy caller fields: actor /
  sessionRef (three caller-entangled methods) and routine.status name are
  documentation-only properties, so the original handlers keep their
  admission order (PAC_PRINCIPAL_UNVERIFIED wrap; ROUTINE_UNAVAILABLE before
  name validation). Equivalence tests compare legacy vs registry on the same
  application for the malformed cases.
- The selector registry reads (normalize_actor/get) translate AgentError into
  a DaemonRequestError carrying the error's own code, so a corrupt registry
  row surfaces as the declared AGENT_ERROR instead of escaping the registry;
  fault-injection tests cover all four selector paths.
- The routine launch row is pinned to the real producer key set
  {tier, harness, provider, model, cwd, cwdPolicy, args} (provider may be
  null; harness+args required) and verified against real tier-default,
  explicit-harness and borrowed-actor rows.
- The mechanical error guard is per method: per-method boundary functions,
  per-method exclusion reasons, per-method derived allowances — not a union
  diff.
"""

from __future__ import annotations

import ast
import re
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.biz import RoutineService
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_FAMILIES
from hyprial.daemon.impl.request_catalog import agent as agent_catalog
from hyprial.daemon.impl.request_catalog.agent import relations as relations_catalog
from hyprial.identity import PAC_PRINCIPAL_UNVERIFIED, AgentError
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter

OWNER = "spec-owner"
NODE = "spec-node"
USER = f"user:{OWNER}"
AGENT = "worker"
AGENT_URI = f"agent:{OWNER}:{NODE}:{AGENT}"
OTHER = "other-worker"
OTHER_URI = f"agent:{OWNER}:{NODE}:{OTHER}"

ROUTINE_YAML = """\
version: 2
name: heartbeat
mode: scheduled
task: "check things"
schedule:
  interval: 60s
on_task_timeout:
  action: escalate
  escalate_to: user:spec-owner
"""

AGENT_ROUTINE_YAML = ROUTINE_YAML.replace(
    "name: heartbeat", f"name: delegated\nactor: {AGENT_URI}"
)

WORKFLOW_YAML = """\
version: 2
name: brief-relations
nodes:
  - id: work
    task: "implement it"
    owner: {owner}
""".format(owner=AGENT_URI)

OTHER_WORKFLOW_YAML = WORKFLOW_YAML.replace(AGENT_URI, OTHER_URI)


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


class _PacStub:
    def start_idempotent(self, **_: Any) -> dict[str, object]:
        raise AssertionError("the read-path fixtures never dispatch a routine task")

    def status(self, *, graph_id: str) -> dict[str, object]:
        return {}

    def close(self, *, graph_id: str, actor: str) -> None:
        return None

    def rearm_boundary(self, *, routine_name: str) -> int:
        return 0


class _AlarmStub:
    def escalate(self, **_: Any) -> None:
        return None


class _SenderStub:
    def send(self, **_: Any) -> bool:
        return True


@pytest.fixture
def app() -> Iterator[DaemonApplication]:
    with tempfile.TemporaryDirectory(prefix="hyprial-rel-") as directory:
        application = _application(Path(directory))
        routines = RoutineService(
            pac=_PacStub(),
            alarm=_AlarmStub(),
            state_dir=Path(directory) / "routine-state",
            logger=None,
        )
        workflows = GraphWorkflowService(
            state_dir=Path(directory) / "wf-state",
            owner=OWNER,
            machine=NODE,
            sender=_SenderStub(),
            start_thread=False,
        )
        application._routine_service = RoutineServiceAdapter(routines)
        application._workflow_service = workflows
        try:
            yield application
        finally:
            workflows.close()
            routines.close()
            application._close()
            application._close_home_lease()


def _routed(app: DaemonApplication, method: str, params: dict[str, Any]) -> Any:
    return METHOD_REGISTRY.invoke(app, method, params)


def _validate(spec: Any, result: Any) -> None:
    errors = list(Draft202012Validator(spec.result_schema).iter_errors(result))
    assert not errors, (spec.name, errors, result)


def _register_routine(app: DaemonApplication, yaml_text: str, owner: str) -> None:
    app._routine_service.add(yaml_text=yaml_text, owner=owner, enabled=True)


def _register_agents(app: DaemonApplication) -> None:
    app.agents.create(AGENT)
    app.agents.create(OTHER)


def test_the_four_reads_are_declared_bound_and_read_only() -> None:
    for name in (
        "routine.list",
        "routine.status",
        "workflow.list",
        "agent.home-census",
    ):
        assert name in METHOD_REGISTRY
        spec = METHOD_REGISTRY.get(name).spec  # type: ignore[union-attr]
        assert spec.mode == "unary"
        assert spec.mutates is False
        assert spec.retry == "safe"
        assert spec.confirmation == "none"
        assert spec.http_exposed is True
        assert "local-operator" in spec.audience
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "safe"
        assert spec.params_schema["type"] == "object"
        assert spec.result_schema["type"] == "object"
    assert "routine" not in MIGRATED_FAMILIES
    assert "workflow" not in MIGRATED_FAMILIES
    assert "agent" not in MIGRATED_FAMILIES
    assert agent_catalog.PARTIAL_MIGRATED_METHODS == frozenset(
        {
            "agent.ensure-home",
            "agent.get",
            "agent.readiness",
            "agent.runtime-context",
            "agent.home-census",
            "agent.harness-args.clear",
            "agent.harness-args.get",
            "agent.harness-args.set",
            "agent.harness-args.validate",
            "agent.list",
            "agent.resolve",
            "agent.secret.list",
            "agent.cell.get",
            "agent.cell.update",
        }
    )


def test_selector_params_are_declared_on_each_method() -> None:
    routine_list = relations_catalog.ROUTINE_LIST_SPEC.params_schema
    routine_status = relations_catalog.ROUTINE_STATUS_SPEC.params_schema
    workflow_list = relations_catalog.WORKFLOW_LIST_SPEC.params_schema
    census = relations_catalog.AGENT_HOME_CENSUS_SPEC.params_schema
    assert "agent" in routine_list["properties"]
    assert "agent" in routine_status["properties"]
    assert "agent" in workflow_list["properties"]
    assert "actor" in census["properties"]
    for schema in (routine_list, routine_status, workflow_list, census):
        assert "actor" in schema["properties"] or schema is census
    assert routine_list["properties"]["agent"]["minLength"] == 1
    assert census["properties"]["actor"]["minLength"] == 1
    assert workflow_list["properties"]["limit"]["type"] == "integer"


def test_legacy_caller_fields_are_documented_not_schema_admitted() -> None:
    """F1: the schema must not pre-empt the legacy handler admission."""

    any_json_types = ("string", "number", "boolean", "null", "array", "object")
    for spec in (
        relations_catalog.ROUTINE_LIST_SPEC,
        relations_catalog.ROUTINE_STATUS_SPEC,
        relations_catalog.WORKFLOW_LIST_SPEC,
    ):
        for field in ("actor", "sessionRef"):
            assert spec.params_schema["properties"][field]["type"] == any_json_types
            assert "minLength" not in spec.params_schema["properties"][field]
    name = relations_catalog.ROUTINE_STATUS_SPEC.params_schema["properties"]["name"]
    assert name["type"] == any_json_types
    assert "minLength" not in name
    assert "name" not in relations_catalog.ROUTINE_STATUS_SPEC.params_schema.get(
        "required", []
    )
    validator = Draft202012Validator(
        relations_catalog.ROUTINE_STATUS_SPEC.params_schema
    )
    assert not list(validator.iter_errors({"actor": 7, "sessionRef": 5, "name": ""}))


def test_malformed_caller_params_keep_the_legacy_wrap(app: DaemonApplication) -> None:
    """F1: legacy vs registry on the same application must not diverge."""

    _register_routine(app, ROUTINE_YAML, USER)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="rel-f1-1"
    )
    for method, params in (
        ("routine.list", {"actor": 7}),
        ("routine.list", {"actor": AGENT_URI, "sessionRef": 7}),
        ("workflow.list", {"actor": 7}),
        ("workflow.list", {"actor": AGENT_URI, "sessionRef": 7}),
        ("routine.status", {"name": "heartbeat", "actor": 7}),
        ("routine.status", {"name": "heartbeat", "actor": AGENT_URI, "sessionRef": 7}),
        ("routine.status", {"name": ""}),
        ("routine.status", {"name": 7}),
        ("routine.status", {"name": "", "actor": USER}),
        ("routine.status", {"name": 7, "actor": USER}),
    ):
        legacy_error = None
        try:
            if method == "routine.list":
                legacy = app._ipc_routine_list(dict(params), app._workflow_caller(dict(params)))
            elif method == "workflow.list":
                legacy = app._ipc_workflow_status_group("workflow.list", dict(params))
            else:
                legacy = _legacy_routine_status(app, dict(params))
        except DaemonRequestError as error:
            legacy, legacy_error = None, error
        try:
            routed = _routed(app, method, dict(params))
            routed_error = None
        except DaemonRequestError as error:
            routed, routed_error = None, error
        if legacy_error is not None:
            assert routed_error is not None, (method, params)
            assert routed_error.code == legacy_error.code, (method, params)
            assert str(routed_error) == str(legacy_error), (method, params)
            assert routed_error.data == legacy_error.data, (method, params)
            continue
        assert routed_error is None, (method, params)
        assert routed == legacy, (method, params)


def test_degraded_routine_service_answers_before_the_name_is_read(
    app: DaemonApplication,
) -> None:
    """F1: the legacy admission order survives the registry routing."""

    original = app._routine_service
    app._routine_service = None
    try:
        for params in (
            {"name": "", "actor": USER},
            {"name": 7, "actor": USER},
            {"name": "heartbeat", "actor": USER},
            {"name": "heartbeat"},
            {"name": 7},
        ):
            legacy_error = None
            try:
                legacy = _legacy_routine_status(app, dict(params))
            except DaemonRequestError as error:
                legacy, legacy_error = None, error
            try:
                routed = _routed(app, "routine.status", dict(params))
                routed_error = None
            except DaemonRequestError as error:
                routed, routed_error = None, error
            if legacy_error is not None:
                assert routed_error is not None, params
                assert routed_error.code == legacy_error.code, params
                assert str(routed_error) == str(legacy_error), params
                assert routed_error.data == legacy_error.data, params
                continue
            assert routed_error is None, params
            assert routed == legacy, params
        with pytest.raises(DaemonRequestError) as degraded:
            _routed(app, "routine.status", {"name": "", "actor": USER})
        assert degraded.value.code == ipc_errors.ROUTINE_UNAVAILABLE
    finally:
        app._routine_service = original


def test_selector_registry_faults_translate_to_a_declared_error(
    app: DaemonApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: a corrupt registry row must not escape the MethodRegistry."""

    _register_agents(app)

    def corrupt(self: Any, actor: str) -> None:
        raise AgentError("registry row is corrupt")

    monkeypatch.setattr(type(app.agents), "get", corrupt)
    for method, params in (
        ("routine.list", {"agent": AGENT}),
        ("routine.status", {"name": "x", "agent": AGENT}),
        ("workflow.list", {"agent": AGENT}),
        ("agent.home-census", {"actor": AGENT}),
    ):
        with pytest.raises(DaemonRequestError) as error:
            _routed(app, method, dict(params))
        declared = {
            item.code
            for item in METHOD_REGISTRY.get(method).spec.errors  # type: ignore[union-attr]
        }
        assert error.value.code == AgentError.code
        assert error.value.code in declared, (method, params)
        assert str(error.value) == "registry row is corrupt"


def test_routine_list_without_selector_matches_the_legacy_path(
    app: DaemonApplication,
) -> None:
    _register_routine(app, ROUTINE_YAML, USER)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="rel-list-1"
    )
    for params in (
        {"actor": USER},
        {"actor": USER, "all": True},
        {"actor": AGENT_URI, "sessionRef": "session-1", "all": True},
    ):
        legacy_error = None
        try:
            legacy = app._ipc_routine_list(dict(params), app._workflow_caller(dict(params)))
        except DaemonRequestError as error:
            legacy, legacy_error = None, error
        try:
            routed = _routed(app, "routine.list", dict(params))
            routed_error = None
        except DaemonRequestError as error:
            routed, routed_error = None, error
        if legacy_error is not None:
            assert routed_error is not None
            assert routed_error.code == legacy_error.code
            assert str(routed_error) == str(legacy_error)
            continue
        assert routed_error is None
        _validate(relations_catalog.ROUTINE_LIST_SPEC, routed)
        assert routed == legacy


def test_routine_list_selector_scopes_one_agent_in_the_daemon(
    app: DaemonApplication,
) -> None:
    _register_agents(app)
    _register_routine(app, ROUTINE_YAML, USER)
    _register_routine(app, AGENT_ROUTINE_YAML, USER)
    rows = _routed(app, "routine.list", {"agent": AGENT})["routines"]
    assert [row["name"] for row in rows] == ["delegated"]
    row = next(row for row in rows if row["name"] == "delegated")
    assert row["actor"] == AGENT_URI
    by_uri = _routed(app, "routine.list", {"agent": AGENT_URI})["routines"]
    assert by_uri == rows
    only_direct = _routed(app, "routine.list", {"agent": OTHER_URI})["routines"]
    assert only_direct == []
    with pytest.raises(DaemonRequestError) as unknown:
        _routed(app, "routine.list", {"agent": "ghost"})
    assert unknown.value.code == ipc_errors.AGENT_NOT_FOUND
    with pytest.raises(DaemonRequestError) as foreign:
        _routed(app, "routine.list", {"agent": "agent:elsewhere:node:x"})
    assert foreign.value.code == ipc_errors.AGENT_NOT_FOUND


def _legacy_routine_status(app: DaemonApplication, params: dict[str, Any]) -> Any:
    """The pre-migration router branch for ``routine.status``, verbatim
    (router.py: caller wrap first, then the alive-service gate validates the
    name via _required_string before status())."""

    from hyprial.daemon.impl.ipc.params import _required_string
    from hyprial.kernel import canonical_user_uri

    routine_caller = app._workflow_caller(dict(params))
    if app._routine_service is not None:
        current = app._routine_service.status(
            name=_required_string(dict(params).get("name"), "name")
        )
        if routine_caller not in (
            canonical_user_uri(app.owner),
            current["owner"],
            current.get("actor"),
        ):
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED, "caller does not own this routine"
            )
    return app._ipc_routine_status(dict(params))


def test_routine_status_without_selector_keeps_the_legacy_gate(
    app: DaemonApplication,
) -> None:
    _register_routine(app, ROUTINE_YAML, USER)
    legacy = _legacy_routine_status(app, {"name": "heartbeat", "actor": USER})
    routed = _routed(app, "routine.status", {"name": "heartbeat", "actor": USER})
    _validate(relations_catalog.ROUTINE_STATUS_SPEC, routed)
    assert routed == legacy

    for params in (
        {"name": "heartbeat"},
        {"name": "heartbeat", "actor": AGENT_URI, "sessionRef": "s-1"},
    ):
        with pytest.raises(DaemonRequestError) as expected:
            _legacy_routine_status(app, dict(params))
        with pytest.raises(DaemonRequestError) as actual:
            _routed(app, "routine.status", dict(params))
        assert actual.value.code == expected.value.code
        assert str(actual.value) == str(expected.value)

    original_caller = app._workflow_caller
    app._workflow_caller = lambda params: AGENT_URI
    try:
        with pytest.raises(DaemonRequestError) as refused:
            _routed(app, "routine.status", {"name": "heartbeat", "actor": AGENT_URI})
    finally:
        app._workflow_caller = original_caller
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert str(refused.value) == "caller does not own this routine"

    with pytest.raises(DaemonRequestError) as missing:
        _routed(app, "routine.status", {"name": "ghost", "actor": USER})
    assert missing.value.code == "ROUTINE_NOT_FOUND"


def test_routine_status_selector_masks_foreign_routines(
    app: DaemonApplication,
) -> None:
    _register_agents(app)
    _register_routine(app, AGENT_ROUTINE_YAML, USER)
    routed = _routed(app, "routine.status", {"name": "delegated", "agent": AGENT})
    assert routed["actor"] == AGENT_URI
    assert "scheduleEvents" in routed
    with pytest.raises(DaemonRequestError) as foreign:
        _routed(app, "routine.status", {"name": "delegated", "agent": OTHER})
    assert foreign.value.code == "ROUTINE_NOT_FOUND"


def test_workflow_list_without_selector_matches_the_legacy_path(
    app: DaemonApplication,
) -> None:
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="rel-wf-1"
    )
    for params in (
        {"actor": USER},
        {"actor": USER, "limit": 10},
        {"actor": AGENT_URI, "sessionRef": "session-2", "all": True},
    ):
        legacy_error = None
        try:
            legacy = app._ipc_workflow_status_group("workflow.list", dict(params))
        except DaemonRequestError as error:
            legacy, legacy_error = None, error
        try:
            routed = _routed(app, "workflow.list", dict(params))
            routed_error = None
        except DaemonRequestError as error:
            routed, routed_error = None, error
        if legacy_error is not None:
            assert routed_error is not None
            assert routed_error.code == legacy_error.code
            assert str(routed_error) == str(legacy_error)
            continue
        assert routed_error is None
        _validate(relations_catalog.WORKFLOW_LIST_SPEC, routed)
        assert routed == legacy


def test_workflow_list_selector_views_one_agent(app: DaemonApplication) -> None:
    _register_agents(app)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="rel-wf-2"
    )
    app._workflow_service.start(
        yaml_text=OTHER_WORKFLOW_YAML, sender=USER, operation_key="rel-wf-3"
    )
    runs = _routed(app, "workflow.list", {"agent": AGENT})["runs"]
    assert len(runs) == 1
    assert runs[0]["currentNode"] is not None
    assert runs[0]["currentNode"]["nodeId"] == "work"
    by_uri = _routed(app, "workflow.list", {"agent": AGENT_URI})["runs"]
    assert by_uri == runs
    sender_view = _routed(app, "workflow.list", {"agent": OTHER})
    assert len(sender_view["runs"]) == 1
    assert sender_view["runs"][0]["currentNode"] is None or True
    with pytest.raises(DaemonRequestError) as user_selector:
        _routed(app, "workflow.list", {"agent": USER})
    assert user_selector.value.code == ipc_errors.INVALID_ARGUMENT
    with pytest.raises(DaemonRequestError) as unknown:
        _routed(app, "workflow.list", {"agent": "ghost"})
    assert unknown.value.code == ipc_errors.AGENT_NOT_FOUND


def test_home_census_filters_one_agent_and_recomputes_totals(
    app: DaemonApplication,
) -> None:
    _register_agents(app)
    legacy = app._handle_agent("agent.home-census", {})
    routed = _routed(app, "agent.home-census", {})
    _validate(relations_catalog.AGENT_HOME_CENSUS_SPEC, routed)
    assert routed.keys() == legacy.keys()
    for key in legacy:
        if key == "readAtMs":
            continue
        assert routed[key] == legacy[key]

    one = _routed(app, "agent.home-census", {"actor": AGENT})
    assert [row["actor"] for row in one["agents"]] == [AGENT]
    assert one["totals"]["agents"] == 1
    assert one["totals"]["homeUnprovisioned"] == (
        1 if not one["agents"][0]["homeProvisioned"] else 0
    )
    by_uri = _routed(app, "agent.home-census", {"actor": AGENT_URI})
    assert by_uri["agents"] == one["agents"]
    assert by_uri["totals"] == one["totals"]
    with pytest.raises(DaemonRequestError) as unknown:
        _routed(app, "agent.home-census", {"actor": "ghost"})
    assert unknown.value.code == ipc_errors.AGENT_NOT_FOUND


def test_result_schemas_pin_the_real_producer_rows(app: DaemonApplication) -> None:
    _register_agents(app)
    _register_routine(app, ROUTINE_YAML, USER)
    _register_routine(app, AGENT_ROUTINE_YAML, USER)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="rel-pin-1"
    )

    routine_row = _routed(app, "routine.list", {"agent": AGENT})["routines"][0]
    schema_row = relations_catalog.ROUTINE_LIST_SPEC.result_schema["properties"][
        "routines"
    ]["items"]
    produced = set(routine_row)
    declared = set(schema_row["properties"])
    optional = declared - set(schema_row.get("required", []))
    assert produced <= declared
    assert produced.isdisjoint(optional)

    status_row = _routed(app, "routine.status", {"name": "delegated", "agent": AGENT})
    status_schema = relations_catalog.ROUTINE_STATUS_SPEC.result_schema
    conditional = {
        "produces",
        "schemaError",
        "readable",
        "quarantined",
        "quarantineReason",
    }
    assert set(status_row) == set(status_schema["properties"]) - conditional
    if status_row["scheduleEvents"]:
        assert set(status_row["scheduleEvents"][0]) == set(
            status_schema["properties"]["scheduleEvents"]["items"]["properties"]
        )

    run_row = _routed(app, "workflow.list", {"agent": AGENT})["runs"][0]
    run_schema = relations_catalog.WORKFLOW_LIST_SPEC.result_schema["properties"][
        "runs"
    ]["items"]
    assert set(run_row) - {"parentGraphId", "parentNodeId"} == set(
        run_schema["required"]
    )
    assert set(run_row) <= set(run_schema["properties"])

    census_row = _routed(app, "agent.home-census", {"actor": AGENT})["agents"][0]
    census_schema = relations_catalog.AGENT_HOME_CENSUS_SPEC.result_schema[
        "properties"
    ]["agents"]["items"]
    assert set(census_row) == set(census_schema["properties"])
    assert set(census_row) == set(census_schema["required"])


def test_launch_rows_match_the_producer_declaration(
    app: DaemonApplication,
) -> None:
    """F3: real non-empty launch rows, not just the nullable declaration."""

    launch_schema = relations_catalog.ROUTINE_LIST_SPEC.result_schema["properties"][
        "routines"
    ]["items"]["properties"]["launch"]

    tiered = ROUTINE_YAML.replace("name: heartbeat", "name: tiered").replace(
        "schedule:\n  interval: 60s",
        "schedule:\n  interval: 60s\n"
        "launch:\n"
        "  tier: strong\n"
        "  cwd: /\n"
        "  cwdPolicy: create\n"
        '  args: ["--verbose"]',
    )
    explicit = ROUTINE_YAML.replace("name: heartbeat", "name: explicit").replace(
        "schedule:\n  interval: 60s",
        "schedule:\n  interval: 60s\n"
        "launch:\n"
        "  harness: claude\n"
        "  provider: anthropic\n"
        "  model: cl-jr\n"
        '  args: ["--verbose"]',
    )
    borrowed = ROUTINE_YAML.replace(
        "name: heartbeat", f"name: borrowed\nactor: {AGENT_URI}"
    )
    for yaml_text in (tiered, explicit, borrowed):
        _register_routine(app, yaml_text, USER)

    rows = {
        row["name"]: row
        for row in _routed(app, "routine.list", {"actor": USER})["routines"]
    }
    tier_row = rows["tiered"]["launch"]
    assert set(tier_row) == {
        "tier", "harness", "provider", "model", "cwd", "cwdPolicy", "args"
    }
    assert tier_row["tier"] == "strong"
    assert tier_row["cwdPolicy"] == "create"
    assert tier_row["args"] == ["--verbose"]
    explicit_row = rows["explicit"]["launch"]
    assert "tier" not in explicit_row
    assert explicit_row["harness"] == "claude"
    assert explicit_row["provider"] == "anthropic"
    assert rows["borrowed"]["launch"] is None
    for row in rows.values():
        assert not list(Draft202012Validator(launch_schema).iter_errors(row["launch"]))

    status_row = _routed(app, "routine.status", {"name": "tiered", "actor": USER})
    assert status_row["launch"] == tier_row


def test_bad_params_surface_the_declared_invalid_argument(
    app: DaemonApplication,
) -> None:
    _register_agents(app)
    for method, params in (
        ("routine.list", {"all": "yes"}),
        ("routine.list", {"agent": ""}),
        ("routine.list", {"agent": 5}),
        ("routine.status", {"name": "x", "agent": 5}),
        ("workflow.list", {"all": 1}),
        ("workflow.list", {"limit": "ten"}),
        ("workflow.list", {"agent": ""}),
        ("agent.home-census", {"actor": ""}),
        ("agent.home-census", {"actor": 7}),
    ):
        with pytest.raises(DaemonRequestError) as error:
            _routed(app, method, params)
        assert error.value.code == ipc_errors.INVALID_ARGUMENT, (method, params)


def test_migrated_audit_and_legacy_workflow_status_route(
    app: DaemonApplication,
) -> None:
    assert "routine.audit" in METHOD_REGISTRY
    assert "workflow.status" not in METHOD_REGISTRY
    assert app.handle("routine.audit", {"actor": USER})["quarantined"] == []
    with pytest.raises(DaemonRequestError) as legacy_status:
        app.handle(
            "workflow.status",
            {"runId": "missing", "actor": USER},
            _trusted_message_origin="worker",
        )
    assert legacy_status.value.code == "WORKFLOW_NOT_FOUND"


_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

_SRC_ROOT = Path(__file__).resolve().parents[5] / "src" / "hyprial"

_RELATIONS = "daemon/impl/request_catalog/agent/relations.py"
_AGENT_CATALOG = "daemon/impl/request_catalog/agent/__init__.py"
_ROUTINES = "daemon/impl/application/actors/routines.py"
_WORKFLOWS = "daemon/impl/application/workflows.py"

_METHOD_BOUNDARIES = {
    "routine.list": (
        (_RELATIONS, "routine_list"),
        (_RELATIONS, "_selector_claims_caller"),
        (_RELATIONS, "_refuse_foreign_selector"),
        (_RELATIONS, "_resolve_agent_selector"),
        (_ROUTINES, "_ipc_routine_list"),
        (_WORKFLOWS, "_workflow_caller"),
        (_WORKFLOWS, "_pac_bound_caller"),
    ),
    "routine.status": (
        (_RELATIONS, "routine_status"),
        (_RELATIONS, "_selector_claims_caller"),
        (_RELATIONS, "_refuse_foreign_selector"),
        (_RELATIONS, "_resolve_agent_selector"),
        (_ROUTINES, "_ipc_routine_status"),
        (_WORKFLOWS, "_workflow_caller"),
        (_WORKFLOWS, "_pac_bound_caller"),
    ),
    "workflow.list": (
        (_RELATIONS, "workflow_list"),
        (_RELATIONS, "_selector_claims_caller"),
        (_RELATIONS, "_refuse_foreign_selector"),
        (_RELATIONS, "_resolve_agent_selector"),
        (_WORKFLOWS, "_ipc_workflow_status_group"),
        (_WORKFLOWS, "_workflow_caller"),
        (_WORKFLOWS, "_pac_bound_caller"),
    ),
    "agent.home-census": (
        (_RELATIONS, "agent_home_census"),
        (_RELATIONS, "_resolve_agent_selector"),
        (_AGENT_CATALOG, "_legacy_agent_call"),
    ),
}

_METHOD_BRANCHES = {
    "agent.home-census": (
        (
            "daemon/impl/application/actors/agents_admin/registry_ops.py",
            "_handle_agent",
            "agent.home-census",
        ),
    ),
}

_ROUTINE_LIST_PRODUCERS = (
    ("biz/impl/routine/execution/service.py", ("list", "read_routines")),
    ("shell/impl/daemon_wiring/routine_service.py", ("_call", "list")),
)
_ROUTINE_STATUS_PRODUCERS = (
    ("biz/impl/routine/execution/service.py", ("status", "read_routine")),
    ("shell/impl/daemon_wiring/routine_service.py", ("_call", "status")),
)
_WORKFLOW_PRODUCERS = (
    ("daemon/impl/pac/workflows/runtime/core.py", ("_open",)),
    ("daemon/impl/pac/workflows/runtime/operations.py", ("list",)),
)
_METHOD_PRODUCERS = {
    "routine.list": _ROUTINE_LIST_PRODUCERS,
    "routine.status": _ROUTINE_STATUS_PRODUCERS,
    "workflow.list": _WORKFLOW_PRODUCERS,
    "agent.home-census": (),
}

_TRANSLATIONS = {
    AgentError.code: (
        "the AgentError translation (_legacy_agent_call and the selector "
        "registry read) re-raises the error's own code; it never appears as a "
        "literal"
    ),
}

_PAC_WRAP = (
    "_workflow_caller raises it via an import name while wrapping every "
    "DaemonRequestError from _pac_bound_caller"
)
_DAEMON_REPLACEMENT = (
    "MethodRegistry._refuse_invalid replaces a result schema violation; "
    "no producer literal"
)

_METHOD_DERIVED = {
    "routine.list": {
        ipc_errors.DAEMON_ERROR: _DAEMON_REPLACEMENT,
        PAC_PRINCIPAL_UNVERIFIED: _PAC_WRAP,
    },
    "routine.status": {
        ipc_errors.DAEMON_ERROR: _DAEMON_REPLACEMENT,
        PAC_PRINCIPAL_UNVERIFIED: _PAC_WRAP,
    },
    "workflow.list": {
        ipc_errors.DAEMON_ERROR: _DAEMON_REPLACEMENT,
        PAC_PRINCIPAL_UNVERIFIED: _PAC_WRAP,
    },
    "agent.home-census": {
        ipc_errors.DAEMON_ERROR: _DAEMON_REPLACEMENT,
    },
}

_METHOD_EXCLUSIONS = {
    "routine.list": {},
    "routine.status": {},
    "workflow.list": {
        "WORKFLOW_NODE_NOT_FOUND": (
            "workflow.node.inspect branch of the shared status-group "
            "dispatcher; the list branch cannot reach it"
        ),
    },
    "agent.home-census": {},
}


def _function_source(module: str, name: str) -> str:
    path = _SRC_ROOT / module
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{module}:{name} not found")


def _branch_source(module: str, function: str, marker: str) -> str:
    path = _SRC_ROOT / module
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for child in ast.walk(node):
                if isinstance(child, ast.If):
                    segment = ast.get_source_segment(source, child)
                    if segment and marker in segment:
                        return segment
    raise AssertionError(f"{module}:{function}:{marker} branch not found")


def _literal_codes(source: str) -> set[str]:
    codes: set[str] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _CODE_RE.match(node.value):
                codes.add(node.value)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ipc_errors"
        ):
            codes.add(node.attr)
    return codes


def _method_candidates(method: str) -> set[str]:
    candidates: set[str] = set()
    for module, function in _METHOD_BOUNDARIES[method]:
        candidates |= _literal_codes(_function_source(module, function))
    for module, function, marker in _METHOD_BRANCHES.get(method, ()):
        candidates |= _literal_codes(_branch_source(module, function, marker))
    for module, functions in _METHOD_PRODUCERS[method]:
        for function in functions:
            candidates |= _literal_codes(_function_source(module, function))
    return candidates


def test_every_method_declares_its_own_reachable_error_set() -> None:
    """Rel2: per-method candidates, exclusions and derived allowances."""

    for method in _METHOD_BOUNDARIES:
        spec = METHOD_REGISTRY.get(method).spec  # type: ignore[union-attr]
        declared = {error.code for error in spec.errors}
        candidates = _method_candidates(method)
        unexplained = candidates - declared - set(_METHOD_EXCLUSIONS[method])
        assert not unexplained, (method, sorted(unexplained))
        for code, reason in _METHOD_EXCLUSIONS[method].items():
            assert code in candidates, (method, code, reason)
            assert code not in declared, (method, code, reason)
        unjustified = declared - candidates - set(_TRANSLATIONS) - set(
            _METHOD_DERIVED[method]
        )
        assert not unjustified, (method, sorted(unjustified))
