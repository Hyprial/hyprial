"""Meaning-level exemptions, W021 semantics and first-seed ratchet helpers.

Decision (owner 2026-11-21, W1B rule clean-up and R2 hardening):
* 21 baseline findings were not POSIX-only code but rule false positives.  They
  are recognised here by what the code means, never by file or line, so a real
  use of the same idiom elsewhere still counts.
* W011 interface example data: a POSIX path literal is example data when every
  ancestor between it and its module-level assignment is a container
  (dict/list/tuple/set), a keyword argument or a ``MethodExample*`` constructor,
  and either such a constructor is among those ancestors or the file is a
  request-catalog example module and the literal names itself synthetic.
* W012 read-only toggle: a private-mode chmod is the write-restore half of a
  read-only toggle only when its own function walks a tree without setting a
  read-only mode itself, and a distinct sibling function of the same class or
  module walks a tree and chmods it solely to a read-only mode (0o444, 0o555).
* W021 platform rejection (explicit Windows rejection is zero tolerance):
  - guard: any reachable non-bare ``raise`` inside a branch of an ``if`` (or the
    implicit post-guard tail when an ``if`` returns/continues/breaks on the
    POSIX/non-Windows side and the remaining block unconditionally raises, or
    the failure of an ``assert``) taken when the platform is Windows (including
    3-arg ``getattr(os, "name", ...)`` / ``getattr(sys, "platform", ...)``),
    when it is not a named POSIX platform, when a POSIX capability is missing,
    or when an imported POSIX-only module is ``None`` / falsy;
  - import: any reachable non-bare ``raise`` inside an ``ImportError`` handler
    of a ``try`` that imports a POSIX-only module;
  - exception / message: an explicit platform-refusal exception type or AST
    ``Name`` / ``Attribute`` / string error-code symbol (``*PlatformUnsupported*``,
    ``*PLATFORM_UNSUPPORTED*``, ``*WINDOWS_UNSUPPORTED*``, ``*POSIX_REQUIRED*``,
    etc. -- without misclassifying unguarded domain ``ipc_errors.UNSUPPORTED``),
    or static string message fragments in the raised exception that refuse the
    OS/host platform ("unsupported on this platform/host", "not supported on
    Windows", "Windows unsupported", "only available on POSIX", "this platform
    does not provide", "requires a POSIX host", etc.).  Domain messages that
    merely mention a platform or credentials ("messaging platform provenance
    requires POSIX credentials") do not match.
* First-seed ratchet: a v2 baseline carries ``seededRules`` once a new rule is
  graduated; comparing against an older v2 base allows initial keys ONLY for a
  rule id never seeded in that base, keeping every already-seeded rule strictly
  only-out (even after its count reaches zero).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

EXAMPLE_CONSTRUCTORS = frozenset({"MethodExample", "MethodExampleError", "MethodExampleSet"})
EXAMPLE_MODULE_ROOT = "src/hyprial/daemon/impl/request_catalog/"
DATA_NODES = (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.keyword)
TREE_WALKS = frozenset({"walk", "fwalk", "rglob"})
CHMODS = frozenset({"chmod", "lchmod"})
ALL_CHMODS = frozenset({"chmod", "fchmod", "lchmod"})
PRIVATE_MODES = frozenset({0o600, 0o700, 0o400, 0o500})
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
POSIX_FACT = ("platform", "posix")
WINDOWS_FACT = ("platform", "windows")
IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})
INITIAL_V2_RULES = frozenset(f"W{number:03d}" for number in (*range(1, 17), 19))
EXC_REFUSAL = re.compile(
    r"(?i)(?:"
    r"platform[_-]?(?:is[_-]?)?(?:unsupported|not[_-]?supported)"
    r"|(?:unsupported|not[_-]?supported)[_-]?(?:on[_-]?)?platform"
    r"|(?:windows|win32)[_-]?(?:is[_-]?)?(?:unsupported|not[_-]?supported|unavailable)"
    r"|(?:unsupported|not[_-]?supported|unavailable)[_-]?(?:on[_-]?)?(?:windows|win32)"
    r"|posix[_-]?(?:is[_-]?)?required"
    r"|requires[_-]?posix(?:[_-]?(?:error|host|os|platform|system|runtime))?(?:\Z|Error\Z)"
    r"|posix[_-]?only(?:[_-]?(?:error|feature|mode|operation|platform|runtime))?(?:\Z|Error\Z)"
    r")"
)
MESSAGE_REFUSAL = re.compile(
    r"(?i)"
    r"(?:unsupported|not[ _]+supported|not[ _]+available|unavailable"
    r"|not[ _]+verified|not[ _]+implemented)\s+on\s+"
    r"(?:this\s+(?:platform|host|os|system)|windows|win32)\b"
    r"|\b(?:windows|win32)\s+(?:is\s+)?(?:not[ _]+supported|unsupported|unavailable|not[ _]+available)\b"
    r"|\bonly\s+(?:available|supported)\s+on\s+posix\b"
    r"|\bthis\s+platform\s+(?:does\s+not\s+(?:provide|support)|has\s+no)\b"
    r"|\brequires\s+(?:a\s+|an\s+)?posix(?:"
    r"\s*$|\s*[.;:,)!]"
    r"|[\s-]+(?:host|system|platform|os|environment|runtime|kernel|file\b|lock|user\b"
    r"|signal|socket|pty|tty|fork|uid|gid|descriptor)"
    r")"
)
MESSAGE_KEYWORDS = frozenset({"code", "error_code", "message", "detail", "reason"})


def callee(node: ast.AST) -> str:
    """The last name of a call's function (``os.walk`` -> ``walk``), or ``""``."""

    func = node.func if isinstance(node, ast.Call) else node
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def platform_getattr(scanner: Any, node: ast.AST) -> bool:
    """``getattr(os, "name"[, default])`` or ``getattr(sys, "platform"[, default])``."""

    if not (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
        and 2 <= len(node.args) <= 3 and not node.keywords
        and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
    ):
        return False
    module = scanner.canon(node.args[0])
    return f"{module}.{node.args[1].value}" in {"os.name", "sys.platform"}


