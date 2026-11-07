"""File, resource and directory relocation with consumer synchronization.

Rule kind ``layout_move`` relocates files or whole directories in the
simulated/real tree. Before any move it verifies: target conflicts,
case-insensitive (Windows-style) collisions against the target directory
listing, path escapes, and symlinked sources or target parents. Python
files that move have their relative imports rebased from the old package to
the new one; consumer modules are rewritten through the imports engine via
``sync_imports``; non-Python consumers are updated through the references
engine via ``reference_updates``; authority manifests keep every entry and
reason and only migrate matched ``path`` values via ``manifest_updates``.
Files marked ``worm: true`` must keep their content hash across the move.
"""

from __future__ import annotations

from .common import Diagnostic, ERROR, Rule, RuleOutcome, ToolError, normalize_rel, sha256_hex
from .parsing import TransformContext
from . import imports as imports_engine
from . import references as references_engine

KINDS = ("layout_move",)


class _Refusal(Exception):
    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


def _expand_moves(ws, moves: list[dict]) -> list[dict]:
    expanded = []
    for move in moves:
        src = normalize_rel(str(move["from"]))
        dst = normalize_rel(str(move["to"]))
        if not ws.exists(src) and not (ws.root / src).exists():
            raise _Refusal("MISSING-SOURCE", f"cannot move missing path {src}")
        if src == dst:
            raise _Refusal("NOOP-MOVE", f"move source equals target: {src}")
        # target conflicts (exact and case-insensitive) are judged in
        # _validate_targets against the case-sensitive directory listing
        src_is_dir = _is_dir(ws, src)
        if src_is_dir:
            children = [
                p for p in ws.paths() if p.startswith(src + "/")
            ]
            if not children:
                raise _Refusal("EMPTY-DIR-MOVE", f"directory {src} has no tracked files")
            for child in children:
                expanded.append(
                    {
                        "from": child,
                        "to": dst + child[len(src) :],
                        "worm": bool(move.get("worm")),
                        "from_sha256": move.get("sha256"),
                    }
                )
        else:
            expanded.append(
                {
                    "from": src,
                    "to": dst,
                    "worm": bool(move.get("worm")),
                    "from_sha256": move.get("sha256"),
                }
            )
    return expanded


def _is_dir(ws, rel: str) -> bool:
    if any(p.startswith(rel + "/") for p in ws.paths()):
        return True
    return (ws.root / rel).is_dir()


def _validate_targets(ws, expanded: list[dict]) -> None:
    seen_targets: dict[str, str] = {}
    for move in expanded:
        src, dst = move["from"], move["to"]
        if src in seen_targets and seen_targets[src] != dst:
            raise _Refusal("TARGET-CONFLICT", f"conflicting moves for {src}")
        seen_targets[src] = dst
        parent = dst.rsplit("/", 1)[0] if "/" in dst else ""
        name = dst.rsplit("/", 1)[-1]
        # symlink checks first: they change what a listing would even mean
        if _is_symlink(ws, src):
            raise _Refusal("SYMLINK-SOURCE", f"refusing to move symlink {src}")
        if parent:
            parts = parent.split("/")
            for depth in range(1, len(parts) + 1):
                ancestor = "/".join(parts[:depth])
                if _is_symlink(ws, ancestor):
                    raise _Refusal(
                        "SYMLINK-TARGET-PARENT",
                        f"move target parent {ancestor} is a symlink",
                    )
        # exact + case-insensitive collisions judged case-sensitively against
        # the directory listing: on case-insensitive filesystems Path.is_file()
        # would wrongly report a different-case target as existing
        listing = ws.list_dir(parent) if parent else _root_names(ws)
        if name in listing:
            raise _Refusal("TARGET-CONFLICT", f"move target {dst} already exists")
        for existing in listing:
            if existing != name and existing.casefold() == name.casefold():
                raise _Refusal(
                    "CASE-COLLISION",
                    f"target {dst} collides case-insensitively with existing "
                    f"{existing!r} (Windows-hostile)",
                )


def _root_names(ws) -> set[str]:

    names = set()
    for path in ws.paths():
        head = path.split("/", 1)[0]
        names.add(head)
    return names


def _is_symlink(ws, rel: str) -> bool:

    return (ws.root / rel).is_symlink() if not ws.is_dirty(rel) else False


