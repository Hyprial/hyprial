"""Import rewrite transform with binding-aware usage updates.

Supported rule kinds:

- ``import_module``: move a module or package ``old_module`` to
  ``new_module``. Rewrites ``import old``/``import old as a``/``import old.sub``,
  ``from old import ...``/``from old.sub import ...`` statements and every
  bound usage of the rewritten imports (qualified chains included).
- ``import_symbol``: move one symbol ``old_symbol`` from ``old_module`` to
  ``new_symbol`` in ``new_module``. Splits mixed ``from``-imports, rewrites
  aliased and unaliased usages, and rewrites module-object chains such as
  ``import old.mod`` + ``old.mod.Symbol``.

The transform refuses (with diagnostics) instead of guessing: shadowed
bindings, ambiguous accesses, star imports of a moved symbol, emptied
imports that would drop standalone comments, dynamic ``getattr`` on a
renamed symbol, and dangling root names all abort the rule for the
affected file.
"""

from __future__ import annotations

import libcst as cst
from libcst.metadata.scope_provider import BaseAssignment, ImportAssignment

from .common import Diagnostic, ERROR, Rule, RuleOutcome, WARNING
from .parsing import (
    import_target_name,
    ParsedPython,
    TransformContext,
    binding_name,
    import_statement_assignments,
    import_symbol_name,
    import_aliases,
    import_root_binding,
)

KINDS = ("import_module", "import_symbol")
_REFLECT_FUNCS = {"getattr", "hasattr", "setattr", "delattr"}


class _Refusal(Exception):
    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


def _dotted(name: str) -> cst.BaseExpression:
    parts = name.split(".")
    node: cst.BaseExpression = cst.Name(parts[0])
    for part in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


def _comma_has_comment(comma: cst.Comma) -> bool:
    """True when a comma's trailing whitespace owns a comment worth keeping."""
    ws = comma.whitespace_after
    if isinstance(ws, cst.TrailingWhitespace):
        return ws.comment is not None
    if isinstance(ws, cst.ParenthesizedWhitespace):
        return ws.first_line.comment is not None or any(
            line.comment is not None for line in ws.empty_lines
        )
    return False


def dotted_of(node: cst.CSTNode) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        prefix = dotted_of(node.value)
        return f"{prefix}.{node.attr.value}" if prefix else None
    return None


def _make_import(module: str, asname: str | None) -> cst.Import:
    alias = cst.ImportAlias(
        name=_dotted(module),
        asname=cst.AsName(name=cst.Name(asname)) if asname else None,
    )
    return cst.Import(names=[alias])


def _make_from(level: int, module: str | None, symbol: str, asname: str | None) -> cst.ImportFrom:
    alias = cst.ImportAlias(
        name=cst.Name(symbol),
        asname=cst.AsName(name=cst.Name(asname)) if asname else None,
    )
    return cst.ImportFrom(
        module=_dotted(module) if module else None,
        relative=[cst.Dot() for _ in range(level)],
        names=[alias],
    )


def _module_bound(parsed: ParsedPython, module: str) -> bool:
    stack = [parsed.module]
    while stack:
        node = stack.pop()
        if isinstance(node, cst.Import):
            for alias in import_aliases(node):
                if import_target_name(alias) == module:
                    return True
        for child in node.children:
            stack.append(child)
    return False


def _insert_module_imports(code: str, modules: list[str]) -> str:
    lines = code.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or not lines[i].strip()):
        i += 1
    if i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            if stripped.strip().count(quote) >= 2 and len(stripped.strip()) > 3:
                i += 1
            else:
                i += 1
                while i < len(lines) and quote not in lines[i]:
                    i += 1
                i += 1
    while i < len(lines) and lines[i].lstrip().startswith("from __future__"):
        i += 1
    newline = "\r\n" if "\r\n" in code[:2000] else "\n"
    block = "".join(f"import {m}{newline}" for m in modules)
    return "".join(lines[:i]) + block + "".join(lines[i:])


