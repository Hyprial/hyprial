"""S3/R1+R2 regressions: the selector filters after admission; the router narrows.

Decision record (implS3, 2026-11-02 — codex-daemon-request REWORK of 394effc0bf,
reviewer evidence 647c820f76, base #2270 @ 95ed848f0e)
- R1: on 394effc0bf the three caller-entangled selector paths skipped the
  ``_workflow_caller`` fence entirely: a synthetic ``actor`` with a
  non-existent ``sessionRef`` plus ``agent=<target>`` read the target's
  routine rows and workflow runs that the base refuses with
  PAC_PRINCIPAL_UNVERIFIED{cause: CALLER_NOT_AUTHORIZED}.  The fence order is
  admission first, selector second: a claims-carrying request must pass the
  real caller fence before any selector work; a verified non-operator caller
  may only select itself (a foreign target is CALLER_NOT_AUTHORIZED); the
  claim-free request keeps the accepted ``agent.secret.list`` operator
  posture; the ``user:<owner>`` positive control still answers.
- These tests run the real DaemonApplication with the real RoutineService,
  GraphWorkflowService and session fence; the live binding is minted through
  the real ``session.register`` path, never a mock, mirroring the reviewer's
  isolated-socket evidence through the same ``handle`` entry the AF_UNIX
  server dispatches to (real-socket harnesses are environment-BLOCKED on this
  machine, see docs/notes/gui-agents-relations-methods-2026-11-02.md).
- R2: the legacy router branches for the registry-migrated names
  ``routine.list``/``routine.status``/``workflow.list`` are dead dispatch;
  the source assertions pin the narrow cleanup and the surviving
  un-migrated siblings.
"""

from __future__ import annotations

import ast
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hyprial.biz import RoutineService
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.identity import PAC_PRINCIPAL_UNVERIFIED
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

OTHER_ROUTINE_YAML = ROUTINE_YAML.replace(
    "name: heartbeat", f"name: other-delegated\nactor: {OTHER_URI}"
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

SELECTOR_CASES = (
    ("routine.list", {"agent": AGENT}),
    ("routine.status", {"name": "delegated", "agent": AGENT}),
    ("workflow.list", {"agent": AGENT}),
)


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


class _Unused:
    pass


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
    with tempfile.TemporaryDirectory(prefix="hyprial-s3-") as directory:
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


def _register_live_session(app: DaemonApplication, actor: str, ref: str) -> None:
    app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": ref,
            "cwd": "/work",
            "command": ["test"],
            "source": "channel-test",
            "runtime": "pi_interactive",
            "channelConfirmed": True,
        },
    )


def _base_refusal(
    app: DaemonApplication, method: str, claims: dict[str, Any]
) -> DaemonRequestError:
    """The no-selector legacy refusal for the same caller claims."""

    try:
        if method == "routine.list":
            app._ipc_routine_list(dict(claims), app._workflow_caller(dict(claims)))
        elif method == "workflow.list":
            app._ipc_workflow_status_group("workflow.list", dict(claims))
        else:
            from hyprial.kernel import canonical_user_uri

            routine_caller = app._workflow_caller(dict(claims))
            current = app._routine_service.status(name="delegated")
            if routine_caller not in (
                canonical_user_uri(app.owner),
                current["owner"],
                current.get("actor"),
            ):
                raise DaemonRequestError(
                    ipc_errors.CALLER_NOT_AUTHORIZED,
                    "caller does not own this routine",
                )
            app._ipc_routine_status(dict(claims))
    except DaemonRequestError as error:
        return error
    raise AssertionError(f"{method} base path answered without a refusal")


def test_selector_with_invalid_session_is_refused_exactly_as_the_base(
    app: DaemonApplication,
) -> None:
    """R1 red/green: the hole returned data the base refuses; both must agree."""

    app.agents.create(AGENT)
    app.agents.create(OTHER)
    app._routine_service.add(yaml_text=AGENT_ROUTINE_YAML, owner=USER, enabled=True)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="s3-hole-1"
    )
    claims = {
        "actor": f"agent:{OWNER}:{NODE}:synthetic",
        "sessionRef": "no-such-session",
    }
    for method, target in SELECTOR_CASES:
        with pytest.raises(DaemonRequestError) as selector:
            _routed(app, method, {**claims, **target})
        base = _base_refusal(app, method, claims)
        assert selector.value.code == base.code == PAC_PRINCIPAL_UNVERIFIED
        assert selector.value.data == base.data == {
            "cause": ipc_errors.CALLER_NOT_AUTHORIZED
        }
        assert str(selector.value) == str(base)


