"""AST inventory for raw inbox mutation/construction bypasses."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path

from tests.support.gates.authority_migration import INBOX_CROSSWALK, SANCTIONED_INBOX_OPERATIONS, module_name, native_identity_errors

MUTATION_METHODS = frozenset(
    {
        "accept_custody",
        "ack",
        "close",
        "dismiss_system_notice",
        "drain_system_notices",
        "fail",
        "fetch_pending",
        "prune_inbox",
        "prune_outbox",
        "receive",
        "receive_progress_event",
        "receive_system_notice",
        "refresh_hold",
        "retire_outbox_receipt",
        "retry_custody_due",
        "retry_due",
        "submit",
        "submit_progress_event",
        "wake_outbox_recipient",
    }
)
AUTHORITY_OPERATIONS = MUTATION_METHODS | {"alarm_emitter"}

# No entire file or package is sanctioned. Exact implementation operations are
# checked against their physical method definitions and single imported host.
_OWNER_NAMES = {
    "_inbox",
    "inbox",
    "_service",
    "service",
    "sender",
    "recipient",
    "restarted",
}


@dataclass(frozen=True, order=True)
class Bypass:
    path: str
    owner: str
    operation: str

    def key(self) -> tuple[str, str, str]:
        return (self.path, self.owner, self.operation)


def _owner(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _owner(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _owner(node.func)
    return ""


def _scope(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    names: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def scan_bypasses(src_root: Path) -> tuple[Bypass, ...]:
    repo = src_root.parents[1]
    bypasses: set[Bypass] = set()
    for path in sorted(src_root.rglob("*.py")):
        label = str(path.relative_to(repo))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        service_symbols = {"InboxService"}
        service_modules: set[str] = set()
        service_providers = {
                "hyprial.inbox",
                "hyprial.daemon",
                "hyprial.daemon.impl.inbox",
                "hyprial.daemon.impl.inbox.service",
        }
        package = module_name(label)
        if not label.endswith('/__init__.py'):
            package = package.rpartition('.')[0]
        for node in ast.walk(tree):
            module = ''
            if isinstance(node, ast.ImportFrom):
                module = (resolve_name('.' * node.level + (node.module or ''), package)
                          if node.level else node.module or '')
            if isinstance(node, ast.ImportFrom) and module in service_providers:
                for alias in node.names:
                    if alias.name == "InboxService":
                        service_symbols.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and module == 'hyprial.daemon.impl.inbox.actor.internal':
                for alias in node.names:
                    if alias.name == '_ActorOwnedInboxService':
                        service_symbols.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in service_providers:
                        service_modules.add(alias.asname or alias.name)

        def is_service_constructor(call: ast.Call) -> bool:
            if isinstance(call.func, ast.Name):
                return call.func.id in service_symbols
            if isinstance(call.func, ast.Attribute) and call.func.attr == "InboxService":
                return _owner(call.func.value) in service_modules
            return False

        lexical_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

        def lexical_scope(node: ast.AST) -> ast.AST:
            current = parents.get(node)
            while current is not None:
                if isinstance(current, lexical_types):
                    return current
                current = parents.get(current)
            return tree

        def enclosing_class(node: ast.AST) -> ast.ClassDef | None:
            current: ast.AST | None = node
            while current is not None:
                if isinstance(current, ast.ClassDef):
                    return current
                current = parents.get(current)
            return None

        scopes: tuple[ast.AST, ...] = (tree,) + tuple(
            node for node in ast.walk(tree) if isinstance(node, lexical_types)
        )
        provenance_by_scope = {scope: set() for scope in scopes}
        bound_by_scope = {scope: set() for scope in scopes}
        class_fields: dict[ast.ClassDef, set[str]] = {}
        assignments: list[tuple[ast.AST, tuple[ast.expr, ...], ast.AST | None]] = []

        for scope in scopes:
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for argument in (
                    *scope.args.posonlyargs,
                    *scope.args.args,
                    *scope.args.kwonlyargs,
                ):
                    bound_by_scope[scope].add(argument.arg)
                    if isinstance(argument.annotation, ast.Name) and (
                        argument.annotation.id in service_symbols
                    ):
                        provenance_by_scope[scope].add(argument.arg)

        for node in ast.walk(tree):
            targets: tuple[ast.expr, ...] = ()
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets = tuple(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = (node.target,)
                value = node.value
            if not targets:
                continue
            scope = lexical_scope(node)
            assignments.append((scope, targets, value))
            for target in targets:
                if isinstance(target, ast.Name):
                    bound_by_scope[scope].add(target.id)
            if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name):
                if node.annotation.id in service_symbols:
                    provenance_by_scope[scope].update(
                        filter(None, (_owner(target) for target in targets))
                    )
            if isinstance(value, ast.Call) and is_service_constructor(value):
                provenance_by_scope[scope].update(
                    filter(None, (_owner(target) for target in targets))
                )
                class_scope = enclosing_class(scope)
                if class_scope is not None:
                    class_fields.setdefault(class_scope, set()).update(
                        name
                        for name in (_owner(target) for target in targets)
                        if name.startswith("self.")
                    )

        def is_provenant(owner: str, scope: ast.AST) -> bool:
            if owner in provenance_by_scope[scope]:
                return True
            root = owner.split(".", 1)[0]
            if (
                scope is not tree
                and root not in bound_by_scope[scope]
                and owner in provenance_by_scope[tree]
            ):
                return True
            class_scope = enclosing_class(scope)
            return (
                class_scope is not None
                and owner.startswith("self.")
                and owner in class_fields.get(class_scope, set())
            )

        changed = True
        while changed:
            changed = False
            for scope, targets, value in assignments:
                if value is None or not is_provenant(_owner(value), scope):
                    continue
                for target in targets:
                    name = _owner(target)
                    if name and name not in provenance_by_scope[scope]:
                        provenance_by_scope[scope].add(name)
                        changed = True
                    class_scope = enclosing_class(scope)
                    if (
                        class_scope is not None
                        and name.startswith("self.")
                        and name not in class_fields.setdefault(class_scope, set())
                    ):
                        class_fields[class_scope].add(name)
                        changed = True

        # Only close() on the exact public biz RoutineService has a known
        # non-inbox type. Unknown services retain conservative detection. This
        # is direct import/constructor/annotated-parameter evidence, not an
        # arbitrary name, path, class or package waiver. Any conflicting binding
        # or actual InboxService provenance removes this negative evidence.
        routine_aliases: set[str] = set()
        imports: dict[str, list[tuple[str, str]]] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imports.setdefault(alias.asname or alias.name, []).append((node.module or '', alias.name))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                scope = lexical_scope(node)
                if scope is not tree:
                    for alias in node.names:
                        bound_by_scope[scope].add(alias.asname or alias.name.split('.')[0])
        for alias, providers in imports.items():
            if providers == [('hyprial.biz', 'RoutineService')]:
                routine_aliases.add(alias)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                routine_aliases.discard(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                for target in targets:
                    if isinstance(target, ast.Name):
                        routine_aliases.discard(target.id)

        def routine_constructor(value: ast.AST | None, scope: ast.AST) -> bool:
            return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in routine_aliases
                    and value.func.id not in bound_by_scope[scope])

        routine_locals: dict[ast.AST, set[str]] = {scope: set() for scope in scopes}
        for scope in (scopes if routine_aliases else ()):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for argument in (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs):
                    if (isinstance(argument.annotation, ast.Name) and argument.annotation.id in routine_aliases
                            and argument.annotation.id not in bound_by_scope[scope]):
                        routine_locals[scope].add(argument.arg)
            grouped: dict[str, list[ast.AST | None]] = {}
            for assignment_scope, targets, value in assignments:
                if assignment_scope is scope:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            grouped.setdefault(target.id, []).append(value)
            for name, values in grouped.items():
                if all(routine_constructor(value, scope) for value in values):
                    routine_locals[scope].add(name)
                else:
                    routine_locals[scope].discard(name)
        routine_fields: dict[ast.ClassDef, dict[str, list[bool]]] = {}
        for scope, targets, value in (assignments if routine_aliases else ()):
            klass = enclosing_class(scope)
            if klass is None:
                continue
            known = (routine_constructor(value, scope) or isinstance(value, ast.Name)
                     and value.id in routine_locals[scope])
            for target in targets:
                name = _owner(target)
                if name.startswith('self.'):
                    routine_fields.setdefault(klass, {}).setdefault(name, []).append(known)

        def non_inbox_close(owner: str, scope: ast.AST) -> bool:
            if is_provenant(owner, scope):
                return False
            if owner in routine_locals[scope]:
                return True
            klass = enclosing_class(scope)
            evidence = routine_fields.get(klass, {}).get(owner, [])
            return bool(evidence) and all(evidence)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and is_service_constructor(node):
                bypasses.add(Bypass(label, _scope(parents, node), "construct"))
                continue
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in AUTHORITY_OPERATIONS
            ):
                owner = _owner(node.args[0])
                if node.args[1].value == 'close' and non_inbox_close(owner, lexical_scope(node)):
                    continue
                if (
                    owner.rsplit(".", 1)[-1] in _OWNER_NAMES
                    or is_provenant(owner, lexical_scope(node))
                ):
                    bypasses.add(
                        Bypass(label, _scope(parents, node), str(node.args[1].value))
                    )
                continue
            attribute = (
                node.func
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                else node if isinstance(node, ast.Attribute) else None
            )
            if attribute is None:
                continue
            operation = attribute.attr
            if operation not in AUTHORITY_OPERATIONS:
                continue
            owner = _owner(attribute.value)
            leaf = owner.rsplit(".", 1)[-1]
            scope = lexical_scope(node)
            if operation == 'close' and non_inbox_close(owner, scope):
                continue
            inbox_shaped = (
                leaf in _OWNER_NAMES
                or "._inbox" in owner
                or ".inbox" in owner
                or ".service" in owner
                or is_provenant(owner, scope)
            )
            if inbox_shaped:
                bypasses.add(Bypass(label, _scope(parents, node), operation))
    return tuple(sorted(bypasses))


def validate_manifest(src_root: Path, manifest_path: Path) -> list[str]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw['schema_version'] != 1:
        return ['manifest schema_version must be exactly integer 1']
    if raw.get('policy') != 'shrinking-only-until-root-composition':
        errors.append('manifest policy must remain shrinking-only-until-root-composition')
    if not isinstance(raw.get('exemptions'), list):
        return errors + ['manifest exemptions must be a list']
    manifested: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw['exemptions']):
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key].strip()
                                                 for key in ('path', 'owner', 'operation')):
            errors.append(f'exemptions[{index}] needs non-empty string path, owner, operation')
            continue
        key = (item['path'], item['owner'], item['operation'])
        if key in manifested:
            errors.append(f'duplicate manifest inbox exemption: {key}')
        manifested.add(key)
    actual = set()
    checked = set()
    findings = scan_bypasses(src_root)
    native_keys = {item.key() for item in findings}
    for native, canonical in INBOX_CROSSWALK.items():
        if canonical in manifested and native not in native_keys:
            errors.append(f'absent exact inbox migration site: {native}')
    for item in findings:
        native = item.key()
        identity = (item.path, item.owner)
        if identity not in checked:
            errors.extend(native_identity_errors(src_root, *identity))
            checked.add(identity)
        if native in SANCTIONED_INBOX_OPERATIONS:
            continue
        actual.add(INBOX_CROSSWALK.get(native, native))
    errors.extend([
        f"unmanifested inbox bypass: {path}:{owner}:{operation}"
        for path, owner, operation in sorted(actual - manifested)
    ])
    errors.extend(
        f"stale inbox bypass exemption must shrink: {path}:{owner}:{operation}"
        for path, owner, operation in sorted(manifested - actual)
    )
    return errors
