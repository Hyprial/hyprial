# Decision record (file header; no inline comments by repository rule):
# - The compiled actor ABI is a private extension inside the kernel
#   implementation, not a public face. Exactly one adapter module may import
#   it; every other source hits `native-module-import`. The allowance is by
#   literal module name because the extension has no Python source file for the
#   normal internal-module resolution.
# - `intra/manysys-leaf` pins `hyprial.kernel.impl.manysys` as a pure OS/stdlib
#   leaf package: `kernel_non_manysys(root)` merges the static sibling baseline
#   with dynamic discovery of every direct subpackage/module under
#   `src/hyprial/kernel/impl` (excluding `manysys` and `__init__`) for each
#   scanned root. Skipping ancestor expansion of `"hyprial.kernel"` when
#   `parent_only=True` in `Audit.intra_walk` is strictly scoped to
#   `rule.rule_id == "intra/manysys-leaf"` so all other intra-domain rules
#   retain identical facade-traversal behavior.
"""Read-only, standard-library five-domain architecture gate (Python 3.12+).

No product imports. Resolve AST imports, explicit public providers and narrowly
audited lazy facades. Unknown reflective calls are review leads, never a PASS.
"""
from __future__ import annotations

import argparse
import ast
from collections import deque
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tokenize

DOMAINS = ("kernel", "identity", "daemon", "biz", "shell")
RANK = {name: index for index, name in enumerate(DOMAINS)}
OUTER_ENTRIES = {"cli.py", "gui_build_hook.py"}
SHARED_PACKAGES = ("hyprial.error",)
PUBLIC_FACES = {f"hyprial.{domain}{suffix}" for domain in DOMAINS for suffix in ("", ".interface")}

NATIVE_MODULE_IMPORTERS = {"hyprial._core.actors": frozenset({"hyprial.kernel.impl.actor_runtime.backends.native"})}

# `src/hyprial/skills/**` is DATA, not a sixth domain.  It holds skill payloads -- SKILL.md
# plus, for some skills, linters and build tools -- that ship inside the wheel;
# `ste100/SKILL.md` documents its linter by a path RELATIVE to the skill directory, so the
# payload has to stay in the package and cannot be moved out.  The five-domain rule is
# derived from the path, so it rejects any `.py` there by construction.
#
# THE FILES ARE STILL SCANNED.  They are parsed, registered and checked like every other
# source -- parse errors, symlinks and duplicate modules are still reported.  ONLY THE DOMAIN
# JUDGEMENT IS SKIPPED.  Pruning the subtree instead would carry away the checks that decide
# whether the payload can be packaged at all, and would force the imports below onto a weaker
# resolver: a relaxed path must not be a weaker path.
#
# INVARIANT, ENFORCED IN BOTH DIRECTIONS: `src/hyprial/skills/**` is data -- product code
# must NOT import it, and it must NOT import product code.  The failures are
# `data-area-import` and `data-area-imports-product-code`.
#
# `scripts/ci/check_structural_limits.py` is deliberately NOT relaxed: it measures file and
# line budgets, not domains.  Pinned by `test_structural_limits_still_governs_the_data_area`.
DATA_AREAS = ("src/hyprial/skills",)


def in_data_area(path: str) -> bool:
    """True when a repo-relative path is inside a data area, compared BY PATH SEGMENT.

    Segment-wise on purpose: a string prefix would also swallow a sibling directory such as
    `src/hyprial/skills-extra/`.
    """

    parts = Path(path).parts
    return any(parts[: len(Path(area).parts)] == Path(area).parts for area in DATA_AREAS)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def internal(module: str) -> bool:
    return module == "hyprial" or module.startswith("hyprial.")


def module_name(relative: str) -> str:
    parts = relative.removeprefix("src/").removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)

# The MODULE heads of the data areas, derived ONCE through `module_name()` -- the same helper
# the file rule uses -- so the `src/` prefix and the `pkg/__init__` shape cannot drift apart
# from it.  Deriving them any other way is how `src.hyprial.skills` gets compared against
# `hyprial.skills`.  This has to sit below `module_name()`'s definition; computing it above
# raises NameError at import, which is how the first version of this patch failed.
DATA_AREA_MODULE_HEADS = tuple(module_name(f"{area}/__init__.py") for area in DATA_AREAS)


def module_in_data_area(module: str | None) -> bool:
    """True when a MODULE name belongs to a data area.

    Note that `hyprial` itself is NOT a data-area module, so a data-area script importing the
    package root is a direction-2 failure.  That is DELIBERATE: the invariant is "the payload
    must not import product code", and the package root is product code.
    """

    if not module:
        return False
    return any(module == head or module.startswith(head + ".") for head in DATA_AREA_MODULE_HEADS)


def under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def ancestors(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:index]) for index in range(1, len(parts))]