def _rebase_relative_imports(ctx: TransformContext, path: str, old_package: str) -> int:
    """Rebase the moved file's relative imports onto its new package."""
    import libcst as cst


    text, encoding = ctx.read_text(path)
    parsed = ctx.parse(path)
    index = ctx.workspace.index
    new_package = index.file_package(path)
    if new_package == old_package:
        return 0

    def resolve(stmt: cst.ImportFrom) -> str | None:
        level = len(stmt.relative)
        module = None
        if isinstance(stmt.module, cst.Name):
            module = stmt.module.value
        elif isinstance(stmt.module, cst.Attribute):
            parts = []
            node = stmt.module
            while isinstance(node, cst.Attribute):
                parts.append(node.attr.value)
                node = node.value
            if isinstance(node, cst.Name):
                parts.append(node.value)
            module = ".".join(reversed(parts))
        return index.resolve_relative(old_package, level, module)

    class Rebaser(cst.CSTTransformer):
        def leave_ImportFrom(self, original_node, updated_node):
            if not original_node.relative:
                return updated_node
            base = resolve(original_node)
            if base is None:
                return updated_node
            level, module = index.relative_form(new_package, base)
            return updated_node.with_changes(
                module=cst.Name(module) if module and "." not in module
                else _dotted_or_none(module),
                relative=[cst.Dot() for _ in range(level)],
            )

    new_module = parsed.module.visit(Rebaser())
    new_text = new_module.code
    if new_text != text:
        ctx.write_text(path, new_text, encoding)
        return 1
    return 0


def _dotted_or_none(module: str | None):
    import libcst as cst

    if not module:
        return None
    parts = module.split(".")
    node = cst.Name(parts[0])
    for part in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


