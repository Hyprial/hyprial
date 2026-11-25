"""Native harness Windows branches retain labels and Job-ownership checks."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


HARNESS = Path(__file__).resolve().parents[4] / 'src/hyprial/daemon/impl/harnesses'


def test_process_fact_exports_are_the_single_native_objects():
    from hyprial import kernel
    from hyprial.kernel.impl.manysys.process import owned_process

    for name in ("darwin_group_has_live_members", "linux_group_has_live_members", "parse_linux_process_stat", "process_birth_identity"):
        assert name in kernel.__all__
        assert getattr(kernel, name) is getattr(owned_process, name)


@pytest.mark.parametrize("source,name,label", [
    ("agent_sdk/__init__.py", "_sdk_process_group", "Claude SDK"),
    ("codex/process.py", "_managed_process_group", "Codex app-server"),
])
def test_windows_constructor_branches_use_the_canonical_port(source, name, label):
    tree = ast.parse((HARNESS / source).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and (".impl.platform" in node.module or ".impl.manysys" in node.module)
        for node in ast.walk(function)
    )
    marker = object()
    calls = []

    def construct(*, label):
        calls.append(label)
        return marker

    namespace = {"os": SimpleNamespace(name="nt"), "create_owned_process_group": construct, "OwnedProcessGroup": object}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(HARNESS / source), "exec"), namespace)
    assert namespace[name]() is marker
    assert calls == [label]


def test_codex_windows_launch_checks_the_native_ownership_predicate():
    tree = ast.parse((HARNESS / "codex/process.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_spawn_managed_codex")
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "is_windows_owned_process_group" for node in ast.walk(function))
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "name"
        for node in ast.walk(function)
    )
    checked = []
    spawned = []

    class _JobGroup:
        async def spawn(self, command, **options):
            spawned.append(("job", tuple(command), options))
            return "job-proc"

    async def fake_exec(*args, **kwargs):
        spawned.append(("exec", args, kwargs))
        return "exec-proc"

    job_group = _JobGroup()
    other_group = object()
    namespace = {
        "asyncio": SimpleNamespace(create_subprocess_exec=fake_exec),
        "spawn_kwargs": lambda: {"creationflags": 512},
        "is_windows_owned_process_group": lambda group: checked.append(group) or (group is job_group),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(HARNESS / "codex/process.py"), "exec"), namespace)
    assert asyncio.run(namespace[function.name](["codex"], job_group)) == "job-proc"
    assert asyncio.run(namespace[function.name](["codex"], other_group)) == "exec-proc"
    assert checked == [job_group, other_group]
    assert spawned == [
        ("job", ("codex",), {}),
        ("exec", ("codex",), {"creationflags": 512}),
    ]


def test_sdk_windows_launch_checks_the_native_ownership_predicate():
    tree = ast.parse((HARNESS / "agent_sdk/__init__.py").read_text())
    checks = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "is_windows_owned_process_group"]
    assert len(checks) == 1
    assert ast.unparse(checks[0].args[0]) == "self._process_group"