@dataclass(frozen=True)
class IntraRule:
    """One row of INTRA_DOMAIN_RULES: modules under ``sources`` must not load ``forbidden``.

    Decisions (cell model phase A, slice T, B1/B2):

    - The five-domain rule says nothing inside a domain, so the daemon's isolated
      layers are pinned here.  network and delivery never load orgfs, cells or
      conversations; threads never loads conversations, orgfs or cells except the
      pure ``cells.params``; conversations never loads the inbox engine,
      application, network, composition, links or threads.
    - A row is enforced in two ways.  ``Audit.edge`` rejects any literal import of
      a forbidden module from a source module, wherever the import sits
      (module level, function body or ``TYPE_CHECKING``).  ``Audit.intra_closure``
      then walks what the import really loads: every package ``__init__`` on the
      way, every module-level import of the reached modules, and the provider of a
      symbol taken from a lazy public face.  Imports inside functions and under
      ``TYPE_CHECKING`` deeper in the chain are not followed, because they do not
      run at import time and would make every module reach the whole daemon.
    - An allowance lifts one row for one source subtree and one target subtree.
      The lift carries through imports that stay inside the same forbidden
      package (``cells.params`` may load ``cells.model``), but not through a
      package ``__init__`` reached only as a parent, and never to another
      forbidden package.
    - Violations carry the rule id in ``rule`` and print one line per violation on
      stderr so a CI log names the broken row without opening the JSON.

    Decisions (cell model phase B, S9a + S10 adjustment):

    - inbox never loads threads (and never orgfs, cells or conversations), and
      user-delivery likewise never loads orgfs, cells, conversations or threads.
      network and delivery never load orgfs, cells or conversations; under S10
      they may load threads (per the S9 spec that names only those three), so
      threads is not in their row.  ``application.messaging.delivery`` never
      loads orgfs, cells or conversations.  It may load ``delivery`` and
      ``threads`` (the send gate), so those are simply not in its row.
    - ``application.messaging.delivery`` is dirty on the real tree: every module
      under it loads the ``application`` package ``__init__`` (the daemon
      composition root) and ``lark`` imports the ``composition`` hub, both of
      which reach orgfs and cells at import time.  DIRTY_DELIVERY records each
      measured (module, first hop, forbidden package) so the row still catches
      every new path.  An entry excuses one source module, only through the
      named witness (the first module of the chain outside the row's own
      sources), only for the listed packages, and never a literal import.  The walk keeps one visit set per first hop for such a
      module, so a second path through another hop is still reported.
    - DIRTY_DELIVERY only shrinks: an entry that no longer excuses a path is the
      violation ``stale-intra-dirty-allowance`` and must be deleted, and
      tests/tooling/architecture/boundaries/test_intra_domain_rules.py pins the
      recorded set as an upper bound, so adding an entry is a reviewed diff.
      Staleness is judged only on a tree that has the row's source package, so
      a synthetic tree without it reports nothing, while a deleted module of
      the real package still leaves its entry stale.
    """

    rule_id: str
    sources: tuple[str, ...]
    forbidden: tuple[str, ...]


@dataclass(frozen=True)
class IntraAllowance:
    """A sanctioned edge from ``source`` modules into the ``target`` subtree."""

    source: str
    target: str


@dataclass(frozen=True)
class IntraDirty:
    """A measured, shrink-only exemption: ``source`` may reach ``forbidden`` only through ``witness``."""

    rule_id: str
    source: str
    witness: str
    forbidden: tuple[str, ...]
    reason: str


DAEMON_IMPL = "hyprial.daemon.impl"
KERNEL_IMPL = "hyprial.kernel.impl"
DEFAULT_ROOT = Path(__file__).resolve().parents[2]
STATIC_KERNEL_SIBLINGS = (
    "actor_runtime", "configuration", "contracts", "facts", "hooks",
    "ipc", "log", "observation", "primitives", "processes",
)


def kernel_non_manysys(root: Path = DEFAULT_ROOT) -> tuple[str, ...]:
    names = set(STATIC_KERNEL_SIBLINGS)
    impl_dir = root / "src/hyprial/kernel/impl"
    if impl_dir.is_dir():
        for entry in impl_dir.iterdir():
            name = entry.name if entry.is_dir() else entry.stem if entry.suffix in {".py", ".pyi"} else ""
            if name and not name.startswith(".") and name not in {"__pycache__", "__init__", "manysys"}:
                names.add(name)
    return (
        "hyprial.kernel.interface", *(f"{KERNEL_IMPL}.{name}" for name in sorted(names)),
        *SHARED_PACKAGES, *(f"hyprial.{domain}" for domain in DOMAINS if domain != "kernel"),
    )


KERNEL_NON_MANYSYS = kernel_non_manysys()
ISOLATED_FROM_CELLS = tuple(f"{DAEMON_IMPL}.{name}" for name in ("orgfs", "cells", "conversations"))
ISOLATED_FROM_THREADS = (*ISOLATED_FROM_CELLS, f"{DAEMON_IMPL}.threads")
MESSAGING_DELIVERY = f"{DAEMON_IMPL}.application.messaging.delivery"
INTRA_DOMAIN_RULES = (
    IntraRule("intra/manysys-leaf", (f"{KERNEL_IMPL}.manysys",), KERNEL_NON_MANYSYS),
    IntraRule("intra/network-isolation", (f"{DAEMON_IMPL}.network",), ISOLATED_FROM_CELLS),
    IntraRule("intra/delivery-isolation", (f"{DAEMON_IMPL}.delivery",), ISOLATED_FROM_CELLS),
    IntraRule("intra/inbox-isolation", (f"{DAEMON_IMPL}.inbox",), ISOLATED_FROM_THREADS),
    IntraRule("intra/messaging-delivery-isolation", (MESSAGING_DELIVERY,), ISOLATED_FROM_CELLS),
    IntraRule("intra/user-delivery-isolation", (f"{DAEMON_IMPL}.adapters.user_delivery",),
              (*ISOLATED_FROM_CELLS, f"{DAEMON_IMPL}.threads")),
    IntraRule("intra/threads-isolation", (f"{DAEMON_IMPL}.threads",),
              tuple(f"{DAEMON_IMPL}.{name}" for name in ("conversations", "orgfs", "cells"))),
    IntraRule("intra/conversations-isolation", (f"{DAEMON_IMPL}.conversations",),
              tuple(f"{DAEMON_IMPL}.{name}" for name in ("inbox", "application", "network", "composition", "links", "threads"))),
)
INTRA_DOMAIN_ALLOWANCES: tuple[IntraAllowance, ...] = ()


