"""AST helpers for the T11 resident ownership tripwire (no inventory data here)."""
from __future__ import annotations
import ast
from importlib.util import resolve_name

_LOGIN_MODULES = {"hyprial.login", "hyprial.shell.login", "hyprial.shell.impl.login", "hyprial.shell.impl.login.flow", "hyprial.shell.impl.login.oidc"}
_GETTER = ast.parse('''
def __getattr__(name: str):
    try:
        module_path, symbol_name = _FACADE_EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    from importlib import import_module
    provider = import_module(module_path)
    export = provider if symbol_name is None else getattr(provider, symbol_name)
    globals()[name] = export
    return export
''').body[0]
_DIR = ast.parse("def __dir__() -> list[str]:\n    return sorted(__all__)\n").body[0]
_UNKNOWN_LITERAL = object()


def _literal(node):
    # Only literals and literal-string concatenation; no evaluation or dataflow.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return _UNKNOWN_LITERAL


def _strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for key, val in value.items() for part in (key, val) for item in _strings(part)]
    if isinstance(value, (tuple, list, set)):
        return [item for part in value for item in _strings(part)]
    return []


def _binding_only(tree):
    """Static reexports or the one literal-map public getter, never arbitrary code."""
    table, exports = None, None
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            return False
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            if not node.orelse and all(isinstance(item, (ast.Import, ast.ImportFrom)) for item in node.body):
                continue
            return False
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, _literal(node.value)
            if name == "_FACADE_EXPORTS":
                if table is not None or not isinstance(node.value, ast.Dict) or not isinstance(value, dict):
                    return False
                if len(value) != len(node.value.keys) or not all(
                    isinstance(key, str) and isinstance(provider, tuple) and len(provider) == 2 and
                    isinstance(provider[0], str) and (provider[1] is None or isinstance(provider[1], str))
                    for key, provider in value.items()
                ):
                    return False
                table = value
            elif name == "__all__":
                if exports is not None or not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
                    return False
                exports = value
            elif value is _UNKNOWN_LITERAL:
                return False
            continue
        if isinstance(node, ast.FunctionDef) and node.name in {"__getattr__", "__dir__"}:
            expected = _GETTER if node.name == "__getattr__" else _DIR
            if ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False):
                continue
        return False
    getters = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"]
    return (not getters and table is None) or (len(getters) == 1 and table is not None and exports is not None and
                                             len(exports) == len(set(exports)) and set(table) == set(exports))


def _protected_lines(source, relative, pattern):
    """Original forbidden vocabulary applied to implementation AST, not metadata."""
    tree = ast.parse(source, filename=relative)
    docstrings = {id(node.value) for parent in ast.walk(tree) if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  for node in parent.body[:1] if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}
    allowed, literal_stores, protected_names = set(docstrings), set(), set()
    login_gate = bool(pattern.search("run_login"))

    def forbidden(value):
        return any(pattern.search(item) or (login_gate and item in _LOGIN_MODULES) for item in _strings(value))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = _literal(node.value)
            if value is not _UNKNOWN_LITERAL:
                allowed.update(id(child) for child in ast.walk(node.value))
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                stored = {child.id for target in targets for child in ast.walk(target) if isinstance(child, ast.Name)}
                literal_stores.update(id(child) for target in targets for child in ast.walk(target) if isinstance(child, ast.Name))
                if forbidden(value) or any(pattern.search(name) for name in stored):
                    protected_names.update(stored)
    lines = set()
    package = "hyprial." + relative.removesuffix(".py").replace("/", ".")
    package = package.removesuffix(".__init__") if relative.endswith("/__init__.py") else package.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if id(node) in allowed:
            continue
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store) and id(node) in literal_stores:
                continue
            if pattern.search(node.id) or (isinstance(node.ctx, ast.Load) and node.id in protected_names):
                lines.add(node.lineno)
        elif isinstance(node, ast.Attribute) and pattern.search(node.attr):
            lines.add(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and pattern.search(node.name):
            lines.add(node.lineno)
        elif isinstance(node, (ast.Constant, ast.BinOp)) and forbidden(_literal(node)):
            lines.add(node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if pattern.search(ast.unparse(node)):
                lines.add(node.lineno)
            if login_gate:
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                else:
                    module = node.module or ""
                    if node.level:
                        module = resolve_name("." * node.level + module, package)
                    modules = [module] + [module + "." + alias.name for alias in node.names]
                if set(modules) & _LOGIN_MODULES:
                    lines.add(node.lineno)
    return sorted(lines)