def apply_rule(rule: Rule, ctx: TransformContext) -> RuleOutcome:
    ws = ctx.workspace
    moves = list(rule.get("moves") or [])
    sync_imports = list(rule.get("sync_imports") or [])
    reference_updates = list(rule.get("reference_updates") or [])
    manifest_updates = list(rule.get("manifest_updates") or [])
    outcome = RuleOutcome(
        rule_id=rule.rule_id,
        kind=rule.kind,
        stage=rule.stage,
        status="refused",
        expected_matches=rule.expected_matches,
    )
    try:
        # scope: WORM sources may only move byte-preserving (worm+hash pin);
        # targets in read-only scopes are refused outright
        from .scope_policy import classify_path

        for move in moves:
            src = normalize_rel(str(move["from"]))
            dst = normalize_rel(str(move["to"]))
            src_decision = classify_path(src, ctx.policy)
            if not src_decision["writable"] and not move.get("worm"):
                raise _Refusal(
                    "SCOPE-WORM-REQUIRES-PIN",
                    f"{src} is read-only by scope policy ({src_decision['reason']}); "
                    "only a worm=True move with a sha256 pin may relocate it",
                )
            dst_decision = classify_path(dst, ctx.policy)
            if not dst_decision["writable"] and not (
                move.get("worm") and move.get("sha256")
            ):
                # a byte-preserving pinned move may land inside a read-only
                # scope (e.g. WORM re-organization); anything else may not
                raise _Refusal(
                    "SCOPE-READ-ONLY",
                    f"move target {dst} is read-only by scope policy "
                    f"({dst_decision['reason']}); only a worm=True move with a "
                    "sha256 pin may write there",
                )

        # idempotency pre-check: every source gone and every target present
        def source_gone(rel: str) -> bool:
            return not ws.exists(rel) and not (ws.root / rel).exists()

        raw_sources = [normalize_rel(str(move["from"])) for move in moves]
        raw_targets = [normalize_rel(str(move["to"])) for move in moves]
        if moves and all(source_gone(src) for src in raw_sources):
            for target in raw_targets:
                if not ws.exists(target):
                    outcome.diagnostics.append(
                        Diagnostic(
                            "MOVE-VANISHED",
                            f"source is gone and target {target} is missing",
                            target,
                            None,
                            ERROR,
                        )
                    )
                    return outcome
            outcome.status = "already_done"
            return outcome
        expanded = _expand_moves(ws, moves)
        _validate_targets(ws, expanded)
    except _Refusal as exc:
        outcome.diagnostics.append(
            Diagnostic(exc.code, exc.message, None, exc.line, ERROR)
        )
        return outcome
    except ToolError as exc:
        outcome.diagnostics.append(
            Diagnostic("PATH-INVALID", str(exc), None, None, ERROR)
        )
        return outcome

    changed: list[str] = []
    content_changed = False
    moved_paths = []
    for move in expanded:
        data = ws.read(move["from"])
        digest = sha256_hex(data)
        if move.get("from_sha256") and move["from_sha256"] != digest:
            outcome.diagnostics.append(
                Diagnostic(
                    "INPUT-DRIFT",
                    f"{move['from']} content hash does not match the pinned hash",
                    move["from"],
                    None,
                    ERROR,
                )
            )
            return outcome
        old_module = ws.index.module_of(move["from"])
        old_package = (
            ws.index.package_of_module(old_module, move["from"].endswith("__init__.py"))
            if old_module
            else None
        )
        ws.move(move["from"], move["to"])
        moved_paths.append((move, old_package))
        changed.extend([move["from"], move["to"]])
        if move.get("worm"):
            if sha256_hex(ws.read(move["to"])) != digest:
                outcome.diagnostics.append(
                    Diagnostic(
                        "WORM-HASH-CHANGED",
                        f"WORM file {move['from']} changed content during move",
                        move["to"],
                        None,
                        ERROR,
                    )
                )
                return outcome

    for move, old_package in moved_paths:
        if move["to"].endswith(".py") and old_package:
            if _rebase_relative_imports(ctx, move["to"], old_package):
                content_changed = True
                changed.append(move["to"])

    for sync in sync_imports:
        sync_rule = Rule(
            {
                "rule_id": f"{rule.rule_id}#sync:{sync['old_module']}",
                "kind": "import_module",
                "stage": rule.stage,
                "status": rule.status,
                "expected_matches": int(sync.get("expected_matches") or 0),
                "old_module": sync["old_module"],
                "new_module": sync["new_module"],
                "old_symbol": None,
                "new_symbol": None,
                "reason": "consumer sync for layout move",
            }
        )
        sub = imports_engine.apply_rule(sync_rule, ctx)
        outcome.diagnostics.extend(sub.diagnostics)
        if sub.status == "refused":
            outcome.status = "refused"
            outcome.matches = len(expanded)
            return outcome
        if sub.status == "applied":
            content_changed = True
            changed.extend(sub.changed_paths)
            if sync.get("expected_matches") and sub.matches != int(sync["expected_matches"]):
                outcome.diagnostics.append(
                    Diagnostic(
                        "MATCH-DRIFT",
                        f"consumer sync for {sync['old_module']}: expected "
                        f"{sync['expected_matches']} sites, rewrote {sub.matches}",
                        None,
                        None,
                        ERROR,
                    )
                )
                outcome.status = "refused"
                return outcome

    for spec in reference_updates:
        sub_rule = dict(spec)
        sub_rule.setdefault("rule_id", f"{rule.rule_id}#ref:{spec.get('source_path')}")
        sub_rule.setdefault("kind", "reference_rule")
        sub_rule.setdefault("stage", rule.stage)
        sub_rule.setdefault("status", rule.status)
        sub_rule.setdefault("expected_matches", spec.get("expected_matches", sum(
            int(op.get("count", 1)) for op in spec.get("operations", [])
        ) or 0))
        sub = references_engine.apply_rule(Rule(sub_rule), ctx)
        outcome.diagnostics.extend(sub.diagnostics)
        if sub.status == "refused":
            outcome.status = "refused"
            return outcome
        if sub.status == "applied":
            content_changed = True
            changed.extend(sub.changed_paths)

    for spec in manifest_updates:
        sub_rule = {
            "rule_id": f"{rule.rule_id}#manifest:{spec.get('path')}",
            "kind": "reference_rule",
            "stage": rule.stage,
            "status": rule.status,
            "source_path": spec["path"],
            "format": "json",
            "expected_matches": sum(
                int(op.get("count", 1)) for op in spec.get("operations", [])
            ),
            "operations": spec.get("operations", []),
            "reason": "authority manifest path migration",
        }
        sub = references_engine.apply_rule(Rule(sub_rule), ctx)
        outcome.diagnostics.extend(sub.diagnostics)
        if sub.status == "refused":
            outcome.status = "refused"
            return outcome
        if sub.status == "applied":
            content_changed = True
            changed.append(spec["path"])

    outcome.status = "applied"
    outcome.matches = len(expanded)
    outcome.changed_paths = sorted(set(changed))
    outcome.detail["content_changed"] = content_changed
    outcome.detail["moves"] = [
        {"from": m["from"], "to": m["to"]} for m in expanded
    ]
    return outcome


__all__ = ["KINDS", "apply_rule"]