_MESSAGING_RULE = "intra/messaging-delivery-isolation"
_ORGFS_AND_CELLS = (f"{DAEMON_IMPL}.orgfs", f"{DAEMON_IMPL}.cells")
_APP_ROOT = "application __init__ (daemon composition root) loads orgfs.runtime and, via sessions.turns, cells.observability"
DIRTY_DELIVERY: tuple[IntraDirty, ...] = (
    *(IntraDirty(_MESSAGING_RULE, f"{MESSAGING_DELIVERY}{suffix}", f"{DAEMON_IMPL}.application", _ORGFS_AND_CELLS, _APP_ROOT)
      for suffix in ("", ".lark", ".sends", ".status_queries", ".wake")),
    IntraDirty(_MESSAGING_RULE, f"{MESSAGING_DELIVERY}.lark", f"{DAEMON_IMPL}.composition", (f"{DAEMON_IMPL}.cells",),
               "composition hub: composition.domains loads cells.observability.policy"),
    IntraDirty(_MESSAGING_RULE, f"{MESSAGING_DELIVERY}.sends", f"{DAEMON_IMPL}.composition", (f"{DAEMON_IMPL}.cells",),
               "composition hub: composition.domains loads cells.observability.policy"),
    IntraDirty(_MESSAGING_RULE, f"{MESSAGING_DELIVERY}.status_queries", f"{DAEMON_IMPL}.application.actors.agents_admin",
               _ORGFS_AND_CELLS, "agents_admin reaches the application composition root"),
    IntraDirty(_MESSAGING_RULE, f"{MESSAGING_DELIVERY}.status_queries", f"{DAEMON_IMPL}.application.actors.agents_admin.registry_ops",
               _ORGFS_AND_CELLS, "registry_ops reaches the application composition root and the composition hub"),
)


def dirty_for(rule: IntraRule, origin: str) -> list[IntraDirty]:
    return [entry for entry in DIRTY_DELIVERY if entry.rule_id == rule.rule_id and entry.source == origin]


def witness_of(rule: IntraRule, via: tuple[str, ...]) -> str:
    """The first hop of an import chain that leaves the rule's own source packages."""

    return next((module for module in via if not any(under(module, prefix) for prefix in rule.sources)), via[0])


def intra_rules_for(module: str) -> list[IntraRule]:
    return [rule for rule in INTRA_DOMAIN_RULES if any(under(module, prefix) for prefix in rule.sources)]


def intra_forbidden(rule: IntraRule, module: str) -> str | None:
    return next((prefix for prefix in rule.forbidden if under(module, prefix)), None)


def intra_allowed(origin: str, module: str, parent_only: bool = False) -> bool:
    """True when an allowance for ``origin`` covers ``module`` (or, as a parent package, leads to it)."""

    for allowance in INTRA_DOMAIN_ALLOWANCES:
        if under(origin, allowance.source) and (under(module, allowance.target) or (parent_only and under(allowance.target, module))):
            return True
    return False


def in_shared_package(module: str | None) -> bool:
    return module is not None and any(module == name or module.startswith(name + ".") for name in SHARED_PACKAGES)


def domain_of(module: str) -> str | None:
    if module == "hyprial":
        return "kernel"
    parts = module.split(".")
    if len(parts) > 1 and parts[0] == "hyprial" and parts[1] in DOMAINS:
        return parts[1]
    if module in {f"hyprial.{Path(name).stem}" for name in OUTER_ENTRIES}:
        return "shell"
    return None


@dataclass
class Source:
    path: str
    module: str
    domain: str | None
    sha256: str
    tree: ast.Module
    data_area: bool = False

    @property
    def package(self) -> str:
        return self.module if self.path.endswith("/__init__.py") else self.module.rpartition(".")[0]


def relative_target(source: Source, name: str | None, level: int) -> str | None:
    try:
        return importlib.util.resolve_name("." * level + (name or ""), source.package) if level else name
    except (ImportError, ValueError):
        return None


def literal_strings(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    if all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts):
        return [item.value for item in node.elts]
    return None


def is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in assigned_names(item)]
    return []


