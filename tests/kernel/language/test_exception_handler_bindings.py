"""AST guard: every named exception handler must resolve at runtime."""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]
PYTHON_ROOTS = (".agents", "contract", "scripts", "spikes", "src", "tests")
BUILTIN_NAMES = frozenset(dir(builtins))


@dataclass(eq=False)
class _Scope:
    kind: str
    parent: _Scope | None
    bindings: set[str] = field(default_factory=set)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)
    handlers: list[tuple[int, str]] = field(default_factory=list)
    has_star_import: bool = False


class _ExceptionBindingScanner(ast.NodeVisitor):
    def __init__(self, tree: ast.AST) -> None:
        self.root = _Scope("module", None)
        self.scope = self.root
        self.scopes = [self.root]
        self.visit(tree)

    def _visit_child_scope(
        self,
        kind: str,
        body: list[ast.stmt],
        arguments: ast.arguments | None = None,
    ) -> None:
        parent = self.scope
        self.scope = _Scope(kind, parent)
        self.scopes.append(self.scope)
        if arguments is not None:
            args = (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
            self.scope.bindings.update(argument.arg for argument in args)
            if arguments.vararg is not None:
                self.scope.bindings.add(arguments.vararg.arg)
            if arguments.kwarg is not None:
                self.scope.bindings.add(arguments.kwarg.arg)
        for statement in body:
            self.visit(statement)
        self.scope = parent

    def visit_Import(self, node: ast.Import) -> None:
        self.scope.bindings.update(
            alias.asname or alias.name.split(".")[0] for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.scope.has_star_import = True
            else:
                self.scope.bindings.add(alias.asname or alias.name)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.nonlocals.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.bindings.add(node.name)
        expressions = (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults)
        for expression in expressions:
            if expression is not None:
                self.visit(expression)
        self._visit_child_scope("function", node.body, node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.bindings.add(node.name)
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._visit_child_scope("class", node.body)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.bindings.add(node.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            for candidate in ast.walk(node.type):
                if isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Load):
                    self.scope.handlers.append((candidate.lineno, candidate.id))
            self.visit(node.type)
        if node.name is not None:
            self.scope.bindings.add(node.name)
        for statement in node.body:
            self.visit(statement)


def _resolves(scope: _Scope, name: str) -> bool:
    if name in BUILTIN_NAMES:
        return True
    if name in scope.globals:
        root = scope
        while root.parent is not None:
            root = root.parent
        return name in root.bindings or root.has_star_import

    origin_kind = scope.kind
    first = True
    while scope is not None:
        # A function nested in a class does not close over the class namespace.
        skip_class = not first and origin_kind == "function" and scope.kind == "class"
        if not skip_class and name not in scope.globals:
            if name in scope.bindings or scope.has_star_import:
                return True
        scope = scope.parent
        first = False
    return False


def test_named_exception_handlers_are_bound() -> None:
    offenders: list[str] = []
    for root_name in PYTHON_ROOTS:
        for path in sorted((REPOSITORY / root_name).rglob("*.py")):
            try:
                source = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Other tests create and remove probe trees under tests/.
                continue
            tree = ast.parse(source, filename=str(path))
            scanner = _ExceptionBindingScanner(tree)
            for scope in scanner.scopes:
                for line, name in scope.handlers:
                    if not _resolves(scope, name):
                        relative = path.relative_to(REPOSITORY)
                        offenders.append(
                            f"{relative}:{line}: except references unbound {name}"
                        )

    assert not offenders, "unbound names in exception handlers:\n" + "\n".join(offenders)
