"""The cell entry gate of I5-2 parts B/C: envelopes are the only work key.

What this gate holds, and how it proves it can see red:

- Production ``CellDispatchEnvelope``/``CellDispatchStamp`` construction is a
  closed set: only ``composition/cell_delivery`` mints them.  Any other src
  module constructing one is an unreviewed second builder.
- ``cells.actor`` has the five reviewed RUNTIME consumers: the four I5-2
  delivery seams (the composition wiring, the envelope builder, the harness
  port client edge and the actor's delivery behaviour) plus I5-5's cell
  run-state observation translator (``cell_observation/cell_states.py``).
  Type-annotation-only imports under ``TYPE_CHECKING`` are not runtime
  consumers and are ignored; a new runtime consumer must widen this
  reviewed list, not slip in.
- The harness execution edge accepts exactly one envelope: the port client,
  the facade and the supervisor carry no worker-name or bare-delivery
  parameter on ``dispatch``/``steer``, and the port client rejects a
  non-envelope payload before submitting.
- The dispatch/steer commands carry the envelope and nothing else, and the
  actor behavior selects the member from ``stamp.member`` — never from a
  caller-supplied name — feeding ``process.enqueue(envelope.input)``.
- Every ``DaemonEventBridge`` construction in src passes the required
  ``cell_delivery`` keyword, so the production bridge never offers a bare
  delivery through a no-port seam.

The scanners are pure over a source root so every mutation proof runs on
``tmp_path`` samples, never by mutating the repository.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "src"

CELL_ACTOR_PREFIX = "hyprial.daemon.impl.cells.actor"

ENVELOPE_BUILDERS = frozenset(
    {
        "src/hyprial/daemon/impl/composition/cell_delivery/__init__.py",
        "src/hyprial/daemon/impl/composition/cell_delivery/headless.py",
        "src/hyprial/daemon/impl/composition/cell_delivery/port.py",
    }
)

CELL_ACTOR_CONSUMERS = frozenset(
    {
        "src/hyprial/daemon/impl/composition/cells/wiring.py",
        "src/hyprial/daemon/impl/composition/cell_delivery/headless.py",
        "src/hyprial/daemon/impl/composition/cell_observation/cell_states.py",
        "src/hyprial/daemon/impl/composition/harness_ports.py",
        "src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery/gate.py",
    }
)

EXECUTION_EDGES = (
    "src/hyprial/daemon/impl/composition/harness_ports.py",
    "src/hyprial/daemon/impl/harnesses/actor/facade.py",
    "src/hyprial/daemon/impl/processes/supervisor.py",
)

DISPATCH_COMMAND_FILE = "src/hyprial/daemon/impl/harnesses/runtime/ports.py"
DELIVERY_BEHAVIOR_FILE = "src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery/gate.py"
BRIDGE_WIRING_FILE = "src/hyprial/daemon/impl/application/wiring/transport.py"

MINTED = ("CellDispatchEnvelope", "CellDispatchStamp")


def _import_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    prefix = node.module or ""
    if node.level:
        prefix = "." * node.level + prefix
    return [prefix] if prefix else []


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def scan_envelope_mints(src_root: Path) -> list[str]:
    findings: list[str] = []
    for file in sorted(src_root.rglob("*.py")):
        relative = file.relative_to(src_root.parent).as_posix()
        tree = _tree(file)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name in MINTED and relative not in ENVELOPE_BUILDERS:
                findings.append(f"{relative}: {name}")
    return findings


def _type_checking_lines(tree: ast.Module) -> set[int]:
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = test.id if isinstance(test, ast.Name) else None
        attribute = (
            test.attr if isinstance(test, ast.Attribute) and isinstance(test.value, ast.Name)
            else None
        )
        if name != "TYPE_CHECKING" and attribute != "TYPE_CHECKING":
            continue
        for child in ast.walk(node):
            if child is not node:
                guarded.add(getattr(child, "lineno", -1))
    return guarded


def scan_cell_actor_consumers(src_root: Path) -> list[str]:
    findings: list[str] = []
    for file in sorted(src_root.rglob("*.py")):
        relative = file.relative_to(src_root.parent).as_posix()
        if relative.startswith("src/hyprial/daemon/impl/cells/actor/"):
            continue
        tree = _tree(file)
        guarded = _type_checking_lines(tree)
        hit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom) and node.lineno not in guarded:
                for name in _import_modules(node):
                    stripped = name.lstrip(".")
                    if stripped == CELL_ACTOR_PREFIX or stripped.startswith(
                        CELL_ACTOR_PREFIX + "."
                    ):
                        hit = True
                        break
            if hit:
                break
        if hit and relative not in CELL_ACTOR_CONSUMERS:
            findings.append(relative)
    return findings


def _method(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    return None


def scan_execution_edges(src_root: Path) -> list[str]:
    findings: list[str] = []
    for relative in EXECUTION_EDGES:
        file = src_root.parent / relative
        if not file.exists():
            continue
        tree = _tree(file)
        for method_name in ("dispatch", "steer"):
            method = _method(tree, method_name)
            if method is None:
                findings.append(f"{relative}: missing {method_name}")
                continue
            parameters = {
                argument.arg
                for argument in method.args.args + method.args.kwonlyargs
            }
            forbidden = parameters & {"name", "worker", "delivery", "delivery_id"}
            if forbidden:
                findings.append(
                    f"{relative}:{method_name}: bare parameters {sorted(forbidden)}"
                )
            if "envelope" not in parameters:
                findings.append(f"{relative}:{method_name}: no envelope parameter")
    return findings


def scan_port_client_validation(src_root: Path) -> list[str]:
    file = src_root.parent / "src/hyprial/daemon/impl/composition/harness_ports.py"
    source = file.read_text(encoding="utf-8")
    if "HARNESS_DELIVERY_REQUIRES_ENVELOPE" not in source:
        return ["harness_ports.py: no envelope rejection at the edge"]
    return []


def scan_command_payloads(src_root: Path) -> list[str]:
    file = src_root.parent / DISPATCH_COMMAND_FILE
    tree = _tree(file)
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name not in {"DispatchHarnessDeliveryCommand", "SteerHarnessDeliveryCommand"}:
            continue
        fields = {
            statement.target.id
            for statement in node.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
        }
        if "envelope" not in fields:
            findings.append(f"{node.name}: no envelope field")
        for bare in fields & {"name", "delivery", "delivery_id"}:
            findings.append(f"{node.name}: bare field {bare}")
    return findings


def _function_source(tree: ast.Module, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return ast.unparse(item)
    return ""


def scan_delivery_behavior(src_root: Path) -> list[str]:
    file = src_root.parent / DELIVERY_BEHAVIOR_FILE
    tree = _tree(file)
    findings: list[str] = []
    for name in ("_on_dispatch", "_on_steer"):
        source = _function_source(tree, name)
        if not source:
            findings.append(f"delivery.py: {name} is missing")
            continue
        if "stamp.member" not in source:
            findings.append(f"delivery.py: {name} does not read the stamp member")
        if "command.name" in source:
            findings.append(f"delivery.py: {name} still reads a caller name")
    dispatch = _function_source(tree, "_on_dispatch")
    if "process.enqueue(envelope.input)" not in dispatch:
        findings.append("delivery.py: the process does not receive envelope.input")
    return findings


def scan_bridge_wiring(src_root: Path) -> list[str]:
    findings: list[str] = []
    for file in sorted(src_root.rglob("*.py")):
        relative = file.relative_to(src_root.parent).as_posix()
        tree = _tree(file)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "DaemonEventBridge":
                continue
            if not any(keyword.arg == "cell_delivery" for keyword in node.keywords):
                findings.append(f"{relative}: a DaemonEventBridge without cell_delivery")
    composition_file = src_root.parent / BRIDGE_WIRING_FILE
    if composition_file.exists():
        composition = composition_file.read_text(encoding="utf-8")
        if "compose_headless_cell_delivery" not in composition:
            findings.append("transport.py: the composition builder is not used")
    return findings


def test_envelopes_are_minted_only_by_the_composition_builder() -> None:
    assert scan_envelope_mints(SRC) == []

def test_the_cell_actor_consumer_set_is_closed() -> None:
    assert scan_cell_actor_consumers(SRC) == []

def test_the_execution_edges_accept_exactly_one_envelope() -> None:
    assert scan_execution_edges(SRC) == []

def test_the_port_client_rejects_bare_payloads() -> None:
    assert scan_port_client_validation(SRC) == []

def test_the_dispatch_and_steer_commands_carry_only_envelopes() -> None:
    assert scan_command_payloads(SRC) == []

def test_the_actor_selects_the_member_from_the_stamp() -> None:
    assert scan_delivery_behavior(SRC) == []

def test_the_production_bridge_is_always_wired_with_the_cell_port() -> None:
    assert scan_bridge_wiring(SRC) == []


def test_the_scanner_sees_a_mint_outside_the_builder(tmp_path: Path) -> None:
    package = tmp_path / "src/hyprial/daemon/impl/runtime"
    package.mkdir(parents=True)
    (package / "reconcile.py").write_text(
        "from hyprial.daemon.impl.cells.actor.contracts import CellDispatchEnvelope\n"
        "value = CellDispatchEnvelope(stamp=None)\n",
        encoding="utf-8",
    )
    assert scan_envelope_mints(tmp_path / "src") != []
    assert scan_cell_actor_consumers(tmp_path / "src") != []


def test_the_scanner_sees_a_direct_dispatch_mutation(tmp_path: Path) -> None:
    package = tmp_path / "src/hyprial/daemon/impl/processes"
    package.mkdir(parents=True)
    (package / "supervisor.py").write_text(
        "class ManagedHarnessRuntime:\n"
        "    def dispatch(self, name, delivery) -> bool:\n"
        "        return self._facade.dispatch(name, delivery)\n",
        encoding="utf-8",
    )
    assert scan_execution_edges(tmp_path / "src") != []


def test_the_scanner_sees_a_bare_steer_parameter_mutation(tmp_path: Path) -> None:
    package = tmp_path / "src/hyprial/daemon/impl/composition"
    package.mkdir(parents=True)
    (package / "harness_ports.py").write_text(
        "class HarnessPortClient:\n"
        "    def steer(self, name, delivery_id, timeout=5.0) -> bool:\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert scan_execution_edges(tmp_path / "src") != []


def test_the_scanner_sees_a_command_payload_mutation(tmp_path: Path) -> None:
    package = tmp_path / "src/hyprial/daemon/impl/harnesses/runtime"
    package.mkdir(parents=True)
    (package / "ports.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\nclass DispatchHarnessDeliveryCommand:\n"
        "    correlation_id: str\n"
        "    name: str\n"
        "    delivery: object\n",
        encoding="utf-8",
    )
    assert scan_command_payloads(tmp_path / "src") != []


def test_the_scanner_sees_a_name_keyed_selection_mutation(tmp_path: Path) -> None:
    package = tmp_path / "src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery"
    package.mkdir(parents=True)
    (package / "gate.py").write_text(
        "class _DeliveryBehavior:\n"
        "    def _on_dispatch(self, command) -> None:\n"
        "        selected = record.spec.name == command.name\n"
        "        process.enqueue(command.delivery)\n",
        encoding="utf-8",
    )
    assert scan_delivery_behavior(tmp_path / "src") != []


def test_the_scanner_sees_an_unwired_bridge_mutation(tmp_path: Path) -> None:
    package = tmp_path / "src/hyprial/daemon/impl/application/wiring"
    package.mkdir(parents=True)
    (package / "transport.py").write_text(
        "runtime = DaemonEventBridge(harnesses=port)\n",
        encoding="utf-8",
    )
    assert scan_bridge_wiring(tmp_path / "src") != []


def test_the_scanner_accepts_a_wired_bridge_and_ignores_type_checking(tmp_path: Path) -> None:
    package = tmp_path / "src/hyprial/daemon/impl/application/wiring"
    package.mkdir(parents=True)
    (package / "transport.py").write_text(
        "runtime = DaemonEventBridge(\n"
        "    harnesses=port,\n"
        "    cell_delivery=compose_headless_cell_delivery(app),\n"
        ")\n",
        encoding="utf-8",
    )
    other = tmp_path / "src/hyprial/daemon/impl/runtime"
    other.mkdir(parents=True)
    (other / "settlement.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from hyprial.daemon.impl.cells.actor.contracts import Stamp\n",
        encoding="utf-8",
    )
    assert scan_bridge_wiring(tmp_path / "src") == []
    assert scan_cell_actor_consumers(tmp_path / "src") == []