def platform_func_getattr(scanner: Any, node: ast.AST) -> bool:
    """``getattr(platform, "system"|"platform"[, default])`` used as a callable."""

    if not (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
        and 2 <= len(node.args) <= 3 and not node.keywords
        and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
    ):
        return False
    return scanner.canon(node.args[0]) == "platform" and node.args[1].value in {"system", "platform"}


def example_module(relative: str) -> bool:
    """A request-catalog module whose path names it as example data."""

    if not relative.startswith(EXAMPLE_MODULE_ROOT):
        return False
    return any("example" in part for part in relative[len(EXAMPLE_MODULE_ROOT):].split("/"))


def example_data(chain: list[ast.AST], in_example_module: bool) -> bool:
    """``chain`` is module -> ... -> literal; true for interface example data."""

    if len(chain) < 3 or not isinstance(chain[0], ast.Module):
        return False
    if not isinstance(chain[1], (ast.Assign, ast.AnnAssign)):
        return False
    constructor = False
    for ancestor in chain[2:-1]:
        if isinstance(ancestor, ast.Call) and callee(ancestor) in EXAMPLE_CONSTRUCTORS:
            constructor = True
        elif isinstance(ancestor, ast.Call) or not isinstance(ancestor, DATA_NODES):
            return False
        elif ancestor in getattr(chain[1], "targets", ()):
            return False
    return constructor or (in_example_module and "example" in str(chain[-1].value).lower())


def _chmod_modes(call: ast.Call) -> list[ast.AST]:
    return [*call.args, *(keyword.value for keyword in call.keywords if keyword.arg == "mode")]


def _read_only_mode(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Constant) and type(node.value) is int):
        return False
    return not node.value & 0o222 and bool(node.value & 0o044)


def _has_read_only_chmod(function: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Call) and callee(item) in CHMODS
        and any(_read_only_mode(arg) for arg in _chmod_modes(item))
        for item in ast.walk(function)
    )


def _has_private_chmod(function: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Call) and callee(item) in ALL_CHMODS
        and any(
            isinstance(sub, ast.Constant) and type(sub.value) is int and sub.value in PRIVATE_MODES
            for mode in _chmod_modes(item) for sub in ast.walk(mode)
        )
        for item in ast.walk(function)
    )


