"""LibCST parsing cache and metadata helpers shared by the transform engines."""

from __future__ import annotations

import libcst as cst
import libcst.metadata as csm
from libcst.metadata.scope_provider import Access, BaseAssignment, ImportAssignment

from .common import ToolError, Workspace, decode_source


class ParsedPython:
    """A parsed Python file with lazily resolved scope metadata."""

    def __init__(self, path: str, source: str, encoding: str) -> None:
        self.path = path
        self.source = source
        self.encoding = encoding
        try:
            self.module = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:
            raise ToolError(f"{path}: LibCST parse error: {exc}") from exc
        self.wrapper = csm.MetadataWrapper(self.module, unsafe_skip_copy=True)
        self._scopes: dict[cst.CSTNode, csm.Scope] | None = None
        self._positions: dict[cst.CSTNode, csm.CodeRange] | None = None

    def scopes(self) -> dict[cst.CSTNode, csm.Scope]:
        if self._scopes is None:
            self._scopes = self.wrapper.resolve(csm.ScopeProvider)
        return self._scopes

    def positions(self) -> dict[cst.CSTNode, csm.CodeRange]:
        if self._positions is None:
            self._positions = self.wrapper.resolve(csm.PositionProvider)
        return self._positions

    def line_of(self, node: cst.CSTNode) -> int:
        return self.positions()[node].start.line

    def scope_of(self, node: cst.CSTNode) -> csm.Scope:
        scope = self.scopes().get(node)
        if scope is None:
            raise ToolError(f"{self.path}: no scope metadata for node at {node}")
        return scope

    def assignments_for_name(self, node: cst.CSTNode, name: str) -> set[BaseAssignment]:
        """All assignments visible for `name` in the scope containing `node`."""
        return set(self.scope_of(node)[name])

    def access_map(self) -> dict[int, Access]:
        """Map id(access.node) -> Access for every recorded access."""
        result: dict[int, Access] = {}
        for scope in set(self.scopes().values()):
            for access in scope.accesses:
                result[id(access.node)] = access
        return result

    def build_parent_map(self) -> dict[int, cst.CSTNode]:
        parents: dict[int, cst.CSTNode] = {}

        def walk(node: cst.CSTNode, parent: cst.CSTNode | None) -> None:
            if parent is not None:
                parents[id(node)] = parent
            for child in node.children:
                walk(child, node)

        walk(self.module, None)
        return parents


def import_aliases(stmt: cst.CSTNode) -> list[cst.ImportAlias]:
    if isinstance(stmt, cst.Import):
        return [a for a in stmt.names]
    if isinstance(stmt, cst.ImportFrom):
        if isinstance(stmt.names, cst.ImportStar):
            return []
        return [a for a in stmt.names]
    return []


def binding_name(alias: cst.ImportAlias) -> str:
    if alias.asname is not None:
        return alias.asname.name.value
    if isinstance(alias.name, cst.Name):
        return alias.name.value
    parts = []
    node = alias.name
    while isinstance(node, cst.Attribute):
        parts.append(node.attr.value)
        node = node.value
    parts.append(node.value)
    return ".".join(reversed(parts))


def import_target_name(alias: cst.ImportAlias) -> str:
    """Full dotted import target (`a.b.c` for `import a.b.c as x`)."""
    if isinstance(alias.name, cst.Name):
        return alias.name.value
    parts = []
    node = alias.name
    while isinstance(node, cst.Attribute):
        parts.append(node.attr.value)
        node = node.value
    parts.append(node.value)
    return ".".join(reversed(parts))


def assign_target_names(small: cst.CSTNode) -> list[str]:
    """Bound names of an Assign/AnnAssign statement (AssignTarget.target)."""
    names: list[str] = []
    targets = getattr(small, "targets", None)
    if targets is None:
        target = getattr(small, "target", None)
        targets = [target] if target is not None else []
    for wrapper in targets:
        node = getattr(wrapper, "target", wrapper)
        if isinstance(node, cst.Name):
            names.append(node.value)
    return names


def import_symbol_name(alias: cst.ImportAlias) -> str:
    """The imported symbol as written (`s` for `from m import s as n`)."""
    if isinstance(alias.name, cst.Name):
        return alias.name.value
    parts = []
    node = alias.name
    while isinstance(node, cst.Attribute):
        parts.append(node.attr.value)
        node = node.value
    parts.append(node.value)
    return ".".join(reversed(parts))