class Audit:
    def __init__(self, root: Path):
        self.root = root
        self.sources: dict[str, Source] = {}
        self.modules: set[str] = set()
        self.violations: list[dict] = []
        self.unknown: list[dict] = []
        self.edges: list[dict] = []
        self.faces: dict[str, dict] = {}
        self.bindings: dict[str, dict] = {}
        self.managed_calls: set[tuple[str, int]] = set()
        self.imports_covered = 0
        self.dynamic_covered = 0
        self.parents: dict[str, dict] = {}
        self.tc_bindings: dict[str, tuple[set[str], set[str]]] = {}
        self.links: dict[str, list[tuple[str, str | None, bool, bool, ast.AST]]] = {}
        self.loads: dict[tuple[str, str | None], list[tuple[str, bool, bool]]] = {}
        self.dirty_used: set[tuple[IntraDirty, str]] = set()
        manysys_forbidden = kernel_non_manysys(root)
        self.intra_rules = tuple(
            IntraRule(row.rule_id, row.sources, tuple(dict.fromkeys((*row.forbidden, *manysys_forbidden))))
            if row.rule_id == "intra/manysys-leaf" else row
            for row in INTRA_DOMAIN_RULES
        )

    def location(self, source: Source, node: ast.AST | None) -> dict:
        line, column = getattr(node, "lineno", 1), getattr(node, "col_offset", 0)
        owner, current = [], node
        parents = self.parents.get(source.module, {})
        while current is not None and id(current) in parents:
            current = parents[id(current)]
            if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                owner.append(current.name)
        qualified = ".".join(reversed(owner)) or "<module>"
        return {"path": source.path, "line": line, "column": column, "owner": qualified,
                "sha256": source.sha256,
                "site": f"{source.path}:{line}:{column}:{type(node).__name__}:{qualified}"}

    def error(self, kind: str, source: Source | None = None, node: ast.AST | None = None, **details):
        self.violations.append({"kind": kind, **(self.location(source, node) if source else {}), **details})

    def collect(self):
        base = self.root / "src/hyprial"
        if not base.is_dir() or base.is_symlink():
            self.error("missing-product-root", path="src/hyprial")
            return
        for directory, names, files in os.walk(base, followlinks=False):
            names[:] = sorted(name for name in names if name != "__pycache__")
            for name in names + sorted(files):
                path = Path(directory) / name
                if path.is_symlink():
                    self.error("source-symlink", path=path.relative_to(self.root).as_posix())
            names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink() or path.suffix not in {".py", ".pyi"}:
                    continue
                rel = path.relative_to(self.root).as_posix()
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    self.error("unreadable-input", path=rel, detail=str(exc))
                    continue
                try:
                    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
                    tree = ast.parse(raw.decode(encoding), filename=rel)
                except (SyntaxError, UnicodeError) as exc:
                    self.error("parse-error", path=rel, sha256=digest(raw), detail=str(exc))
                    continue
                mod = module_name(rel) if path.suffix == ".py" else module_name(rel[:-1])
                source = Source(rel, mod, domain_of(mod), digest(raw), tree, in_data_area(rel))
                if mod in self.sources:
                    self.error("duplicate-module", source, detail="Python and stub source share an ambiguous identity")
                    continue
                self.sources[mod] = source
                self.parents[mod] = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
                self.modules.update(".".join(mod.split(".")[:i]) for i in range(1, len(mod.split(".")) + 1))
                if source.data_area or in_shared_package(mod):
                    # Registered and parsed like everything else; only the domain
                    # judgement is skipped.  See the invariant next to DATA_AREAS.
                    pass
                elif source.domain is None:
                    self.error("unmapped-product-file", source)
                elif mod != "hyprial" and mod.split(".")[1] in DOMAINS:
                    # Domain code lives under impl, with only two explicit public face files.
                    tail = mod.split(".")[2:]
                    if tail and tail != ["interface"] and tail[0] != "impl":
                        self.error("unmapped-product-file", source, detail="domain code must live under impl")
        for face in sorted(PUBLIC_FACES):
            if face not in self.sources:
                self.error("missing-public-face", module=face)
        if "hyprial" not in self.sources:
            self.error("missing-package-metadata", path="src/hyprial/__init__.py")

    def type_checking(self, source: Source, node: ast.AST) -> bool:
        if source.module not in self.tc_bindings:
            names, modules = {"TYPE_CHECKING"}, {"typing"}
            for item in ast.walk(source.tree):
                if isinstance(item, ast.ImportFrom) and item.module == "typing":
                    names.update(alias.asname or alias.name for alias in item.names if alias.name == "TYPE_CHECKING")
                elif isinstance(item, ast.Import):
                    modules.update(alias.asname or alias.name for alias in item.names if alias.name == "typing")
            self.tc_bindings[source.module] = names, modules
        names, modules = self.tc_bindings[source.module]
        current = node
        while id(current) in self.parents[source.module]:
            parent = self.parents[source.module][id(current)]
            if isinstance(parent, ast.If) and current in parent.body:
                test = parent.test
                if (isinstance(test, ast.Name) and test.id in names) or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING" and isinstance(test.value, ast.Name) and test.value.id in modules):
                    return True
            current = parent
        return False

    def module_level(self, source: Source, node: ast.AST) -> bool:
        current = node
        while id(current) in self.parents[source.module]:
            current = self.parents[source.module][id(current)]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return False
        return not self.type_checking(source, node)

    def build_bindings(self):
        for module, source in self.sources.items():
            bindings = {}
            for node in ast.walk(source.tree):
                if not self.module_level(source, node):
                    continue
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    bindings[node.name] = {"module": module, "symbol": node.name, "local": True}
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        for name in assigned_names(target):
                            bindings[name] = {"module": module, "symbol": name, "local": True}
                elif isinstance(node, ast.ImportFrom):
                    target = relative_target(source, node.module, node.level)
                    if not target:
                        continue
                    for alias in node.names:
                        child = f"{target}.{alias.name}"
                        bindings[alias.asname or alias.name] = {"module": child if child in self.modules else target,
                            "symbol": None if child in self.modules else alias.name}
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        bindings[alias.asname or alias.name.split(".")[0]] = {"module": alias.name if alias.asname else alias.name.split(".")[0], "symbol": None}
            self.bindings[module] = bindings

    def table_mutated(self, source: Source, name: str) -> bool:
        assignments = 0
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
                assignments += 1
            if isinstance(node, ast.Subscript) and is_name(node.value, name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and is_name(node.func.value, name):
                return True
            if isinstance(node, ast.AugAssign) and is_name(node.target, name):
                return True
        return assignments > 1

    def face(self, module: str, stack: tuple = ()) -> dict:
        if module in self.faces:
            return self.faces[module]
        source = self.sources.get(module)
        if source is None or module in stack:
            return {"names": [], "providers": {}}
        names, forwarded = None, None
        for node in source.tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(is_name(target, "__all__") for target in targets):
                    names = literal_strings(node.value)
                    if names is None:
                        self.error("nonliteral-all", source, node)
                    elif len(names) != len(set(names)):
                        self.error("duplicate-export", source, node)
            elif isinstance(node, ast.ImportFrom) and any(alias.name == "__all__" for alias in node.names):
                forwarded = relative_target(source, node.module, node.level)
                expected = module + ".interface" if module in {f"hyprial.{d}" for d in DOMAINS} else None
                if forwarded != expected or any(alias.name == "__all__" and alias.asname not in (None, "__all__") for alias in node.names):
                    self.error("invalid-all-forwarding", source, node, target=forwarded)
                    forwarded = None
        if self.table_mutated(source, "__all__"):
            self.error("mutated-export-table", source, table="__all__")
        if names is not None and forwarded is not None:
            self.error("invalid-all-forwarding", source, detail="forwarded __all__ is reassigned")
        providers = {}
        if forwarded:
            delegated = self.face(forwarded, (*stack, module))
            names = delegated["names"]
            getter = self.bindings[module].get("__getattr__", {})
            if getter.get("module") == forwarded and getter.get("symbol") == "__getattr__":
                providers = dict(delegated["providers"])
        if names is None:
            self.error("missing-export-allowlist", source)
            names = []
        lazy = self.lazy_map(source, names)
        if lazy is not None:
            providers.update(lazy)
        else:
            for name in names:
                if name in self.bindings[module]:
                    providers[name] = self.bindings[module][name]
        result = {"names": names, "providers": providers, "path": source.path, "sha256": source.sha256}
        self.faces[module] = result
        for name in names:
            provider = providers.get(name)
            if provider is None or not self.provider_exists(provider):
                self.error("missing-export-provider", source, export=name, provider=provider)
        if forwarded:
            for name in names:
                actual = providers.get(name)
                expected = delegated["providers"].get(name)
                if actual and expected and self.canonical_provider(actual) != self.canonical_provider(expected):
                    self.error("inconsistent-forwarded-provider", source, export=name, provider=actual)
        return result

    def provider_exists(self, provider: dict, seen: tuple = ()) -> bool:
        return self.canonical_provider(provider, seen) is not None

    def canonical_provider(self, provider: dict, seen: tuple = ()) -> dict | None:
        module, symbol = provider["module"], provider.get("symbol")
        if not internal(module):
            return {"module": module, "symbol": symbol}
        if module not in self.modules:
            return None
        if symbol is None or provider.get("local"):
            return {"module": module, "symbol": symbol}
        key = module, symbol
        if key in seen:
            return None
        if module in PUBLIC_FACES:
            resolved = self.face(module, tuple(m for m, _ in seen))["providers"].get(symbol)
        else:
            resolved = self.bindings.get(module, {}).get(symbol)
        return self.canonical_provider(resolved, (*seen, key)) if resolved else None

    def lazy_map(self, source: Source, names: list[str]) -> dict | None:
        entries = [n for n in source.tree.body if isinstance(n, ast.Assign) and any(is_name(t, "_FACADE_EXPORTS") for t in n.targets)]
        if not entries:
            return None
        table = entries[0].value
        providers = {}
        valid = len(entries) == 1 and isinstance(table, ast.Dict)
        if valid:
            for key, value in zip(table.keys, table.values):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or key.value in providers:
                    valid = False
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    providers[key.value] = {"module": value.value, "symbol": key.value}
                elif isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == 2 and isinstance(value.elts[0], ast.Constant) and isinstance(value.elts[0].value, str) and isinstance(value.elts[1], ast.Constant) and (isinstance(value.elts[1].value, str) or value.elts[1].value is None):
                    providers[key.value] = {"module": value.elts[0].value, "symbol": value.elts[1].value}
                else:
                    valid = False
        if not valid:
            self.error("invalid-lazy-provider-map", source, entries[0])
        if set(providers) != set(names):
            self.error("lazy-map-all-mismatch", source, missingFromAll=sorted(set(providers) - set(names)), missingFromMap=sorted(set(names) - set(providers)))
            valid = False
        if self.table_mutated(source, "_FACADE_EXPORTS"):
            self.error("mutated-export-table", source, table="_FACADE_EXPORTS")
            valid = False
        for node in ast.walk(source.tree):
            if is_name(node, "_FACADE_EXPORTS") and isinstance(node.ctx, ast.Load):
                parent = self.parents[source.module][id(node)]
                if not isinstance(parent, ast.Subscript) or parent.value is not node:
                    self.error("escaped-export-table", source, node)
                    valid = False
        calls = self.lazy_call_shape(source, table)
        if not any(isinstance(node, ast.FunctionDef) and node.name == "__getattr__" for node in source.tree.body):
            self.error("missing-lazy-facade", source)
        if calls is not None and valid:
            self.managed_calls.add((source.module, id(calls)))
            for name, provider in providers.items():
                self.edge(source, calls, provider["module"], provider.get("symbol"), "lazy-provider", export=name)
        return providers

    def lazy_call_shape(self, source: Source, table: ast.AST) -> ast.Call | None:
        """Finite, deliberately narrow supported protocol; no generic dataflow."""
        functions = [n for n in source.tree.body if isinstance(n, ast.FunctionDef) and n.name == "__getattr__"]
        if len(functions) != 1 or not isinstance(table, ast.Dict):
            return None
        function = functions[0]
        if len(function.args.args) != 1 or function.args.posonlyargs or function.args.kwonlyargs or function.args.vararg or function.args.kwarg:
            return None
        public_name = function.args.args[0].arg
        body = list(function.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body.pop(0)
        if not body or not isinstance(body[0], ast.Try):
            return None
        guard = body.pop(0)
        if len(guard.body) != 1 or len(guard.handlers) != 1 or guard.orelse or guard.finalbody:
            return None
        lookup, handler = guard.body[0], guard.handlers[0]
        if not isinstance(lookup, ast.Assign) or len(lookup.targets) != 1 or not isinstance(lookup.value, ast.Subscript) or not is_name(lookup.value.value, "_FACADE_EXPORTS") or not is_name(lookup.value.slice, public_name):
            return None
        if not is_name(handler.type, "KeyError") or len(handler.body) != 1 or not isinstance(handler.body[0], ast.Raise) or not isinstance(handler.body[0].exc, ast.Call) or not is_name(handler.body[0].exc.func, "AttributeError"):
            return None
        targets = assigned_names(lookup.targets[0])
        pair_values = all(isinstance(value, (ast.Tuple, ast.List)) for value in table.values)
        if len(targets) != (2 if pair_values else 1):
            return None
        module_var, symbol_var = targets[0], targets[1] if pair_values else public_name
        if len(set([public_name, *targets])) != len(targets) + 1:
            return None
        reflect_names = {alias.asname or alias.name for item in source.tree.body if isinstance(item, ast.ImportFrom) and item.module == "importlib" for alias in item.names if alias.name == "import_module"}
        if body and isinstance(body[0], ast.ImportFrom) and body[0].module == "importlib" and len(body[0].names) == 1 and body[0].names[0].name == "import_module":
            reflect_names = {body.pop(0).names[0].asname or "import_module"}
        if not body or not isinstance(body[0], ast.Assign) or len(body[0].targets) != 1 or not isinstance(body[0].targets[0], ast.Name):
            return None
        assignment = body.pop(0)
        value = assignment.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in reflect_names:
            dynamic = value
            provider_var = assignment.targets[0].id
            if not pair_values or not body or not isinstance(body[0], ast.Assign) or len(body[0].targets) != 1 or not isinstance(body[0].targets[0], ast.Name):
                return None
            assignment = body.pop(0)
            selected = assignment.value
            if not isinstance(selected, ast.IfExp) or not isinstance(selected.test, ast.Compare) or not is_name(selected.test.left, symbol_var) or len(selected.test.ops) != 1 or not isinstance(selected.test.ops[0], ast.Is) or len(selected.test.comparators) != 1 or not isinstance(selected.test.comparators[0], ast.Constant) or selected.test.comparators[0].value is not None or not is_name(selected.body, provider_var):
                return None
            access = selected.orelse
            if not isinstance(access, ast.Call) or not is_name(access.func, "getattr") or len(access.args) != 2 or access.keywords or not is_name(access.args[0], provider_var) or not is_name(access.args[1], symbol_var):
                return None
        else:
            if pair_values and any(isinstance(value.elts[1], ast.Constant) and value.elts[1].value is None for value in table.values):
                return None
            if not isinstance(value, ast.Call) or not is_name(value.func, "getattr") or len(value.args) != 2 or value.keywords or not is_name(value.args[1], symbol_var):
                return None
            dynamic = value.args[0]
        if not isinstance(dynamic, ast.Call) or not isinstance(dynamic.func, ast.Name) or dynamic.func.id not in reflect_names or len(dynamic.args) != 1 or dynamic.keywords or not is_name(dynamic.args[0], module_var):
            return None
        result_var = assignment.targets[0].id
        if body and isinstance(body[0], ast.Assign):
            cache = body.pop(0)
            if len(cache.targets) != 1 or not isinstance(cache.targets[0], ast.Subscript) or not isinstance(cache.targets[0].value, ast.Call) or not is_name(cache.targets[0].value.func, "globals") or cache.targets[0].value.args or cache.targets[0].value.keywords or not is_name(cache.targets[0].slice, public_name) or not is_name(cache.value, result_var):
                return None
        if len(body) != 1 or not isinstance(body[0], ast.Return) or not is_name(body[0].value, result_var):
            return None
        return dynamic

    def edge(self, source: Source, node: ast.AST, target: str | None, symbol: str | None, kind: str, **details):
        if not target:
            self.error("unresolved-import-target", source, node)
            return
        if not internal(target):
            return
        self.edges.append({**self.location(source, node), "kind": kind, "from": source.module, "to": target, "symbol": symbol, **details})
        native_importers = NATIVE_MODULE_IMPORTERS.get(target)
        if native_importers is not None:
            if source.module not in native_importers:
                self.error("native-module-import", source, node, target=target)
            return
        if target not in self.modules:
            self.error("missing-internal-module", source, node, target=target)
            return
        if kind != "lazy-provider":
            self.intra_edge(source, node, target, symbol)
        # THE DATA-AREA BRANCHES.  They must stay ABOVE both lines further down: the
        # `source.domain is None` early return is SILENT, and anything placed after it would
        # leave a data-area script's imports of product code reporting nothing at all --
        # which is the hole this closes.
        destination_is_data = module_in_data_area(target)
        if source.data_area != destination_is_data:
            self.error(
                "data-area-imports-product-code" if source.data_area else "data-area-import",
                source,
                node,
                target=target,
            )
            return
        if destination_is_data:
            # Both ends inside a data area: no domain rule applies to either, and a false
            # positive here would make the payload unmaintainable.
            return
        if in_shared_package(source.module) and not in_shared_package(target):
            self.error("shared-package-imports-product-code", source, node, target=target)
            return
        if in_shared_package(target):
            return
        destination = domain_of(target)
        if destination is None:
            self.error("unmapped-internal-module", source, node, target=target)
            return
        if source.domain is None:
            return
        if RANK[source.domain] < RANK[destination]:
            self.error("reverse-domain", source, node, target=target, sourceDomain=source.domain, targetDomain=destination)
        if source.domain == destination:
            return
        if target == "hyprial" and symbol == "__version__" and "__version__" in self.bindings.get("hyprial", {}):
            return
        if target not in PUBLIC_FACES:
            self.error("public-face-bypass", source, node, target=target)
        elif symbol is not None and symbol not in self.face(target)["names"]:
            self.error("unlisted-public-symbol", source, node, target=target, symbol=symbol)

    def deferred(self, source: Source, node: ast.AST) -> bool:
        current = node
        while id(current) in self.parents[source.module]:
            current = self.parents[source.module][id(current)]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return True
        return False

    def intra_edge(self, source: Source, node: ast.AST, target: str, symbol: str | None):
        """Record the edge for the closure walk and reject a literal import a rule forbids."""

        typed = self.type_checking(source, node)
        self.links.setdefault(source.module, []).append((target, symbol, typed, not typed and not self.deferred(source, node), node))
        for rule in (row for row in self.intra_rules if any(under(source.module, prefix) for prefix in row.sources)):
            forbidden = intra_forbidden(rule, target)
            if forbidden and not intra_allowed(source.module, target):
                self.error("intra-domain-import", source, node, rule=rule.rule_id, target=target,
                           forbidden=forbidden, via=[], **{"from": source.module})

    def intra_loads(self, target: str, symbol: str | None) -> list[tuple[str, bool, bool]]:
        """(module, reached only as a parent package, literal) for what importing ``target`` loads."""

        key = target, symbol
        if key not in self.loads:
            loaded = [(target, False, True)] + [(parent, True, True) for parent in reversed(ancestors(target))]
            if symbol is not None and target in PUBLIC_FACES:
                provider = self.canonical_provider({"module": target, "symbol": symbol})
                if provider and internal(provider["module"]) and provider["module"] != target:
                    loaded = [(provider["module"], False, False)] + [(parent, True, False) for parent in reversed(ancestors(provider["module"]))] + loaded
            self.loads[key] = loaded
        return self.loads[key]

    def intra_closure(self):
        for origin in sorted(self.sources):
            for rule in (row for row in self.intra_rules if any(under(origin, prefix) for prefix in row.sources)):
                self.intra_walk(rule, origin)
        for entry in DIRTY_DELIVERY:
            rule = next((row for row in self.intra_rules if row.rule_id == entry.rule_id), None)
            if rule is None or not any(prefix in self.sources for prefix in rule.sources):
                continue
            for forbidden in entry.forbidden:
                if (entry, forbidden) not in self.dirty_used:
                    self.error("stale-intra-dirty-allowance", self.sources.get(entry.source), None, rule=entry.rule_id,
                               witness=entry.witness, forbidden=forbidden, **{"from": entry.source})

    def intra_walk(self, rule: IntraRule, origin: str):
        """Breadth-first walk of what ``origin`` loads at import time; report each forbidden package once."""

        source = self.sources[origin]
        dirty = dirty_for(rule, origin)
        queue = deque()
        for target, symbol, typed, _, node in self.links.get(origin, []):
            if not typed:
                queue.extend((module, parent_only, None, (module,), node, literal) for module, parent_only, literal in self.intra_loads(target, symbol))
        queue.extend((parent, True, None, (parent,), None, False) for parent in reversed(ancestors(origin))
                     if not any(under(parent, prefix) for prefix in rule.sources))
        seen, reported = set(), set()
        while queue:
            module, parent_only, exempt, via, site, literal = queue.popleft()
            forbidden = intra_forbidden(rule, module)
            child_exempt = None
            if forbidden and intra_allowed(origin, module, parent_only):
                child_exempt = forbidden if intra_allowed(origin, module) else None
            elif forbidden and exempt == forbidden:
                child_exempt = None if parent_only else forbidden
            elif forbidden:
                excuse = next((entry for entry in dirty if not literal and witness_of(rule, via) == entry.witness and forbidden in entry.forbidden), None)
                if excuse is not None:
                    self.dirty_used.add((excuse, forbidden))
                    continue
                if not literal and forbidden not in reported:
                    reported.add(forbidden)
                    self.error("intra-domain-import", source, site, rule=rule.rule_id, target=module,
                               forbidden=forbidden, via=list(via), **{"from": origin})
                continue
            if module != origin and any(under(module, prefix) for prefix in rule.sources):
                continue
            if rule.rule_id == "intra/manysys-leaf" and parent_only and module == "hyprial.kernel":
                continue
            key = (module, child_exempt, witness_of(rule, via) if dirty else None)
            if key in seen:
                continue
            seen.add(key)
            for target, symbol, _, eager, _ in self.links.get(module, []):
                if eager:
                    queue.extend((loaded, only_parent, child_exempt, (*via, loaded), site, False) for loaded, only_parent, _ in self.intra_loads(target, symbol))

    def static_imports(self, source: Source):
        for node in ast.walk(source.tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            self.imports_covered += 1
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.edge(source, node, alias.name, None, "import")
            else:
                base = relative_target(source, node.module, node.level)
                for alias in node.names:
                    if alias.name == "*":
                        self.error("star-import", source, node, target=base)
                        continue
                    child = f"{base}.{alias.name}" if base else ""
                    self.edge(source, node, child if child in self.modules else base, None if child in self.modules else alias.name, "from")

    def dynamic_imports(self, source: Source):
        names, modules = {"__import__": "__import__"}, {}
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"importlib", "builtins", "runpy"}:
                        modules[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins", "runpy"}:
                for alias in node.names:
                    if alias.name in {"import_module", "__import__", "run_module", "run_path"}:
                        names[alias.asname or alias.name] = alias.name
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call):
                continue
            callable_name = names.get(node.func.id) if isinstance(node.func, ast.Name) else None
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in modules:
                owner = modules[node.func.value.id]
                if (owner == "importlib" and node.func.attr == "import_module") or (owner == "builtins" and node.func.attr == "__import__") or (owner == "runpy" and node.func.attr in {"run_module", "run_path"}):
                    callable_name = node.func.attr
            if callable_name is None:
                continue
            if (source.module, id(node)) in self.managed_calls:
                self.dynamic_covered += 1
                continue
            first = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in {"name", "mod_name"}), None)
            target = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
            if target and target.startswith(".") and callable_name == "import_module":
                package = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "package"), None)
                package_name = source.package if is_name(package, "__package__") else package.value if isinstance(package, ast.Constant) and isinstance(package.value, str) else None
                try:
                    target = importlib.util.resolve_name(target, package_name) if package_name else None
                except (ImportError, ValueError):
                    target = None
            if callable_name == "run_path":
                path = self.literal_source_path(source, first)
                if path is not None and path.is_relative_to(self.root / "src/hyprial"):
                    self.dynamic_covered += 1
                    self.edge(source, node, module_name(path.relative_to(self.root).as_posix()), None, "dynamic-source-path")
                    continue
            if target is None or callable_name == "run_path":
                self.unknown.append({**self.location(source, node), "kind": "unresolved-dynamic-call", "callable": callable_name,
                    "callSha256": digest(ast.dump(node, include_attributes=False).encode()),
                    "detail": "possible internal runtime import requires callsite review; no dynamic-call waiver"})
            else:
                self.dynamic_covered += 1
                self.edge(source, node, target, None, "dynamic")
                if callable_name == "__import__" and internal(target):
                    fromlist = node.args[3] if len(node.args) > 3 else next((k.value for k in node.keywords if k.arg == "fromlist"), None)
                    if fromlist is not None:
                        members = literal_strings(fromlist)
                        if members is None:
                            self.unknown.append({**self.location(source, node), "kind": "unresolved-dynamic-fromlist", "callable": callable_name,
                                "callSha256": digest(ast.dump(node, include_attributes=False).encode()), "detail": "computed fromlist can select internal child modules"})
                        else:
                            for member in members:
                                if member == "*":
                                    self.error("star-import", source, node, target=target)
                                    continue
                                child = f"{target}.{member}"
                                self.edge(source, node, child if child in self.modules else target, None if child in self.modules else member, "dynamic-fromlist")

    def literal_source_path(self, source: Source, node: ast.AST | None, seen: tuple = ()) -> Path | None:
        """Only __file__ + pathlib parent/with_name/literal joins; no runtime IO."""
        if is_name(node, "__file__"):
            return self.root / source.path
        if isinstance(node, ast.Name) and node.id not in seen:
            scope = node
            while id(scope) in self.parents[source.module]:
                scope = self.parents[source.module][id(scope)]
                if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    parameters = scope.args.posonlyargs + scope.args.args + scope.args.kwonlyargs
                    if any(argument.arg == node.id for argument in parameters) or any(isinstance(item, ast.Name) and item.id == node.id and isinstance(item.ctx, ast.Store) for item in ast.walk(scope)):
                        return None
            values = [item.value for item in source.tree.body if isinstance(item, ast.Assign) and len(item.targets) == 1 and is_name(item.targets[0], node.id)]
            if len(values) == 1:
                return self.literal_source_path(source, values[0], (*seen, node.id))
        if isinstance(node, ast.Call) and not node.keywords:
            path_names = {alias.asname or alias.name for item in source.tree.body if isinstance(item, ast.ImportFrom) and item.module == "pathlib" for alias in item.names if alias.name == "Path"}
            if isinstance(node.func, ast.Name) and node.func.id in path_names | {"str"} and len(node.args) == 1:
                return self.literal_source_path(source, node.args[0], seen)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "with_name" and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                base = self.literal_source_path(source, node.func.value, seen)
                if base is not None:
                    try:
                        return base.with_name(node.args[0].value)
                    except ValueError:
                        return None
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self.literal_source_path(source, node.value, seen)
            return base.parent if base is not None else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            base = self.literal_source_path(source, node.left, seen)
            return (base / node.right.value).resolve() if base is not None else None
        return None

    def report(self) -> dict:
        self.collect()
        self.build_bindings()
        for face in sorted(PUBLIC_FACES):
            if face in self.sources:
                self.face(face)
        for source in self.sources.values():
            self.static_imports(source)
            self.dynamic_imports(source)
        self.intra_closure()
        for source in self.sources.values():
            try:
                unchanged = digest((self.root / source.path).read_bytes()) == source.sha256
            except OSError:
                unchanged = False
            if not unchanged:
                self.error("input-drift", source)
        status = "FAIL" if self.violations else "BLOCKED" if self.unknown else "PASS"
        return {"schemaVersion": 1, "status": status, "root": str(self.root), "domainOrder": DOMAINS,
            "gateSha256": digest(Path(__file__).read_bytes()),
            "dynamicReviewStatus": "BLOCKED" if self.unknown else "PASS",
            "approvedOuterEntries": sorted(OUTER_ENTRIES), "filesCovered": len(self.sources),
            "importStatementsCovered": self.imports_covered, "dynamicCallsResolved": self.dynamic_covered,
            "unknownDynamicCalls": self.unknown, "violations": self.violations, "edges": self.edges,
            "inputs": [{"path": s.path, "sha256": s.sha256, "module": s.module, "domain": s.domain} for s in self.sources.values()],
            "publicFaces": [{"module": face, **self.faces[face],
                "canonicalExports": {name: self.canonical_provider(provider) for name, provider in self.faces[face]["providers"].items()}}
                for face in sorted(self.faces)],
            "limitations": ["Static AST proof only; no product imported or runtime behavior proven.",
                "Reflective callable detection covers importlib.import_module, builtin __import__, runpy module/path calls and direct import aliases; indirect aliases, eval/exec and plugin reflection require separate review.",
                "Provider existence follows explicit imports/definitions; conditional runtime availability needs installed import tests."]}


def scan(root: Path) -> dict:
    return Audit(root.resolve()).report()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = scan(args.root)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    for row in report["violations"]:
        if row["kind"] == "stale-intra-dirty-allowance":
            print(f"{row['rule']}: stale DIRTY_DELIVERY entry {row['from']} via {row['witness']} -> {row['forbidden']}; delete it", file=sys.stderr)
        elif row.get("rule"):
            chain = " -> ".join(row["via"]) or "direct"
            print(f"{row['rule']}: {row['from']} must not load {row['forbidden']} ({row['path']}:{row['line']}, via {chain})", file=sys.stderr)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