def walks_tree(function: ast.AST) -> bool:
    """The function body calls a tree walk (``os.walk``, ``os.fwalk``, ``Path.rglob``)."""

    return any(isinstance(item, ast.Call) and callee(item) in TREE_WALKS for item in ast.walk(function))


def read_only_toggle(function: ast.AST) -> bool:
    """The function walks a tree and chmods it solely to a read-only mode."""

    return walks_tree(function) and _has_read_only_chmod(function) and not _has_private_chmod(function)


def toggle_containers(tree: ast.Module) -> dict[int, frozenset[int]]:
    """Map ``id(module_or_class)`` to ``frozenset`` of ``id(func)`` that are pure read-only toggles."""

    containers: dict[int, frozenset[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef)):
            members = frozenset(
                id(item) for item in node.body
                if isinstance(item, FUNCTIONS) and read_only_toggle(item)
            )
            if members:
                containers[id(node)] = members
    return containers


def write_restore(chain: list[ast.AST], toggles: dict[int, frozenset[int]]) -> bool:
    """True only when a distinct sibling function is a pure read-only tree toggle."""

    for index in range(len(chain) - 1, 0, -1):
        if isinstance(chain[index], FUNCTIONS):
            function = chain[index]
            siblings = toggles.get(id(chain[index - 1]), frozenset())
            return (
                id(function) not in siblings
                and bool(siblings - {id(function)})
                and walks_tree(function)
                and not _has_read_only_chmod(function)
            )
    return False


def _posix_capability(facts: frozenset, posix_members: dict[str, dict[str, str]]) -> bool:
    return any(
        fact[0] == "cap" and (posix_members.get(fact[1], {}).get(fact[2]) or fact[2].startswith("supports_"))
        for fact in facts
    )


def _module_absent(test: ast.AST, aliases: set[str]) -> tuple[bool, bool]:
    """(a POSIX-only module is missing when ``test`` is truthy, ... when falsy)."""

    if isinstance(test, ast.Name) and test.id in aliases:
        return False, True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        true, false = _module_absent(test.operand, aliases)
        return false, true
    if (
        isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.left, ast.Name)
        and test.left.id in aliases and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None and isinstance(test.ops[0], (ast.Is, ast.IsNot))
    ):
        return (True, False) if isinstance(test.ops[0], ast.Is) else (False, True)
    return False, False


def _refusing_side(scanner: Any, test: ast.AST, posix_members: dict[str, dict[str, str]]) -> tuple[bool, bool]:
    """(taking the truthy side refuses Windows, taking the falsy side refuses Windows)."""

    true, false = scanner.facts(test)
    absent = _module_absent(test, scanner.posix_aliases)
    return (
        WINDOWS_FACT in true or POSIX_FACT in false or _posix_capability(false, posix_members) or absent[0],
        WINDOWS_FACT in false or POSIX_FACT in true or _posix_capability(true, posix_members) or absent[1],
    )


