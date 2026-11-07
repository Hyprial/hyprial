"""Regressions for the actual daemon-owned upper-composition seams."""

import ast
from pathlib import Path


APPLICATION = Path(__file__).resolve().parents[3] / 'src/hyprial/daemon/impl/application'


def test_from_environment_declares_the_factories_it_forwards():
    tree = ast.parse((APPLICATION / "__init__.py").read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DaemonApplication")
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "from_environment")
    assert {"quota_evaluator_factory", "quota_hook_consumer_factory"} <= {
        argument.arg for argument in method.args.kwonlyargs
    }


def test_quota_factory_receives_the_actual_daemon_dependencies():
    tree = ast.parse((APPLICATION / "__init__.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "quota_evaluator_factory"]
    assert len(calls) == 1
    assert len(calls[0].args) == 1
    assert isinstance(calls[0].args[0], ast.Name)
    assert calls[0].args[0].id == "quota_deps"


def test_timeout_handlers_precede_their_generic_port_base():
    tree = ast.parse((APPLICATION / "actors/routines.py").read_text())
    pairs = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        names = [handler.type.id for handler in node.handlers if isinstance(handler.type, ast.Name)]
        if "RoutinePortTimeout" in names and "RoutinePortError" in names:
            pairs.append(names)
            assert names.index("RoutinePortTimeout") < names.index("RoutinePortError")
    assert len(pairs) == 4