def test_bound_caller_selecting_a_foreign_agent_is_refused(
    app: DaemonApplication,
) -> None:
    """R1 regression: a verified caller must not widen its view via the selector."""

    app.agents.create(AGENT)
    app.agents.create(OTHER)
    app._routine_service.add(yaml_text=AGENT_ROUTINE_YAML, owner=USER, enabled=True)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="s3-hole-2"
    )
    _register_live_session(app, OTHER, "session-s3-2")
    caller = app._workflow_caller({"actor": OTHER, "sessionRef": "session-s3-2"})
    # The verified caller is the agent's official address (its a.<id> URI
    # under id-form minting), never a URI minted from the name.
    assert caller == app.agents.uri_for(OTHER)
    for method, target in SELECTOR_CASES:
        params = {"actor": OTHER, "sessionRef": "session-s3-2", **target}
        with pytest.raises(DaemonRequestError) as refused:
            _routed(app, method, params)
        assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED, (
            method,
            str(refused.value),
        )
        declared = {
            item.code
            for item in METHOD_REGISTRY.get(method).spec.errors  # type: ignore[union-attr]
        }
        assert refused.value.code in declared, method


def test_bound_caller_selecting_itself_answers_exactly_its_own_rows(
    app: DaemonApplication,
) -> None:
    """Self-select never widens: it equals the caller-scoped base view."""

    app.agents.create(AGENT)
    app.agents.create(OTHER)
    app._routine_service.add(yaml_text=OTHER_ROUTINE_YAML, owner=USER, enabled=True)
    app._workflow_service.start(
        yaml_text=OTHER_WORKFLOW_YAML, sender=USER, operation_key="s3-self-1"
    )
    _register_live_session(app, OTHER, "session-s3-3")
    claims = {"actor": OTHER, "sessionRef": "session-s3-3"}
    routed = _routed(app, "routine.list", {**claims, "agent": OTHER})
    base = app._ipc_routine_list(dict(claims), app._workflow_caller(dict(claims)))
    assert [row["name"] for row in routed["routines"]] == ["other-delegated"]
    assert routed == base
    status = _routed(
        app, "routine.status", {**claims, "name": "other-delegated", "agent": OTHER}
    )
    assert status["actor"] == OTHER_URI
    runs = _routed(app, "workflow.list", {**claims, "agent": OTHER})["runs"]
    assert len(runs) == 1


def test_claim_free_selector_keeps_the_accepted_operator_posture(
    app: DaemonApplication,
) -> None:
    """The GUI path presents no caller claims; the selector stays a filter."""

    app.agents.create(AGENT)
    app._routine_service.add(yaml_text=AGENT_ROUTINE_YAML, owner=USER, enabled=True)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="s3-operator-1"
    )
    routed = _routed(app, "routine.list", {"agent": AGENT})
    assert [row["name"] for row in routed["routines"]] == ["delegated"]
    status = _routed(app, "routine.status", {"name": "delegated", "agent": AGENT})
    assert status["actor"] == AGENT_URI
    runs = _routed(app, "workflow.list", {"agent": AGENT})["runs"]
    assert len(runs) == 1


def test_operator_positive_control_still_answers_with_a_selector(
    app: DaemonApplication,
) -> None:
    """user:<owner> on the trusted boundary keeps its business filter."""

    app.agents.create(AGENT)
    app._routine_service.add(yaml_text=AGENT_ROUTINE_YAML, owner=USER, enabled=True)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="s3-operator-2"
    )
    routed = _routed(app, "routine.list", {"actor": USER, "agent": AGENT})
    assert [row["name"] for row in routed["routines"]] == ["delegated"]
    status = _routed(
        app, "routine.status", {"name": "delegated", "actor": USER, "agent": AGENT}
    )
    assert status["actor"] == AGENT_URI
    runs = _routed(app, "workflow.list", {"actor": USER, "agent": AGENT})["runs"]
    assert len(runs) == 1


_ROUTER = (
    Path(__file__).resolve().parents[6]
    / "src" / "hyprial" / "daemon" / "impl" / "ipc" / "router.py"
)


def _handle_string_constants() -> set[str]:
    source = _ROUTER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "handle":
            names: set[str] = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    names.add(child.value)
            return names
    raise AssertionError("handle() not found in router.py")


def test_legacy_router_branches_drop_the_migrated_names() -> None:
    """R2: dead legacy dispatch for the migrated names is gone, siblings stay."""

    names = _handle_string_constants()
    for migrated in ("routine.list", "routine.status", "routine.audit", "routine.pause", "routine.resume", "workflow.list", "workflow.history.list"):
        assert migrated not in names, migrated
    for sibling in (
        "routine.add",
        "routine.remove",
        "routine.set",
        "workflow.status",
        "workflow.node.inspect",
        "workflow.cancel",
        "workflow.history.status",
    ):
        assert sibling in names, sibling