def _always_exits(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, (ast.Raise, ast.Return, ast.Break, ast.Continue)):
            return True
        if isinstance(statement, ast.If) and statement.orelse:
            if _always_exits(statement.body) and _always_exits(statement.orelse):
                return True
        elif isinstance(statement, (ast.With, ast.AsyncWith)) and _always_exits(statement.body):
            return True
        elif isinstance(statement, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            if _always_exits(statement.finalbody):
                return True
            body_exits = _always_exits(statement.body) or _always_exits(statement.orelse)
            if body_exits and all(_always_exits(handler.body) for handler in statement.handlers):
                return True
    return False


def _reachable_raises(statements: list[ast.stmt]) -> Iterator[ast.Raise]:
    for statement in statements:
        if isinstance(statement, ast.Raise):
            if statement.exc is not None:
                yield statement
            return
        if isinstance(statement, (ast.Return, ast.Break, ast.Continue)):
            return
        if isinstance(statement, ast.If):
            yield from _reachable_raises(statement.body)
            yield from _reachable_raises(statement.orelse)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            yield from _reachable_raises(statement.body)
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            yield from _reachable_raises(statement.body)
            yield from _reachable_raises(statement.orelse)
        elif isinstance(statement, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            yield from _reachable_raises(statement.body)
            for handler in statement.handlers:
                yield from _reachable_raises(handler.body)
            yield from _reachable_raises(statement.orelse)
            yield from _reachable_raises(statement.finalbody)
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                yield from _reachable_raises(case.body)
        if _always_exits([statement]):
            return


def _imports_posix_module(statement: ast.stmt, posix_modules: frozenset[str]) -> bool:
    if isinstance(statement, ast.Import):
        return any(alias.name.split(".")[0] in posix_modules for alias in statement.names)
    return isinstance(statement, ast.ImportFrom) and (statement.module or "").split(".")[0] in posix_modules


def _static_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        joined = "".join(parts)
        return [joined] if any(part != "{}" for part in parts) else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_strings(node.left)
        right = _static_strings(node.right)
        if left and right:
            return [f"{left[0]}{right[0]}"]
        return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _static_strings(node.left)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _static_strings(node.func.value)
    return []


def _always_raises(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, ast.Raise):
            return statement.exc is not None
        if isinstance(statement, (ast.Return, ast.Break, ast.Continue)):
            return False
        if isinstance(statement, ast.If) and statement.orelse:
            if _always_raises(statement.body) and _always_raises(statement.orelse):
                return True
        elif isinstance(statement, (ast.With, ast.AsyncWith)) and _always_raises(statement.body):
            return True
        if _always_exits([statement]):
            return False
    return False


def _symbol_chain(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _symbol_chain(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _symbol_refuses_platform(node: ast.AST) -> bool:
    if isinstance(node, ast.IfExp):
        return _symbol_refuses_platform(node.body) or _symbol_refuses_platform(node.orelse)
    symbol = _symbol_chain(node)
    return bool(symbol) and any(EXC_REFUSAL.search(part) for part in symbol.split("."))


def _exc_refuses_platform(exc: ast.AST) -> bool:
    target = exc.func if isinstance(exc, ast.Call) else exc
    if _symbol_refuses_platform(target) or EXC_REFUSAL.search(callee(exc)):
        return True
    if not isinstance(exc, ast.Call):
        return False
    for arg in [*exc.args, *(keyword.value for keyword in exc.keywords)]:
        if _symbol_refuses_platform(arg):
            return True
    candidates = [
        *exc.args,
        *(keyword.value for keyword in exc.keywords if keyword.arg in MESSAGE_KEYWORDS),
    ]
    for arg in candidates:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and EXC_REFUSAL.search(arg.value):
            return True
        for message in _static_strings(arg):
            if MESSAGE_REFUSAL.search(message):
                return True
    return False


def _early_exit_rejections(
    scanner: Any, node: ast.AST, posix_members: dict[str, dict[str, str]]
) -> Iterator[tuple[ast.AST, str]]:
    for field in ("body", "orelse", "finalbody"):
        statements = getattr(node, field, None)
        if not isinstance(statements, list):
            continue
        for index, statement in enumerate(statements[:-1]):
            if not (isinstance(statement, ast.If) and not statement.orelse):
                continue
            if not (_always_exits(statement.body) and not any(_reachable_raises(statement.body))):
                continue
            if not _refusing_side(scanner, statement.test, posix_members)[1]:
                continue
            tail = statements[index + 1:]
            if _always_raises(tail):
                guard = ast.unparse(statement.test)[:50]
                for raised in _reachable_raises(tail):
                    yield raised, f"if {guard}: return; raise"


def _rejections(scanner: Any, node: ast.AST, posix_members: dict[str, dict[str, str]],
                posix_modules: frozenset[str]) -> Iterator[tuple[ast.AST, str]]:
    yield from _early_exit_rejections(scanner, node, posix_members)
    if isinstance(node, ast.If):
        refuse_true, refuse_false = _refusing_side(scanner, node.test, posix_members)
        guard = ast.unparse(node.test)[:50]
        if refuse_true:
            for raised in _reachable_raises(node.body):
                yield raised, f"if {guard}: raise"
        if refuse_false:
            for raised in _reachable_raises(node.orelse):
                yield raised, f"if {guard}: ... else: raise"
    elif isinstance(node, ast.Assert) and _refusing_side(scanner, node.test, posix_members)[1]:
        yield node, f"assert {ast.unparse(node.test)[:50]}"
    elif isinstance(node, ast.Try) and any(_imports_posix_module(item, posix_modules) for item in node.body):
        for handler in node.handlers:
            names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            caught = {ast.unparse(name).rsplit(".", 1)[-1] for name in names if name is not None}
            if caught & IMPORT_ERRORS:
                for raised in _reachable_raises(handler.body):
                    yield raised, "try: import <posix module> except ImportError: raise"
    elif isinstance(node, ast.Raise) and node.exc is not None and _exc_refuses_platform(node.exc):
        yield node, f"raise {ast.unparse(node.exc)[:50]}"


def rejections(scanner: Any, node: ast.AST, posix_members: dict[str, dict[str, str]],
               posix_modules: frozenset[str]) -> list[tuple[ast.AST, str]]:
    """W021 findings first shown at ``node``: ``(raise or assert node, symbol)``, one per raise."""

    if isinstance(node, ast.Module):
        scanner.posix_aliases = {
            alias.asname or alias.name.split(".")[0]
            for item in ast.walk(node) if isinstance(item, ast.Import)
            for alias in item.names if alias.name.split(".")[0] in posix_modules
        }
        scanner.rejected = set()
    found = []
    for raised, symbol in _rejections(scanner, node, posix_members, posix_modules):
        if id(raised) not in scanner.rejected:
            scanner.rejected.add(id(raised))
            found.append((raised, symbol))
    return found


def validate_seeded_rules(
    document: dict[str, Any],
    entries: dict[tuple[str, str], dict[str, Any]],
    rules: dict[str, Any],
    no_baseline_rules: frozenset[str],
    where: str,
    error_cls: type[Exception],
) -> None:
    """Validate optional ``seededRules`` list in a baseline document."""

    if "seededRules" not in document:
        return
    raw = document["seededRules"]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise error_cls(f"{where}: seededRules must be a list of rule ids")
    if len(raw) != len(set(raw)):
        raise error_cls(f"{where}: duplicate rule id in seededRules")
    for rule in raw:
        if rule not in rules or rule in no_baseline_rules:
            raise error_cls(f"{where}: invalid seededRules entry {rule!r}")
    declared = frozenset(raw)
    missing = sorted({rule for _path, rule in entries if rule not in declared})
    if missing:
        raise error_cls(f"{where}: rules in entries missing from seededRules: {missing}")


def extract_seeded_rules(
    text: str, entries: dict[tuple[str, str], dict[str, Any]]
) -> frozenset[str]:
    """Rules already seeded in a v2 baseline (legacy v2 implies INITIAL_V2_RULES)."""

    document = json.loads(text)
    present = {rule for _path, rule in entries}
    if isinstance(document, dict) and isinstance(document.get("seededRules"), list):
        return frozenset(document["seededRules"]) | present
    return INITIAL_V2_RULES | present


def compare_baselines(
    current: dict[tuple[str, str], dict[str, Any]],
    base: dict[tuple[str, str], dict[str, Any]],
    base_seeded_rules: frozenset[str] | None = None,
) -> list[str]:
    """Only-out comparison allowing a one-time first seed of an unseeded rule id."""

    seeded = (
        (INITIAL_V2_RULES if base_seeded_rules is None else frozenset(base_seeded_rules))
        | {rule for _path, rule in base}
    )
    errors: list[str] = []
    for key, entry in sorted(current.items()):
        _path, rule = key
        if rule not in seeded:
            continue
        if key not in base:
            errors.append(f"new baseline key {key}: write the code the portable way instead")
        elif entry["count"] > base[key]["count"]:
            errors.append(f"baseline key {key} raised {base[key]['count']} -> {entry['count']}")
    return errors


def read_base_snapshot(
    root: Path,
    ref: str,
    relative_path: str,
    version: int,
    parse_fn: Any,
    error_cls: type[Exception],
) -> tuple[dict[tuple[str, str], dict[str, Any]], frozenset[str]] | None:
    """Read and parse the baseline + seeded rules at ``ref``; ``None`` on pre-v2/missing seed."""

    completed = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{relative_path}"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        if subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                          capture_output=True, check=False).returncode != 0:
            raise error_cls(f"base ref {ref!r} is not available (fetch it first)")
        return None
    try:
        parsed = parse_fn(completed.stdout, f"{ref}:{relative_path}")
    except error_cls:
        if json.loads(completed.stdout).get("version") != version:
            return None
        raise
    return parsed, extract_seeded_rules(completed.stdout, parsed)