class _FilePlanner:
    def __init__(
        self,
        parsed: ParsedPython,
        old_module: str,
        new_module: str,
        old_symbol: str | None,
        new_symbol: str | None,
        ctx: TransformContext,
    ) -> None:
        self.parsed = parsed
        self.old_module = old_module
        self.new_module = new_module
        self.old_symbol = old_symbol
        self.new_symbol = new_symbol
        self.mode = "module" if old_symbol in (None, "") else "symbol"
        self.ctx = ctx
        self.package = ctx.workspace.index.file_package(parsed.path)
        self.stmt_edits: dict[int, dict[str, list[cst.BaseSmallStatement]]] = {}
        self.name_map: dict[int, cst.Name] = {}
        self.chain_map: dict[int, cst.BaseExpression] = {}
        self.string_map: dict[int, cst.SimpleString] = {}
        self.ensure_imports: set[str] = set()
        self.matched_statements = 0
        self.chain_sites = 0
        self.warnings: list[Diagnostic] = []
        self._from_bindings: set[str] = set()
        self._dangling_roots: set[str] = set()
        self._parents: dict[int, cst.CSTNode] = {}

    # -- helpers ---------------------------------------------------------
    def _resolve_from_base(self, stmt: cst.ImportFrom) -> str | None:
        level = len(stmt.relative)
        module = None
        if isinstance(stmt.module, cst.Name):
            module = stmt.module.value
        elif isinstance(stmt.module, cst.Attribute):
            module = dotted_of(stmt.module)
        return self.ctx.workspace.index.resolve_relative(self.package, level, module)

    def _refuse(self, code: str, message: str, line: int | None = None) -> None:
        raise _Refusal(code, message, line)

    def _import_matches(self, name: str) -> bool:
        if self.mode == "module":
            return name == self.old_module or name.startswith(self.old_module + ".")
        return name == self.old_module

    def _rewrite_module_name(self, name: str) -> str:
        if name == self.old_module:
            return self.new_module
        return self.new_module + name[len(self.old_module) :]

    def _iter_import_statements(self):
        stack = [self.parsed.module]
        while stack:
            node = stack.pop()
            if isinstance(node, (cst.Import, cst.ImportFrom)):
                yield node
            for child in node.children:
                stack.append(child)

    # -- import statements -------------------------------------------------
    def plan(self) -> None:
        self._parents = self.parsed.build_parent_map()
        self._plan_import_statements()
        self._plan_usages()
        self._check_reflection()
        self._check_dangling_roots()

    def _plan_import_statements(self) -> None:
        for stmt in list(self._iter_import_statements()):
            if isinstance(stmt, cst.Import):
                self._plan_import(stmt)
            elif isinstance(stmt, cst.ImportFrom):
                self._plan_import_from(stmt)

    def _plan_import(self, stmt: cst.Import) -> None:
        matched = any(self._import_matches(import_target_name(a)) for a in import_aliases(stmt))
        if not matched:
            return
        if self.mode == "symbol":
            for alias in import_aliases(stmt):
                if import_target_name(alias) == self.old_module and alias.asname is not None:
                    self._aliased_module_binding = alias.asname.name.value
            return
        self.matched_statements += 1
        new_aliases = [
            alias.with_changes(name=_dotted(self._rewrite_module_name(import_target_name(alias))))
            if self._import_matches(import_target_name(alias))
            else alias
            for alias in import_aliases(stmt)
        ]
        self.stmt_edits[id(stmt)] = {"inline": [stmt.with_changes(names=new_aliases)], "newline": []}

    _aliased_module_binding: str | None = None

    def _plan_import_from(self, stmt: cst.ImportFrom) -> None:
        base = self._resolve_from_base(stmt)
        if isinstance(stmt.names, cst.ImportStar):
            matched = base == self.old_module or (
                self.mode == "module" and base is not None and base.startswith(self.old_module + ".")
            )
            if matched:
                if self.mode == "symbol":
                    self._refuse(
                        "STAR-IMPORT-SYMBOL",
                        f"cannot move symbol {self.old_symbol!r} out of a star import of "
                        f"{self.old_module}",
                        self.parsed.line_of(stmt),
                    )
                self.matched_statements += 1
                self.stmt_edits[id(stmt)] = {
                    "inline": [self._rebase_from(stmt, self._rewrite_module_name(base), stmt)],
                    "newline": [],
                }
            return
        if base is None:
            return
        subpackage = self.mode == "module" and base.startswith(self.old_module + ".")
        if base != self.old_module and not subpackage:
            return
        if self.mode == "module":
            self.matched_statements += 1
            self.stmt_edits[id(stmt)] = {
                "inline": [self._rebase_from(stmt, self._rewrite_module_name(base), stmt)],
                "newline": [],
            }
            return
        matched_names = [a for a in import_aliases(stmt) if import_symbol_name(a) == self.old_symbol]
        if not matched_names:
            return
        self.matched_statements += 1
        removed = matched_names[0]
        remaining = [a for a in import_aliases(stmt) if import_symbol_name(a) != self.old_symbol]
        alias_name = (
            removed.asname.name.value if removed.asname is not None else None
        )
        inline: list[cst.BaseSmallStatement] = []
        newline: list[cst.BaseSmallStatement] = []
        if remaining:
            if stmt.rpar is not None and stmt.rpar is not cst.MaybeSentinel.DEFAULT:
                # Parenthesized: trailing commas are legal and may carry a
                # trailing comment. When the removed alias owned such a comma,
                # keep that comma (with its comment) on the new last alias so
                # the comment survives the split.
                removed_comma = removed.comma
                if (
                    not isinstance(removed_comma, cst.MaybeSentinel)
                    and _comma_has_comment(removed_comma)
                ):
                    remaining[-1] = remaining[-1].with_changes(comma=removed_comma)
            else:
                # Non-parenthesized: a trailing comma after the last remaining
                # alias is a syntax error, so the new last alias drops its comma.
                remaining[-1] = remaining[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
            inline.append(stmt.with_changes(names=list(remaining)))
        else:
            self._check_removable_comments(stmt)
        if len(stmt.relative) > 0:
            level, module = self.ctx.workspace.index.relative_form(self.package, self.new_module)
        else:
            level, module = 0, self.new_module
        newline.append(_make_from(level, module, self.new_symbol, alias_name))
        self.stmt_edits[id(stmt)] = {"inline": inline, "newline": newline}
        self._from_bindings.add(alias_name if alias_name else self.old_symbol)

    def _rebase_from(
        self, stmt: cst.ImportFrom, new_base: str, original: cst.ImportFrom
    ) -> cst.ImportFrom:
        if len(original.relative) == 0:
            return stmt.with_changes(module=_dotted(new_base), relative=[])
        level, module = self.ctx.workspace.index.relative_form(self.package, new_base)
        return stmt.with_changes(
            module=_dotted(module) if module else None,
            relative=[cst.Dot() for _ in range(level)],
        )

    def _check_removable_comments(self, stmt: cst.ImportFrom) -> None:
        lines = self.parsed.source.splitlines()
        start = self.parsed.line_of(stmt)
        end = self.parsed.positions()[stmt].end.line
        for lineno in range(start, end + 1):
            stripped = lines[lineno - 1].strip()
            if stripped.startswith("#"):
                self._refuse(
                    "COMMENT-ONLY-REMOVAL-UNSUPPORTED",
                    f"removing the emptied import would drop a standalone comment at line "
                    f"{lineno}; rewrite the import list manually or keep one symbol",
                    lineno,
                )

    # -- usages -------------------------------------------------------------
    def _plan_usages(self) -> None:
        targets: list[tuple[list[BaseAssignment], str, str]] = []
        for stmt in self._iter_import_statements():
            if isinstance(stmt, cst.Import):
                for alias in import_aliases(stmt):
                    if not self._import_matches(import_target_name(alias)):
                        continue
                    if self.mode == "symbol" and import_target_name(alias) != self.old_module:
                        continue
                    assignments = import_statement_assignments(self.parsed, stmt, alias)
                    if not assignments:
                        continue
                    binding = import_root_binding(stmt, alias)
                    kind = "aliased" if alias.asname is not None else "module"
                    targets.append((assignments, binding, kind))
            elif isinstance(stmt, cst.ImportFrom):
                if isinstance(stmt.names, cst.ImportStar):
                    continue
                base = self._resolve_from_base(stmt)
                if base != self.old_module:
                    continue
                for alias in import_aliases(stmt):
                    if self.mode == "symbol" and import_symbol_name(alias) != self.old_symbol:
                        continue
                    assignments = import_statement_assignments(self.parsed, stmt, alias)
                    if not assignments:
                        continue
                    targets.append((assignments, binding_name(alias), "from"))

        seen: set[int] = set()
        for assignments, binding, kind in targets:
            for asg in assignments:
                for access in asg.references:
                    if id(access.node) in seen:
                        continue
                    seen.add(id(access.node))
                    self._plan_access(access, binding, kind)

    def _plan_access(self, access, binding: str, kind: str) -> None:
        node = access.node
        root = node
        while True:
            parent = self._parents.get(id(root))
            if isinstance(parent, cst.Attribute) and parent.value is root:
                root = parent
            else:
                break
        chain = dotted_of(root)
        if chain is None:
            return
        referents = set(access.referents)
        line = self.parsed.line_of(node)

        if self.mode == "module":
            if kind == "aliased":
                return  # alias keeps naming the moved module; nothing to rewrite
            root_name = chain.split(".")[0]
            root_node = node
            while isinstance(root_node, cst.Attribute):
                root_node = root_node.value
            shadowed = [
                r
                for r in self.parsed.assignments_for_name(root_node, root_name)
                if not isinstance(r, ImportAssignment)
            ]
            if shadowed:
                self._refuse(
                    "SHADOWED-BINDING",
                    f"name {root_name!r} is shadowed by a non-import binding here; cannot prove "
                    "this usage refers to the moved module",
                    line,
                )
            if chain == self.old_module or chain.startswith(self.old_module + "."):
                if id(root) not in self.chain_map:
                    self.chain_map[id(root)] = _dotted(
                        self.new_module + chain[len(self.old_module) :]
                    )
                    self.chain_sites += 1
            else:
                self._dangling_roots.add(chain.split(".")[0])
            return

        # symbol mode
        if kind == "from":
            if chain != binding and not chain.startswith(binding + "."):
                return
            if len(referents) > 1:
                self._refuse(
                    "AMBIGUOUS-BINDING",
                    f"name {binding!r} has {len(referents)} possible bindings; refusing to "
                    "rewrite the moved symbol usage",
                    line,
                )
            local_new = self.new_symbol if binding == self.old_symbol else binding
            if isinstance(node, cst.Name) and node.value != local_new:
                self.name_map[id(node)] = cst.Name(local_new)
            return
        if kind == "aliased":
            old_prefix = binding + "." + str(self.old_symbol)
        else:
            old_prefix = f"{self.old_module}.{self.old_symbol}"
        new_prefix = f"{self.new_module}.{self.new_symbol}"
        if chain == old_prefix or chain.startswith(old_prefix + "."):
            root_name = chain.split(".")[0]
            root_node = node
            while isinstance(root_node, cst.Attribute):
                root_node = root_node.value
            shadowed = [
                r
                for r in self.parsed.assignments_for_name(root_node, root_name)
                if not isinstance(r, ImportAssignment)
            ]
            if shadowed:
                self._refuse(
                    "SHADOWED-BINDING",
                    f"chain root {root_name!r} is shadowed by a non-import binding; "
                    "cannot prove this usage refers to the moved module",
                    line,
                )
            if id(root) not in self.chain_map:
                self.chain_map[id(root)] = _dotted(new_prefix + chain[len(old_prefix) :])
                self.chain_sites += 1
                self.ensure_imports.add(self.new_module)
        # chains naming other attributes of the old module stay untouched

    def _check_reflection(self) -> None:
        """getattr-family calls on a renamed symbol must be provable or refused."""
        if self.mode != "symbol" or self.new_symbol == self.old_symbol:
            return
        stack = [self.parsed.module]
        while stack:
            node = stack.pop()
            if isinstance(node, cst.Call) and isinstance(node.func, cst.Name) and node.func.value in _REFLECT_FUNCS:
                if node.args:
                    arg0 = node.args[0].value
                    if isinstance(arg0, cst.Name) and arg0.value in self._from_bindings:
                        arg1 = node.args[1].value if len(node.args) > 1 else None
                        if isinstance(arg1, cst.SimpleString):
                            if arg1.raw_value == self.old_symbol:
                                self.string_map[id(arg1)] = arg1.with_changes(
                                    value=arg1.value.replace(
                                        self.old_symbol, self.new_symbol
                                    )
                                )
                            # a literal that names something else is untouched
                        else:
                            self._refuse(
                                "REFLECT-ACCESS",
                                f"{node.func.value}() uses renamed symbol {self.old_symbol!r} "
                                "with a non-literal attribute name; refusing to guess",
                                self.parsed.line_of(node),
                            )
            for child in node.children:
                stack.append(child)

    def _check_dangling_roots(self) -> None:
        if not self._dangling_roots:
            return
        for root in sorted(self._dangling_roots):
            still_bound = False
            for stmt in self._iter_import_statements():
                if isinstance(stmt, cst.Import):
                    if any(
                        import_root_binding(stmt, a) == root
                        and not self._import_matches(import_target_name(a))
                        for a in import_aliases(stmt)
                    ):
                        still_bound = True
                elif isinstance(stmt, cst.ImportFrom) and not isinstance(stmt.names, cst.ImportStar):
                    if any(binding_name(a) == root for a in import_aliases(stmt)):
                        still_bound = True
            if self.new_module.split(".")[0] == root:
                still_bound = True
            if still_bound:
                self.warnings.append(
                    Diagnostic(
                        "DANGLING-ROOT-MITIGATED",
                        f"root name {root!r} stays bound by other imports after the rewrite",
                        self.parsed.path,
                        None,
                        WARNING,
                    )
                )
            else:
                self._refuse(
                    "DANGLING-ROOT",
                    f"usages of root name {root!r} outside {self.old_module} would break "
                    f"because no other import keeps it bound after moving to {self.new_module}",
                )

    # -- rendering -----------------------------------------------------------
    def render(self) -> str:
        if not (self.stmt_edits or self.name_map or self.chain_map or self.string_map):
            return self.parsed.source

        line_inline: dict[int, dict[int, list[cst.BaseSmallStatement]]] = {}
        line_new: dict[int, list[cst.BaseSmallStatement]] = {}
        suite_inline: dict[int, dict[int, list[cst.BaseSmallStatement]]] = {}
        suite_new: dict[int, list[cst.BaseSmallStatement]] = {}
        for stmt_id, payload in self.stmt_edits.items():
            parent = self._parents.get(stmt_id)
            if isinstance(parent, cst.SimpleStatementLine):
                line_inline.setdefault(id(parent), {})[stmt_id] = payload["inline"]
                if payload["newline"]:
                    line_new.setdefault(id(parent), []).extend(payload["newline"])
            elif isinstance(parent, cst.SimpleStatementSuite):
                suite_inline.setdefault(id(parent), {})[stmt_id] = payload["inline"]
                if payload["newline"]:
                    suite_new.setdefault(id(parent), []).extend(payload["newline"])
            else:
                self._refuse(
                    "UNSUPPORTED-STATEMENT-CONTEXT",
                    "matched import is not inside a simple statement line or suite",
                )

        name_map = self.name_map
        chain_map = self.chain_map
        string_map = self.string_map

        class FinalRewriter(cst.CSTTransformer):
            def visit_Name(self, node: cst.Name) -> bool:
                return id(node) not in name_map

            def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
                return name_map.get(id(original_node), updated_node)

            def visit_Attribute(self, node: cst.Attribute) -> bool:
                return id(node) not in chain_map

            def leave_Attribute(
                self, original_node: cst.Attribute, updated_node: cst.Attribute
            ) -> cst.BaseExpression:
                return chain_map.get(id(original_node), updated_node)

            def leave_SimpleString(
                self, original_node: cst.SimpleString, updated_node: cst.SimpleString
            ) -> cst.SimpleString:
                return string_map.get(id(original_node), updated_node)

            def leave_SimpleStatementLine(self, original_node, updated_node):
                if id(original_node) not in line_inline:
                    return updated_node
                edits = line_inline[id(original_node)]
                new_body: list[cst.BaseSmallStatement] = []
                for small in original_node.body:
                    if id(small) in edits:
                        new_body.extend(edits[id(small)])
                    else:
                        new_body.append(small)
                new_lines = line_new.get(id(original_node), [])
                if not new_body:
                    if new_lines:
                        return cst.FlattenSentinel(
                            [
                                cst.SimpleStatementLine(
                                    leading_lines=original_node.leading_lines,
                                    body=list(new_lines),
                                    trailing_whitespace=original_node.trailing_whitespace,
                                )
                            ]
                        )
                    return cst.RemoveFromParent()
                rebuilt = original_node.with_changes(body=new_body)
                if new_lines:
                    extras = [
                        cst.SimpleStatementLine(
                            leading_lines=original_node.leading_lines,
                            body=[small],
                            trailing_whitespace=original_node.trailing_whitespace,
                        )
                        for small in new_lines
                    ]
                    return cst.FlattenSentinel([rebuilt, *extras])
                return rebuilt

            def leave_SimpleStatementSuite(self, original_node, updated_node):
                if id(original_node) not in suite_inline:
                    return updated_node
                edits = suite_inline[id(original_node)]
                new_body: list[cst.BaseSmallStatement] = []
                for small in original_node.body:
                    if id(small) in edits:
                        new_body.extend(edits[id(small)])
                    else:
                        new_body.append(small)
                extra = suite_new.get(id(original_node), [])
                if not new_body:
                    if extra:
                        new_body = list(extra)
                    else:
                        return original_node.with_changes(body=[cst.Pass()])
                if extra:
                    new_body = [
                        *new_body[:-1],
                        new_body[-1].with_changes(semicolon=cst.Semicolon()),
                        *extra,
                    ]
                return original_node.with_changes(body=new_body)

        new_mod = self.parsed.module.visit(FinalRewriter())
        code = new_mod.code
        ensure = sorted(m for m in self.ensure_imports if not _module_bound(self.parsed, m))
        if ensure:
            code = _insert_module_imports(code, ensure)
        return code


def _count_new_form(
    parsed: ParsedPython, new_module: str, new_symbol: str | None, ctx: TransformContext
) -> int:
    count = 0
    package = ctx.workspace.index.file_package(parsed.path)
    stack = [parsed.module]
    while stack:
        node = stack.pop()
        if isinstance(node, cst.Import):
            for alias in import_aliases(node):
                if import_target_name(alias) == new_module:
                    count += 1
        elif isinstance(node, cst.ImportFrom) and not isinstance(node.names, cst.ImportStar):
            level = len(node.relative)
            module = None
            if isinstance(node.module, cst.Name):
                module = node.module.value
            elif isinstance(node.module, cst.Attribute):
                module = dotted_of(node.module)
            base = ctx.workspace.index.resolve_relative(package, level, module)
            if base == new_module:
                if new_symbol is None or any(
                    import_target_name(a) == new_symbol for a in import_aliases(node)
                ):
                    count += 1
        for child in node.children:
            stack.append(child)
    return count


def apply_rule(rule: Rule, ctx: TransformContext) -> RuleOutcome:
    ws = ctx.workspace
    old_module = str(rule.get("old_module"))
    new_module = str(rule.get("new_module"))
    old_symbol = rule.get("old_symbol")
    new_symbol = rule.get("new_symbol")
    symbol_mode = old_symbol not in (None, "")
    outcome = RuleOutcome(
        rule_id=rule.rule_id,
        kind=rule.kind,
        stage=rule.stage,
        status="refused",
        expected_matches=rule.expected_matches,
    )
    refusals: list[Diagnostic] = []
    total_matches = 0
    total_new_form = 0
    changed: list[str] = []
    last_seg = old_module.rsplit(".", 1)[-1].encode()
    new_bytes = new_module.encode()

    scope_excluded_snapshot = list(ctx.scope_excluded)
    for path in ws.iter_python():
        if not ctx.writable(path):
            continue
        data = ws.read(path)
        if old_module.encode() not in data and last_seg not in data and new_bytes not in data:
            continue
        try:
            parsed = ctx.parse(path)
            planner = _FilePlanner(parsed, old_module, new_module, old_symbol, new_symbol, ctx)
            planner.plan()
            if planner.stmt_edits or planner.name_map or planner.chain_map or planner.string_map:
                new_code = planner.render()
                if new_code != parsed.source:
                    ws.write(path, new_code.encode(parsed.encoding))
                    changed.append(path)
            total_matches += planner.matched_statements + planner.chain_sites
            total_new_form += _count_new_form(
                parsed, new_module, str(new_symbol) if symbol_mode else None, ctx
            )
            outcome.diagnostics.extend(planner.warnings)
        except _Refusal as exc:
            refusals.append(Diagnostic(exc.code, exc.message, path, exc.line, ERROR))
            continue

    outcome.matches = total_matches
    outcome.new_form_count = total_new_form
    outcome.changed_paths = changed
    outcome.detail["scope_excluded"] = [
        row for row in ctx.scope_excluded if row not in scope_excluded_snapshot
    ]
    if refusals:
        outcome.diagnostics = refusals + outcome.diagnostics
        outcome.status = "refused"
        return outcome
    if total_matches == 0:
        if total_new_form > 0:
            outcome.status = "already_done"
        elif outcome.detail.get("scope_excluded"):
            outcome.diagnostics.append(
                Diagnostic(
                    "SCOPE-EXHAUSTED",
                    f"expected {rule.expected_matches} import sites for {old_module}; every "
                    "candidate site is outside the writable scope (see detail.scope_excluded). "
                    "Out-of-scope consumers need an approved compatibility decision, not a "
                    "silent rewrite.",
                    None,
                    None,
                    ERROR,
                )
            )
        else:
            outcome.diagnostics.append(
                Diagnostic(
                    "EXPECTED-MATCHES",
                    f"expected {rule.expected_matches} import sites for {old_module} but found "
                    "0, and no migrated form is present",
                    None,
                    None,
                    ERROR,
                )
            )
        return outcome
    if total_matches != rule.expected_matches:
        outcome.status = "refused"
        outcome.diagnostics.append(
            Diagnostic(
                "MATCH-DRIFT",
                f"expected {rule.expected_matches} import sites but found {total_matches} "
                f"for {old_module} -> {new_module}",
                None,
                None,
                ERROR,
            )
        )
        return outcome
    outcome.status = "applied"
    return outcome


__all__ = ["KINDS", "apply_rule"]