# --- I2 (UUID addressing merge into 0.5.5.4): id-form selectors ---------------
#
# With id-form minting the agent's official address is its ``a.`` id URI,
# routines minted at ``routine.add`` before the record existed keep the name
# URI, and the coordinator / workflow writers store the id URI.  Every
# selector spelling of the one agent must answer the same rows, and a bound
# caller must reach its own rows by either spelling (D4 read alias).


def _id_form_rows(app: DaemonApplication) -> Any:
    agent = app.agents.create(AGENT)
    assert agent.uri != AGENT_URI, "the fixture must run with id-form minting"
    app.agents.create(OTHER)
    id_bound = ROUTINE_YAML.replace(
        "name: heartbeat", f"name: id-bound\nactor: {agent.uri}"
    )
    app._routine_service.add(yaml_text=id_bound, owner=USER, enabled=True)
    app._routine_service.add(yaml_text=AGENT_ROUTINE_YAML, owner=USER, enabled=True)
    app._routine_service.add(yaml_text=OTHER_ROUTINE_YAML, owner=USER, enabled=True)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML.replace(AGENT_URI, agent.uri),
        sender=USER,
        operation_key="i2-id-owner",
    )
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="i2-name-owner"
    )
    app._workflow_service.start(
        yaml_text=OTHER_WORKFLOW_YAML, sender=USER, operation_key="i2-other"
    )
    return agent


def _spellings(agent: Any) -> tuple[str, ...]:
    return (AGENT, AGENT_URI, agent.uri, agent.agent_id)


def test_every_selector_spelling_answers_the_same_id_form_rows(
    app: DaemonApplication,
) -> None:
    agent = _id_form_rows(app)
    app.agents.rename(AGENT, "reviewer")
    for selector in (*_spellings(agent), "reviewer"):
        rows = _routed(app, "routine.list", {"agent": selector})["routines"]
        assert sorted(row["name"] for row in rows) == ["delegated", "id-bound"], selector
        for name in ("delegated", "id-bound"):
            status = _routed(app, "routine.status", {"name": name, "agent": selector})
            assert status["name"] == name
        with pytest.raises(DaemonRequestError) as foreign:
            _routed(
                app, "routine.status", {"name": "other-delegated", "agent": selector}
            )
        assert foreign.value.code == "ROUTINE_NOT_FOUND", selector
        runs = _routed(app, "workflow.list", {"agent": selector})["runs"]
        assert len(runs) == 2, selector
        newest = _routed(app, "workflow.list", {"agent": selector, "limit": 1})["runs"]
        assert newest == runs[:1], selector
        census = _routed(app, "agent.home-census", {"actor": selector})
        assert [row["actor"] for row in census["agents"]] == [AGENT], selector
        assert census["totals"]["agents"] == 1


def test_a_bound_caller_selects_itself_by_any_spelling(
    app: DaemonApplication,
) -> None:
    agent = _id_form_rows(app)
    _register_live_session(app, AGENT, "session-i2-1")
    claims = {"actor": AGENT, "sessionRef": "session-i2-1"}
    assert app._workflow_caller(dict(claims)) == agent.uri
    for selector in _spellings(agent):
        rows = _routed(app, "routine.list", {**claims, "agent": selector})["routines"]
        assert sorted(row["name"] for row in rows) == ["delegated", "id-bound"]
        status = _routed(
            app, "routine.status", {**claims, "name": "delegated", "agent": selector}
        )
        assert status["actor"] == AGENT_URI
        assert len(_routed(app, "workflow.list", {**claims, "agent": selector})["runs"]) == 2
    with pytest.raises(DaemonRequestError) as refused:
        _routed(app, "routine.list", {**claims, "agent": OTHER})
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_routine_status_gate_accepts_the_frozen_name_alias_of_the_caller(
    app: DaemonApplication,
) -> None:
    """The router's routine.* fence (_routine_caller_is), not a bare ``in``."""

    agent = _id_form_rows(app)
    _register_live_session(app, AGENT, "session-i2-2")
    claims = {"actor": AGENT, "sessionRef": "session-i2-2"}
    for name in ("delegated", "id-bound"):
        status = _routed(app, "routine.status", {**claims, "name": name})
        assert status["name"] == name
    listed = _routed(app, "routine.list", dict(claims))["routines"]
    assert sorted(row["name"] for row in listed) == ["delegated", "id-bound"]
    with pytest.raises(DaemonRequestError) as refused:
        _routed(app, "routine.status", {**claims, "name": "other-delegated"})
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert agent.uri != AGENT_URI
