from __future__ import annotations

import ast
import inspect
from textwrap import dedent

from hyprial.biz.impl.routine.execution.service import RoutineFacade
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.runtime.bridge import DaemonEventBridge


def _methods(target: type) -> set[str]:
    methods = set()
    for owner in target.__mro__:
        if owner is object:
            continue
        tree = ast.parse(dedent(inspect.getsource(owner)))
        definition = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        methods.update(
            node.name
            for node in definition.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    return methods


def test_domain_cadence_is_exposed_only_as_typed_timer_facades() -> None:
    workflow = _methods(GraphWorkflowService)
    routine = _methods(RoutineFacade)
    bridge = _methods(DaemonEventBridge)

    assert "submit_timer" in workflow
    assert "submit_timer" in routine
    assert "on_timer" in bridge
    assert "tick" not in workflow
    assert "tick" not in routine
    assert "poll" not in bridge
