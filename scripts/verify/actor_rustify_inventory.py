#!/usr/bin/env python3
"""actor-rustify P1 machine inventory: engine holding sites and process roles.

Scans ``src/hyprial`` with AST and records every site that constructs or
references the actor engine surface: ``ActorRuntime(``, ``ActorSpec(``,
``EffectLane(``, ``GenerationScheduler(``, ``undelivered_sink``,
runtime-level ``event_sink=`` and ``ExpectedActorError``.  Engine internals
(``kernel/impl/actor_runtime/**``) are the implementation under inspection,
not holders, and are excluded.

Process attribution is a static import closure per process entry chain,
including function-level imports (a branch that only executes rarely still
loads its modules when it runs).  Roots:

- ``daemon``: ``hyprial daemon run`` entry chain (``shell.cli.commands.daemon``,
  ``shell.impl.daemon_wiring``, ``daemon.impl.application``) -- the supervised
  long-running process.
- ``mcp``: ``hyprial machine mcp`` entry (``daemon.impl.mcp.entry``).
- ``worker``: harness child processes with their own ``main()``
  (lark worker entry, python worker, exec env socket, agent sdk worker).
- ``cli``: ``hyprial.shell.impl.cli.runtime.entry`` (one-shot command
  processes other than the daemon/mcp/worker entries).

A module may list several roles; the label set is the honest static answer.
The engine ABI version recorded here is ``ENGINE_ABI_VERSION=1``.

Round 2 (review R2/R5, 2026-11-01): every ``EffectLane`` /
``GenerationScheduler`` site additionally records the unparsed constructor
keyword arguments, the receiver the construction is assigned to, and the
watched lifecycle methods invoked on that receiver in the same file
(``submit`` vs ``reserve``/``submit_reserved`` classification evidence,
scheduler ``schedule``/``cancel``/``shutdown`` usage).  Receiver matching is
name-based (``self._x`` / local variable) and can miss wrappers that store
lanes in collections; those gaps stay with the manual per-site assessment in
the evidence note and are never attributed to this script.  Constructions
through the subscripted-generic form ``EffectLane[...](`` are recorded as
kind ``effect_lane_generic`` -- both the round-1 grep baseline and the
round-1 AST visitor missed that form, which hid two real sites in
``identity/impl/agents/actor/_core.py``.

Output is deterministic: identical input tree produces byte-identical JSON.
The default output path is
``docs/notes/actor-rustify-p1-2026-11-01/inventory.json``.

Decisions (P1 brief, 2026-11-01): engine internals excluded from holding
sites; ``DaemonActorRuntime`` (the same-named PAC protocol adapter) is not an
engine holder; roles are static import closures, over-approximating lazy
branches by design; no network, no HOME, no daemon access -- pure file scan.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ENGINE_ABI_VERSION = 1
SCHEMA = "actor-rustify-inventory-v2"

ENGINE_PACKAGE = "hyprial.kernel.impl.actor_runtime"
DEFAULT_OUTPUT = Path("docs/notes/actor-rustify-p1-2026-11-01/inventory.json")

ROLE_ORDER = ("cli", "daemon", "worker", "mcp")

WATCHED_IMPORTS = frozenset({"ExpectedActorError"})

WATCHED_METHODS = frozenset(
    {
        "submit",
        "reserve",
        "submit_reserved",
        "cancel_reservation",
        "acknowledge",
        "close",
        "snapshot",
        "schedule",
        "cancel",
        "shutdown",
        "drain",
    }
)


PROCESS_ROOTS: dict[str, tuple[str, ...]] = {
    "cli": ("hyprial.shell.impl.cli.runtime.entry",),
    "daemon": (
        "hyprial.shell.impl.cli.commands.daemon",
        "hyprial.shell.impl.daemon_wiring",
        "hyprial.daemon.impl.application",
    ),
    "worker": (
        "hyprial.daemon.impl.adapters.worker.entry",
        "hyprial.daemon.impl.adapters.lark.worker.entry",
        "hyprial.daemon.impl.harnesses.python_worker._python_worker",
        "hyprial.daemon.impl.harnesses.agent_sdk._agent_sdk_worker",
    ),
    "mcp": ("hyprial.daemon.impl.mcp.entry",),
}

SITE_KINDS = (
    "actor_runtime",
    "actor_spec",
    "effect_lane",
    "effect_lane_generic",
    "effect_lane_variant",
    "generation_scheduler",
    "undelivered_sink",
    "event_sink_runtime",
    "expected_actor_error",
)


def _module_name_for(relative: Path) -> str | None:
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _is_package(module: str, modules: set[str]) -> bool:
    return module in modules


def _resolve_imports(
    tree: ast.AST, module: str, modules: set[str], is_package: bool = False
) -> set[str]:
    executed: set[str] = set()

    def add(dotted: str) -> None:
        parts = dotted.split(".")
        for index in range(1, len(parts) + 1):
            executed.add(".".join(parts[:index]))

    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                keep = len(package_parts) - (node.level - 1)
                keep = max(0, keep)
                if node.module:
                    base = ".".join(package_parts[:keep]) + "." + node.module
                else:
                    base = ".".join(package_parts[:keep])
            base = base.strip(".")
            if base:
                add(base)
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in modules:
                    add(candidate)
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == "_FACADE_EXPORTS"
                for target in node.targets
            ) and isinstance(node.value, ast.Dict):
                for value in node.value.values:
                    if (
                        isinstance(value, ast.Tuple)
                        and value.elts
                        and isinstance(value.elts[0], ast.Constant)
                        and isinstance(value.elts[0].value, str)
                    ):
                        add(value.elts[0].value)
    return executed


def build_import_closures(root: Path) -> dict[str, set[str]]:
    package_root = root / "src" / "hyprial"
    module_of: dict[Path, str] = {}
    modules: set[str] = set()
    packages: set[str] = set()
    trees: dict[str, ast.AST] = {}
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root)
        name = _module_name_for(relative)
        if name is None:
            continue
        dotted = f"hyprial.{name}" if name else "hyprial"
        module_of[path] = dotted
        modules.add(dotted)
        if relative.name == "__init__.py":
            packages.add(dotted)
        trees[dotted] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    edges: dict[str, set[str]] = {}
    for dotted, tree in trees.items():
        edges[dotted] = _resolve_imports(
            tree, dotted, modules, is_package=dotted in packages
        )

    closures: dict[str, set[str]] = {}
    for role in ("daemon", "worker", "mcp"):
        closures[role] = _closure(role, PROCESS_ROOTS[role], modules, edges)
    claimed = closures["daemon"] | closures["worker"] | closures["mcp"]
    closures["cli"] = _closure(
        "cli", PROCESS_ROOTS["cli"], modules, edges, exclude=claimed
    )
    return closures


def _closure(
    role: str,
    roots: tuple[str, ...],
    modules: set[str],
    edges: dict[str, set[str]],
    exclude: set[str] | None = None,
) -> set[str]:
    excluded = exclude or set()
    seen: set[str] = set()
    queue = [name for name in roots if name in modules]
    missing = [name for name in roots if name not in modules]
    if missing:
        raise SystemExit(
            f"process root module(s) not found for role {role!r}: {missing}"
        )
    while queue:
        current = queue.pop()
        if current in seen or current in excluded:
            continue
        seen.add(current)
        for target in sorted(edges.get(current, ())):
            if target in modules and target not in seen and target not in excluded:
                queue.append(target)
    return seen


@dataclass(frozen=True, slots=True)
class Site:
    kind: str
    path: str
    line: int
    col: int
    owner_class: str | None
    owner_function: str | None
    roles: tuple[str, ...]
    kwargs: dict[str, str] | None = None
    receiver: str | None = None
    usage: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "col": self.col,
            "owner_class": self.owner_class,
            "owner_function": self.owner_function,
            "roles": list(self.roles),
        }
        if self.kwargs is not None:
            payload["kwargs"] = dict(self.kwargs)
            payload["receiver"] = self.receiver
            payload["usage"] = list(self.usage)
        return payload


def _receiver_usage(tree: ast.AST) -> dict[str, set[str]]:
    usage: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in WATCHED_METHODS:
            continue
        receiver = ast.unparse(node.func.value)
        usage.setdefault(receiver, set()).add(node.func.attr)
    return usage


def _kwargs_of(node: ast.Call) -> dict[str, str]:
    return {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in node.keywords
        if keyword.arg is not None
    }


class SiteCollector(ast.NodeVisitor):
    def __init__(
        self,
        relative: str,
        roles: tuple[str, ...],
        usage: dict[str, set[str]] | None = None,
    ) -> None:
        self._relative = relative
        self._roles = roles
        self._usage = usage or {}
        self._class_stack: list[str] = []
        self._function_stack: list[str] = []
        self._pending_receiver: str | None = None
        self.sites: list[Site] = []

    def _visit_assign_target(self, target: ast.expr) -> str | None:
        if isinstance(target, (ast.Name, ast.Attribute)):
            return ast.unparse(target)
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        receiver: str | None = None
        if len(node.targets) == 1:
            receiver = self._visit_assign_target(node.targets[0])
        self._with_pending_receiver(receiver, node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._with_pending_receiver(self._visit_assign_target(node.target), node)

    def _with_pending_receiver(self, receiver: str | None, node: ast.AST) -> None:
        previous = self._pending_receiver
        self._pending_receiver = receiver
        self.generic_visit(node)
        self._pending_receiver = previous

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_stack.append(node.name)
        self.generic_visit(node)
        self._function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _record(self, kind: str, node: ast.AST) -> None:
        self.sites.append(
            Site(
                kind=kind,
                path=self._relative,
                line=node.lineno,
                col=node.col_offset,
                owner_class=self._class_stack[-1] if self._class_stack else None,
                owner_function=(
                    self._function_stack[-1] if self._function_stack else None
                ),
                roles=self._roles,
            )
        )

    def _record_effect_site(self, kind: str, node: ast.Call) -> None:
        receiver = self._pending_receiver
        self._pending_receiver = None
        self.sites.append(
            Site(
                kind=kind,
                path=self._relative,
                line=node.lineno,
                col=node.col_offset,
                owner_class=self._class_stack[-1] if self._class_stack else None,
                owner_function=(
                    self._function_stack[-1] if self._function_stack else None
                ),
                roles=self._roles,
                kwargs=_kwargs_of(node),
                receiver=receiver,
                usage=tuple(sorted(self._usage.get(receiver or "", ()))),
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name == "ActorRuntime":
            self._record("actor_runtime", node)
            for keyword in node.keywords:
                if keyword.arg == "event_sink":
                    self._record("event_sink_runtime", keyword)
        elif name == "ActorSpec":
            self._record("actor_spec", node)
            for keyword in node.keywords:
                if keyword.arg == "undelivered_sink" and not _is_none(keyword.value):
                    self._record("undelivered_sink", keyword)
        elif name == "EffectLane":
            kind = (
                "effect_lane_generic"
                if isinstance(node.func, ast.Subscript)
                else "effect_lane"
            )
            self._record_effect_site(kind, node)
        elif name is not None and name.endswith("EffectLane"):
            self._record_effect_site("effect_lane_variant", node)
        elif name == "GenerationScheduler":
            self._record_effect_site("generation_scheduler", node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in WATCHED_IMPORTS:
                self._record("expected_actor_error", node)
                break
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "undelivered_sink":
            self._record("undelivered_sink", node)
        elif node.id == "ExpectedActorError":
            self._record("expected_actor_error", node)
        self.generic_visit(node)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _call_name(node.value)
    return None


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def scan(root: Path) -> dict[str, object]:
    package_root = root / "src" / "hyprial"
    if not package_root.is_dir():
        raise SystemExit(f"product root not found: {package_root}")
    closures = build_import_closures(root)

    sites: list[Site] = []
    scanned = 0
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative_hyprial = path.relative_to(package_root)
        dotted = _module_name_for(relative_hyprial)
        if dotted is None:
            continue
        full_dotted = f"hyprial.{dotted}" if dotted else "hyprial"
        if full_dotted.startswith(ENGINE_PACKAGE + ".") or full_dotted == ENGINE_PACKAGE:
            continue
        scanned += 1
        relative_repo = (Path("src") / "hyprial" / relative_hyprial).as_posix()
        roles = tuple(role for role in ROLE_ORDER if full_dotted in closures[role])
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = SiteCollector(relative_repo, roles, _receiver_usage(tree))
        collector.visit(tree)
        sites.extend(collector.sites)

    sites.sort(key=lambda s: (s.kind, s.path, s.line, s.col, s.owner_class or ""))
    counts: dict[str, int] = {kind: 0 for kind in SITE_KINDS}
    for site in sites:
        counts[site.kind] += 1
    counts_by_file: dict[str, dict[str, int]] = {}
    for site in sites:
        per_file = counts_by_file.setdefault(site.path, {kind: 0 for kind in SITE_KINDS})
        per_file[site.kind] += 1

    return {
        "schema": SCHEMA,
        "engine_abi_version": ENGINE_ABI_VERSION,
        "source_root": "src/hyprial",
        "engine_package": ENGINE_PACKAGE,
        "modules_scanned": scanned,
        "counts": counts,
        "files_with_sites": len(counts_by_file),
        "per_file": {
            path: {kind: n for kind, n in sorted(per.items()) if n}
            for path, per in sorted(counts_by_file.items())
        },
        "role_modules": {
            role: len(closures[role])
            for role in ROLE_ORDER
        },
        "sites": [site.as_dict() for site in sites],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (default: script's repo)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print the JSON to stdout instead of writing the default file",
    )
    arguments = parser.parse_args(argv)

    report = scan(arguments.root.resolve())
    payload = json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    if arguments.stdout:
        sys.stdout.write(payload)
        return 0
    output = arguments.output or (arguments.root / DEFAULT_OUTPUT)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    print(f"wrote {output} ({len(report['sites'])} sites)")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
