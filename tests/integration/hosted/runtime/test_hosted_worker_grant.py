"""AT03's worker grant: the scenario's send-to authority is exact and sufficient.

task49555/run14488 regression: the hosted-upgrade run failed inside the AT03
journey because the invited worker held no send-to grant, so its harness_send
to the driver was refused (TARGET_NOT_AUTHORIZED: "a hosted visitor without a
grant sees only itself"), the loopback mock ended the turn with its terminal
assistant text, and the daemon settled the pending message with that text --
the nonce never reached the driver. The scenario now grants send-to scoped to
exactly the driver principal. These tests pin that authority from both sides:
the scenario source grants nothing beyond isolation plus this one principal,
and the product decision layer accepts exactly the granted principal with the
exact scope expression the scenario writes.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from hyprial.identity import CallerIdentity, GrantRecord, may_send

SCENARIO = Path(__file__).resolve().parents[4] / "contract" / "e2e" / "run-hosted-start.py"
WORKER = "agent:visitor@example.test:host-node:worker"
DRIVER = "agent:host-owner:host-node:driver"


def _worker_grant_calls() -> list[tuple[str, ast.expr]]:
    """The (capability, scope-expression) pairs of the worker grant CLI calls."""
    tree = ast.parse(SCENARIO.read_text(encoding="utf-8"))
    grants = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cli" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"):
            continue
        args = node.args
        if [a.value for a in args[:3]] != ["agent", "grant", "worker"]:
            continue
        flags = {args[i].value: args[i + 1] for i in range(3, len(args) - 1, 2)}
        grants.append((flags["--capability"].value, flags["--scope"]))
    return grants


def _scenario_names() -> dict[str, str]:
    """Every module-level string binding the scenario defines (its own URIs)."""
    tree = ast.parse(SCENARIO.read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            names[node.targets[0].id] = node.value.value
    return names


def test_scenario_grants_exactly_isolation_and_driver_scoped_send_to() -> None:
    grants = _worker_grant_calls()
    # Exactly two grants, in the scenario's own order: the L0 isolation record
    # with its fixed scope, then the send-to scope named from the scenario's
    # own driver URI -- nothing wider, nothing else.
    assert [cap for cap, _ in grants] == ["isolation", "send-to"]
    assert isinstance(grants[0][1], ast.Constant) and grants[0][1].value == "directory"
    scope = grants[1][1]
    assert isinstance(scope, ast.Call)
    namespace = {**_scenario_names(), "json": json}
    assert eval(compile(ast.Expression(scope), str(SCENARIO), "eval"), {"__builtins__": {}}, namespace) \
        == json.dumps([DRIVER])


def test_granted_scope_satisfies_the_send_to_decision_exactly() -> None:
    caller = CallerIdentity(uri=WORKER, hosted=True, verified=True)
    grants = [GrantRecord(actor=WORKER, capability="send-to", scope=json.dumps([DRIVER]))]
    allowed = may_send(DRIVER, caller=caller, grants=grants)
    assert allowed.allowed, allowed.reason
    stranger = "agent:stranger@example.test:other-node:watcher"
    refused = may_send(stranger, caller=caller, grants=grants)
    assert not refused.allowed, refused.reason
    # Hosted callers get the generic refusal; the target list, not the reason,
    # is what stays minimal.
    assert refused.reason == "no send-to grant: a hosted visitor without a grant sees only itself"


def test_without_the_grant_the_decision_reproduces_the_run14488_refusal() -> None:
    caller = CallerIdentity(uri=WORKER, hosted=True, verified=True)
    refused = may_send(DRIVER, caller=caller, grants=[])
    assert not refused.allowed
    assert refused.reason == "no send-to grant: a hosted visitor without a grant sees only itself"