def import_root_binding(stmt: cst.Import, alias: cst.ImportAlias) -> str:
    """Runtime-bound name of a plain import alias (the root package name)."""
    if alias.asname is not None:
        return alias.asname.name.value
    return binding_name(alias).split(".")[0]


class TransformContext:
    """Per-run context: workspace, write-scope policy, parse cache."""

    def __init__(self, workspace: Workspace, policy=None) -> None:
        self.workspace = workspace
        if policy is None:
            from .scope_policy import default_policy

            policy = default_policy()
        self.policy = policy
        self._cache: dict[tuple[str, str], ParsedPython] = {}
        self.warnings: list[tuple[str, str, str | None, int | None]] = []
        self.scope_excluded: list[dict] = []

    def writable(self, rel: str) -> bool:
        from .scope_policy import classify_path

        decision = classify_path(rel, self.policy)
        if not decision["writable"]:
            row = {"path": rel, "reason": decision["reason"]}
            if row not in self.scope_excluded:
                self.scope_excluded.append(row)
            return False
        return True

    def writable_strict(self, rel: str) -> None:
        """Refuse (ToolError) when a rule targets a read-only path directly."""
        from .common import ToolError
        from .scope_policy import classify_path

        decision = classify_path(rel, self.policy)
        if not decision["writable"]:
            self.writable(rel)  # record for the report
            raise ToolError(
                f"{rel}: path is read-only by scope policy ({decision['reason']}); "
                "the rule cannot target it"
            )

    def parse(self, rel: str) -> ParsedPython:
        data = self.workspace.read(rel)
        key = (rel, __import__("hashlib").sha256(data).hexdigest())
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        source, encoding = decode_source(data, rel)
        parsed = ParsedPython(rel, source, encoding)
        self._cache[key] = parsed
        return parsed

    def warn(self, code: str, message: str, path: str | None = None, line: int | None = None) -> None:
        self.warnings.append((code, message, path, line))

    def read_text(self, rel: str) -> tuple[str, str]:
        from .common import decode_source

        return decode_source(self.workspace.read(rel), rel)

    def write_text(self, rel: str, text: str, encoding: str) -> None:
        self.workspace.write(rel, text.encode(encoding))


def find_import_assignment(
    parsed: ParsedPython, stmt: cst.CSTNode, alias: cst.ImportAlias
) -> ImportAssignment | None:
    """The ImportAssignment created by `stmt` for `alias` (root name for imports)."""
    assignments = import_statement_assignments(parsed, stmt, alias)
    return assignments[0] if assignments else None


def import_statement_assignments(
    parsed: ParsedPython,
    stmt: cst.CSTNode,
    alias: cst.ImportAlias | None = None,
) -> list[ImportAssignment]:
    """Every ImportAssignment created by `stmt`.

    For ``import a.b.c`` LibCST records one assignment per dotted prefix
    (``a``, ``a.b``, ``a.b.c``); chain accesses attach to the deepest prefix,
    so callers that want all usages of the statement must union their
    references.
    """
    scope = parsed.scope_of(stmt)
    if isinstance(stmt, cst.Import):
        if alias is not None:
            aliases = [alias]
        else:
            aliases = import_aliases(stmt)
        names: list[str] = []
        for alias_node in aliases:
            if alias_node.asname is not None:
                names.append(alias_node.asname.name.value)
            else:
                parts = binding_name(alias_node).split(".")
                names.extend(".".join(parts[: i + 1]) for i in range(len(parts)))
    else:
        if alias is not None:
            names = [binding_name(alias)]
        else:
            names = [binding_name(a) for a in import_aliases(stmt)]
    result: list[ImportAssignment] = []
    seen: set[int] = set()
    for name in names:
        try:
            candidates = scope[name]
        except KeyError:
            continue
        for assignment in candidates:
            if assignment.node is stmt and isinstance(assignment, ImportAssignment):
                if id(assignment) not in seen:
                    seen.add(id(assignment))
                    result.append(assignment)
    return result


__all__ = [
    "ParsedPython",
    "TransformContext",
    "assign_target_names",
    "binding_name",
    "find_import_assignment",
    "import_aliases",
    "import_root_binding",
    "import_statement_assignments",
    "import_symbol_name",
    "import_target_name",
]
