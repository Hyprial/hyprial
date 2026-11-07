"""Lark adapter desired-state ownership.

The adapter mutates desired state only through ``LarkDesiredStatePort``.  One
non-Lark lifecycle compensation is reviewed and allowed:
``daemon/impl/application/actors/lifecycle_ops.py:440`` in
``_compensate_replace`` removes the headless intent that the failed switch
just created.  Its only caller is ``_ipc_lifecycle_start``, which rejects
``harness == "lark"`` at line 463 before this path, so a Lark adapter cannot
reach it.  The allowlist is an exact source fragment per file, so any other
dynamic ``harness`` argument is still refused; the reverse-mutation test
proves the dynamic form is caught.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
import textwrap

from hyprial.daemon.impl.adapters.lark.runtime.effects import _ProcessEffects
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.application import DaemonApplication


SOURCE_ROOT = Path(__file__).resolve().parents[5] / 'src/hyprial'
APPLICATION = SOURCE_ROOT / 'daemon/impl/application'


_REVIEWED_NON_LARK_MUTATIONS: dict[str, tuple[str, ...]] = {
    "daemon/impl/application/actors/lifecycle_ops.py": (
        "self.desired_state.remove_harness(spec.harness, spec.name)",
    ),
}


def _reviewed_mutation(relative: str, segment: str) -> bool:
    return segment in _REVIEWED_NON_LARK_MUTATIONS.get(relative, ())


def _application_desired_mutations(
    source: str, *, relative: str = ""
) -> tuple[str, ...]:
    dedented = textwrap.dedent(source)
    tree = ast.parse(dedented)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "desired_state"
            and node.func.attr in {"upsert_harness", "remove_harness"}
        ):
            segment = (ast.get_source_segment(dedented, node) or "").strip()
            if _reviewed_mutation(relative, segment):
                continue
            found.append(node.func.attr)
    return tuple(found)


def test_application_adapter_ipc_has_no_desired_state_second_writer() -> None:
    for path in sorted(APPLICATION.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        assert _application_desired_mutations(source, relative=relative) == ()
    runtime_source = inspect.getsource(DaemonApplication._startup_compose_delivery_plane)
    assert "desired_state=LarkDesiredStatePort(self.desired_state)" in runtime_source


def test_reviewed_non_lark_allowlist_matches_exactly_one_line() -> None:
    for relative, fragments in _REVIEWED_NON_LARK_MUTATIONS.items():
        lines = (SOURCE_ROOT / relative).read_text(encoding="utf-8").splitlines()
        for fragment in fragments:
            assert sum(fragment in line for line in lines) == 1, (relative, fragment)


def test_adapter_effect_owns_persistence_and_process_ordering() -> None:
    source = inspect.getsource(_ProcessEffects._run)
    assert 'operation in {"spawn", "activate-spawn"}' in source
    assert 'operation in {"stop", "deactivate-stop"}' in source
    stop_branch = source.index('operation in {"stop", "deactivate-stop"}')
    stop_call = source.index("process.stop()", stop_branch)
    deactivate = source.index("self._desired_state.deactivate(name)", stop_call)
    assert stop_call < deactivate
    assert "self._desired_state.activate(name)" in source


def test_reverse_mutation_detects_application_second_writer() -> None:
    source = textwrap.dedent(inspect.getsource(DaemonApplication._ipc_adapter_stop))
    mutated = source.replace(
        'event = self._lark_client.stop(name)',
        'self.desired_state.remove_harness("lark", name)\n    '
        'event = self._lark_client.stop(name)',
    )
    assert mutated != source
    assert _application_desired_mutations(mutated) == ("remove_harness",)


def test_dynamic_non_lark_mutation_is_not_silently_ignored() -> None:
    source = textwrap.dedent(inspect.getsource(DaemonApplication._ipc_adapter_stop))
    mutated = source.replace(
        'event = self._lark_client.stop(name)',
        'self.desired_state.remove_harness(spec.harness, spec.name)\n    '
        'event = self._lark_client.stop(name)',
    )
    assert mutated != source
    assert _application_desired_mutations(mutated) == ("remove_harness",)


def test_pykka_remains_private_to_actor_runtime_backend() -> None:
    source_root = SOURCE_ROOT
    importers = tuple(
        sorted(
            str(path.relative_to(source_root))
            for path in source_root.rglob("*.py")
            if "import pykka" in path.read_text(encoding="utf-8")
        )
    )
    assert importers == ("kernel/impl/actor_runtime/backends/pykka.py",)
    assert "pykka" not in inspect.getsource(AdapterRuntime)
