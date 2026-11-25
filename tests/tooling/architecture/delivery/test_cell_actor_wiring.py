"""The cell-actor wiring gate: pure, isolated, wired only through I5-2 seams.

What this gate holds, and how it proves it can see red:

- ``cells/actor`` is a pure interpreter island: its modules may import the
  cell model, the codec, the pure structure reader, their own package and
  the kernel actor vocabulary, plus a fixed stdlib slice — and nothing
  else.  No subprocess/socket/sqlite/queue/http import can pass, no
  ``time.sleep``/``open``/``subprocess`` call name passes, and no class
  name encodes a configuration strategy (a router, a judge or a buffer —
  member names, labels and routes are opaque data).
- Only the reviewed RUNTIME consumer set may import ``cells.actor``.  I5-1
  wired the composition wiring alone ("no delivery path is wired" as a
  gate, not a promise); I5-2 wired the headless delivery seams down to four
  runtime consumers (the composition wiring, the envelope builder, the
  harness port client edge and the actor's delivery behaviour); I5-5 added
  the fifth reviewed runtime consumer (``cell_observation/cell_states.py``
  for snapshot run-state translation).  A ``TYPE_CHECKING`` import is an
  annotation, not a runtime consumer, and is ignored; the same closed set
  is pinned by ``test_cell_entry_gate.py``, and a new runtime consumer
  widens both lists in one reviewed diff.
- The runtime keeps its lookup discipline: ``cells/runtime/service.py``
  still imports no delivery domain (it gained only the health vocabulary).

The scanner is pure over a directory so the mutation proofs run on
``tmp_path`` samples (a stdlib import outside the slice, a product domain
import, a blocking call, a strategy class name, a delivery module importing
the registry), never by mutating the repository.

Decision notes for I5-1:

- Relative imports within ``cells/actor`` are package-local and are accepted by
  the gate. A literal string's ``join`` is data formatting and is not a
  blocking call.
- Dynamic ``getattr`` calls, method aliases, and ``operator.methodcaller`` are
- Dynamic ``getattr`` calls, method aliases, ``operator.methodcaller`` and
  dynamically composed cell group names are known scanner limitations. This
  gate does not expand its AST model for them.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ACTOR_DIR = REPO / "src/hyprial/daemon/impl/cells/actor"
SRC = REPO / "src"

ALLOWED_ACTOR_IMPORTS = frozenset(
    {
        "hyprial.daemon.impl.cells.actor",
        "hyprial.daemon.impl.cells.actor.contracts",
        "hyprial.daemon.impl.cells.actor.actor",
        "hyprial.daemon.impl.cells.actor.registry",
        "hyprial.daemon.impl.cells.actor.snapshot",
        "hyprial.daemon.impl.cells.model",
        "hyprial.daemon.impl.cells.codec",
        "hyprial.daemon.impl.cells.structure",
        "hyprial.daemon.impl.cells.ports",
        "hyprial.kernel",
    }
)

STDLIB_SLICE = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "enum",
        "hashlib",
        "threading",
        "time",
        "types",
        "typing",
        "uuid",
    }
)

ALLOWED_ACTOR_CONSUMERS = frozenset(
    {
        "src/hyprial/daemon/impl/composition/cells/wiring.py",
        "src/hyprial/daemon/impl/composition/cell_delivery/headless.py",
        "src/hyprial/daemon/impl/composition/cell_observation/cell_states.py",
        "src/hyprial/daemon/impl/composition/harness_ports.py",
        "src/hyprial/daemon/impl/harnesses/actor/behaviors/delivery/gate.py",
    }
)

RUNTIME_FILE = "src/hyprial/daemon/impl/cells/runtime/service.py"
FORBIDDEN_RUNTIME_IMPORT_PARTS = (
    "hyprial.daemon.impl.dispatch",
    "hyprial.daemon.impl.harnesses",
    "hyprial.daemon.impl.inbox",
    "hyprial.daemon.impl.links",
    "hyprial.daemon.impl.threads",
)

FORBIDDEN_QUALIFIED_CALLS = frozenset(
    {
        "time.sleep",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "urllib.request.urlopen",
        "sqlite3.connect",
    }
)

FORBIDDEN_BARE_CALLS = frozenset({"open", "exec", "eval", "input", "compile"})

STRATEGY_NAME_PARTS = ("router", "judge", "buffer")
FORBIDDEN_BLOCKING_METHODS = frozenset({"wait", "acquire", "join", "sleep"})


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str
    detail: str


def _import_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    prefix = node.module or ""
    if node.level:
        prefix = "." * node.level + prefix
    return [prefix] if prefix else []


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value)
        return f"{owner}.{node.attr}" if owner is not None else node.attr
    return None


def scan_actor_dir(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not path.is_dir():
        return [Finding(str(path), "missing-dir", "cells/actor does not exist")]
    for file in sorted(path.glob("*.py")):
        module = file.name
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue
                for name in _import_modules(node):
                    stripped = name.lstrip(".")
                    if stripped in ALLOWED_ACTOR_IMPORTS or stripped in STDLIB_SLICE:
                        continue
                    findings.append(Finding(module, "actor-import", stripped))
            elif isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Constant)
                ):
                    continue
                name = _call_name(node.func)
                if name is None:
                    continue
                if name in FORBIDDEN_QUALIFIED_CALLS or (
                    "." not in name and name in FORBIDDEN_BARE_CALLS
                ):
                    findings.append(Finding(module, "blocking-or-io-call", name))
                elif name.rsplit(".", 1)[-1] in FORBIDDEN_BLOCKING_METHODS:
                    findings.append(Finding(module, "blocking-or-io-call", name))
            elif isinstance(node, ast.ClassDef):
                lowered = node.name.lower()
                for part in STRATEGY_NAME_PARTS:
                    if part in lowered:
                        findings.append(Finding(module, "strategy-class-name", node.name))
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


def scan_consumers(src_root: Path) -> list[Finding]:
    prefix = "hyprial.daemon.impl.cells.actor"
    findings: list[Finding] = []
    for file in sorted(src_root.rglob("*.py")):
        relative = file.relative_to(src_root).as_posix()
        if relative.startswith("hyprial/daemon/impl/cells/actor/"):
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        guarded = _type_checking_lines(tree)
        hit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom) and node.lineno not in guarded:
                for name in _import_modules(node):
                    stripped = name.lstrip(".")
                    if stripped == prefix or stripped.startswith(prefix + "."):
                        hit = True
                        break
            if hit:
                break
        if hit and f"src/{relative}" not in ALLOWED_ACTOR_CONSUMERS:
            findings.append(Finding(relative, "unwired-consumer", prefix))
    return findings


def scan_runtime_imports(file: Path) -> list[Finding]:
    findings: list[Finding] = []
    tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for name in _import_modules(node):
                stripped = name.lstrip(".")
                for part in FORBIDDEN_RUNTIME_IMPORT_PARTS:
                    if stripped == part or stripped.startswith(part + "."):
                        findings.append(Finding(RUNTIME_FILE, "runtime-import", stripped))
    return findings


def scan_cell_key_mutations(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for file in sorted(src_root.rglob("*.py")):
        relative = file.relative_to(src_root).as_posix()
        if relative.startswith("hyprial/daemon/impl/cells/actor/"):
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                module = node.module.lstrip(".")
                if module.startswith("hyprial.daemon.impl.cells.actor") and any(
                    alias.name in {"CELL_ACTOR_GROUP", "_CELL_ACTOR_GROUP"}
                    for alias in node.names
                ):
                    findings.append(Finding(relative, "cell-key-mutation", "group-import"))
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name is not None and name.rsplit(".", 1)[-1] == "ActorSpec":
                groups = [keyword.value.value for keyword in node.keywords
                          if keyword.arg == "group" and isinstance(keyword.value, ast.Constant)]
                if "cell" in groups:
                    findings.append(Finding(relative, "cell-key-mutation", "spec"))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "find":
                groups = [node.args[0].value] if node.args and isinstance(node.args[0], ast.Constant) else []
                groups.extend(keyword.value.value for keyword in node.keywords
                              if keyword.arg == "group" and isinstance(keyword.value, ast.Constant))
                if "cell" in groups:
                    findings.append(Finding(relative, "cell-key-mutation", "find"))
    return findings


def test_the_actor_island_imports_only_its_declared_dependencies() -> None:
    findings = scan_actor_dir(ACTOR_DIR)
    assert findings == [], [f"{f.kind}: {f.path} -> {f.detail}" for f in findings]


def test_no_delivery_module_imports_the_cell_actor() -> None:
    findings = scan_consumers(SRC)
    assert findings == [], [f"{f.kind}: {f.path}" for f in findings]


def test_the_runtime_service_still_imports_no_delivery_domain() -> None:
    findings = scan_runtime_imports(REPO / RUNTIME_FILE)
    assert findings == [], [f"{f.kind}: {f.path} -> {f.detail}" for f in findings]


def test_only_the_cell_actor_registry_mutates_the_cell_key_group() -> None:
    findings = scan_cell_key_mutations(SRC)
    assert findings == [], [f"{f.kind}: {f.path} -> {f.detail}" for f in findings]


def test_the_scanner_sees_an_import_outside_the_stdlib_slice(tmp_path: Path) -> None:
    (tmp_path / "contracts.py").write_text("import queue\n", encoding="utf-8")
    findings = scan_actor_dir(tmp_path)
    assert any(f.kind == "actor-import" and f.detail == "queue" for f in findings)


def test_the_scanner_sees_a_product_domain_import(tmp_path: Path) -> None:
    (tmp_path / "actor.py").write_text(
        "from hyprial.daemon.impl.inbox import something\n", encoding="utf-8"
    )
    findings = scan_actor_dir(tmp_path)
    assert any(f.kind == "actor-import" and "inbox" in f.detail for f in findings)


def test_the_scanner_sees_a_blocking_call(tmp_path: Path) -> None:
    (tmp_path / "actor.py").write_text(
        "import time\n\n\ndef f() -> None:\n    time.sleep(1)\n", encoding="utf-8"
    )
    findings = scan_actor_dir(tmp_path)
    assert any(f.kind == "blocking-or-io-call" and f.detail == "time.sleep" for f in findings)


def test_the_scanner_sees_blocking_method_call_shapes(tmp_path: Path) -> None:
    samples = {
        "wait.py": "import threading\nthreading.Event().wait(1)\n",
        "acquire.py": "lock.acquire()\n",
        "join.py": "thread.join()\n",
        "sleep.py": "clock.sleep(1)\n",
    }
    for name, source in samples.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    findings = scan_actor_dir(tmp_path)
    assert {
        finding.detail.rsplit(".", 1)[-1]
        for finding in findings
        if finding.kind == "blocking-or-io-call"
    } >= {"wait", "acquire", "join", "sleep"}


def test_the_scanner_allows_string_join_and_actor_relative_import(tmp_path: Path) -> None:
    (tmp_path / "actor.py").write_text(
        'from .contracts import CellActorWakeup\nvalue = ",".join(parts)\n',
        encoding="utf-8",
    )
    assert scan_actor_dir(tmp_path) == []


def test_the_scanner_sees_a_strategy_class_name(tmp_path: Path) -> None:
    (tmp_path / "actor.py").write_text("class CellRouter:\n    pass\n", encoding="utf-8")
    findings = scan_actor_dir(tmp_path)
    assert any(
        f.kind == "strategy-class-name" and f.detail == "CellRouter" for f in findings
    )


def test_the_scanner_sees_an_unwired_consumer(tmp_path: Path) -> None:
    pkg = tmp_path / "hyprial/daemon/impl/dispatch"
    pkg.mkdir(parents=True)
    (pkg / "runtime.py").write_text(
        "from hyprial.daemon.impl.cells.actor import CellActorRegistry\n",
        encoding="utf-8",
    )
    findings = scan_consumers(tmp_path)
    assert any(f.kind == "unwired-consumer" for f in findings)


def test_the_scanner_allows_the_wiring_consumer(tmp_path: Path) -> None:
    pkg = tmp_path / "hyprial/daemon/impl/composition/cells"
    pkg.mkdir(parents=True)
    (pkg / "wiring.py").write_text(
        "from hyprial.daemon.impl.cells.actor import CellActorRegistry\n",
        encoding="utf-8",
    )
    findings = scan_consumers(tmp_path)
    assert findings == []


def test_the_scanner_sees_a_runtime_delivery_import(tmp_path: Path) -> None:
    sample = tmp_path / "service.py"
    sample.write_text(
        "from hyprial.daemon.impl.harnesses.actor import anything\n", encoding="utf-8"
    )
    findings = scan_runtime_imports(sample)
    assert any(f.kind == "runtime-import" for f in findings)


def test_the_scanner_sees_a_cell_key_spec_or_find(tmp_path: Path) -> None:
    sample = tmp_path / "other.py"
    sample.write_text(
        "spec = ActorSpec(group='cell', key='c1')\nhost.spawn(spec)\nhost.find('cell', 'c1')\n",
        encoding="utf-8",
    )
    findings = scan_cell_key_mutations(tmp_path)
    assert {finding.detail for finding in findings} == {"spec", "find"}


def test_the_scanner_rejects_a_cell_group_constant_import(tmp_path: Path) -> None:
    sample = tmp_path / "other.py"
    sample.write_text(
        "from hyprial.daemon.impl.cells.actor.registry import _CELL_ACTOR_GROUP\n",
        encoding="utf-8",
    )
    findings = scan_cell_key_mutations(tmp_path)
    assert any(finding.detail == "group-import" for finding in findings)
