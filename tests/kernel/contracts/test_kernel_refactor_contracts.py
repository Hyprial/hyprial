"""Kernel boundaries and behavior preserved by the five-domain refactor."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, is_dataclass
import importlib
import sys
from pathlib import Path

import pytest


def test_kernel_has_no_upper_domain_imports() -> None:
    root = Path(__file__).resolve().parents[3] / 'src/hyprial/kernel'
    edges = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                if name.startswith(
                    ("hyprial.identity", "hyprial.daemon", "hyprial.biz", "hyprial.shell")
                ):
                    edges.append((str(path.relative_to(root)), node.lineno, name))
    assert edges == []


def test_kernel_modules_fit_structural_limits() -> None:
    root = Path(__file__).resolve().parents[3] / 'src/hyprial/kernel'
    for directory in [root, *root.rglob("*")]:
        if not directory.is_dir() or "__pycache__" in directory.parts:
            continue
        files = [path for path in directory.iterdir() if path.is_file()]
        source = [path for path in files if path.suffix == ".py"]
        if source:
            assert len(files) <= 7, (directory, len(files))
        for path in source:
            assert len(path.read_bytes().splitlines()) <= 1000, path


def test_kernel_public_face_is_explicit() -> None:
    kernel = importlib.import_module("hyprial.kernel")
    interface = importlib.import_module("hyprial.kernel.interface")
    assert "__getattr__" not in vars(interface)
    assert "__getattr__" not in vars(kernel)
    assert "ProcessLivenessProbeError" in kernel.__all__
    assert "ManagedHarnessProcess" in kernel.__all__
    for name in kernel.__all__:
        assert getattr(kernel, name) is getattr(interface, name)


def test_home_initialization_preserves_order_and_policy_path(tmp_path, monkeypatch) -> None:
    from hyprial.kernel import initialize_hyprial_home

    selected = tmp_path / "selected"
    monkeypatch.setenv("HYPRIAL_HOME", str(selected))
    observations = []

    def policy(path: Path) -> None:
        observations.append(("policy", path, path.is_dir()))

    assert initialize_hyprial_home(ensure_dispatch_policy=policy) == selected
    assert observations == [("policy", selected, True)]


def test_home_initialization_propagates_effect_failures(tmp_path, monkeypatch) -> None:
    from hyprial.kernel import initialize_hyprial_home

    selected = tmp_path / "selected"
    monkeypatch.setenv("HYPRIAL_HOME", str(selected))
    def policy(_path: Path) -> None:
        raise RuntimeError("policy failed")

    with pytest.raises(RuntimeError, match="policy failed"):
        initialize_hyprial_home(ensure_dispatch_policy=policy)
    assert selected.is_dir()


@pytest.mark.parametrize(
    "harness,provider,model,expected",
    [
        ("pi", "deepseek", "deepseek-flash", ("pi", "-p", "--provider", "deepseek", "--model", "deepseek-flash", "--thinking", "high", "hello")),
        ("claude", None, "sonnet", ("claude", "-p", "--model", "sonnet", "hello")),
        ("codex", None, "gpt-5.6-luna", ("codex", "exec", "--skip-git-repo-check", "--model", "gpt-5.6-luna", "hello")),
    ],
)
def test_candidate_probe_argv_is_pure(harness, provider, model, expected) -> None:
    from hyprial.kernel import Candidate, build_probe_command

    loaded_upper = {name for name in sys.modules if name.startswith("hyprial.daemon")}
    assert Candidate(harness, provider, model).probe_cmd == expected
    assert build_probe_command(harness, model, provider) == expected
    assert {name for name in sys.modules if name.startswith("hyprial.daemon")} == loaded_upper


def test_probe_invalid_inputs_keep_errors() -> None:
    from hyprial.kernel import build_probe_command

    with pytest.raises(ValueError, match="pi probes require an explicit provider"):
        build_probe_command("pi", "model")
    with pytest.raises(ValueError, match="unsupported harness 'unknown'"):
        build_probe_command("unknown", "model")


def test_managed_process_contract_keeps_runtime_check() -> None:
    from hyprial.kernel import ManagedHarnessProcess, ProcessLiveness, ProcessLivenessState

    class Child:
        running = True
        pid = 42

        def liveness(self):
            return ProcessLiveness(ProcessLivenessState.ALIVE, observed=True, pid=self.pid)

        def stop(self):
            self.running = False

    child = Child()
    assert isinstance(child, ManagedHarnessProcess)
    assert child.liveness().pid == 42
    child.stop()
    assert child.running is False


def test_contract_namespace_has_no_wire_execution() -> None:
    contracts = importlib.import_module("hyprial.kernel.impl.contracts")
    assert "evaluate_case" not in contracts.__all__
    assert not hasattr(contracts, "evaluate_case")


def test_lifecycle_vocabulary_keeps_frozen_slotted_dataclasses() -> None:
    from hyprial.kernel import (
        DomainEffectClaim,
        LifecycleMutationCompleted,
        LifecycleMutationRequest,
        MutationProvenance,
    )

    provenance = MutationProvenance(True, False, "resource")
    request = LifecycleMutationRequest("correlation", "attempt", "operation", None, {})
    completed = LifecycleMutationCompleted("correlation", "attempt", 3, 4, "agent", provenance, {})
    claim = DomainEffectClaim("operation", "attempt", False, True, "resource")
    assert completed.replayed is False
    for value in (provenance, request, completed, claim):
        assert is_dataclass(value)
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, next(iter(value.__dataclass_fields__)), "changed")
    with pytest.raises(ValueError, match="resource_token must not be blank"):
        MutationProvenance(False, False, " ")
    with pytest.raises(ValueError, match="operation_id must not be blank"):
        LifecycleMutationRequest("correlation", "attempt", " ", None, {})


def test_native_ports_do_not_load_windows_dependencies_on_other_platforms() -> None:
    from hyprial.kernel import (
        OwnedProcessGroup,
        connect_named_pipe,
        create_owned_process_group,
        is_windows_owned_process_group,
        listen_named_pipe,
        windows_process_identity,
    )

    if sys.platform == "win32":
        pytest.skip("this negative path is specific to non-Windows platforms")
    assert "win32api" not in sys.modules
    process_group = create_owned_process_group(label="kernel test")
    assert type(process_group) is OwnedProcessGroup
    assert process_group._label == "kernel test"
    assert not is_windows_owned_process_group(process_group)
    for call in (
        lambda: connect_named_pipe(Path("unused"), 1.0),
        lambda: listen_named_pipe(Path("unused")),
        lambda: windows_process_identity(42),
    ):
        with pytest.raises(OSError, match="Windows named pipes are unavailable"):
            call()
    assert "win32api" not in sys.modules
