"""Non-import reference rewrites: module strings, config fields, path spans.

Rule kind ``reference_rule`` rewrites explicit references in one file. Only
approved, count-verified operations are supported:

- ``module_string`` (python): exact string-constant replacement via LibCST
  (importlib.import_module arguments, ``-m`` argv entries,
  ``importlib.resources.files`` package names, generated config strings).
- ``parents_depth_shift`` (python): shifts the ``N`` in
  ``...__file__....parents[N]`` expressions by a delta.
- ``json_field`` (json): replaces a whole field value addressed by a dotted
  path; formatting is preserved by span surgery and re-validated with
  ``json.loads``.
- ``json_entry_path`` (json): authority-manifest entry migration; rewrites
  only ``path`` values of matching entries, preserving every other field
  byte-for-byte (identity/reason preservation is checked afterwards).
- ``toml_key`` (toml): replaces a string value addressed by a dotted key;
  validated with ``tomllib`` before and after.
- ``text_span`` (text): exact-substring replacement with expected count.

Every operation validates its match count; mismatches refuse the rule.
"""

from __future__ import annotations

import json
import re
import tomllib

import libcst as cst

from .common import Diagnostic, ERROR, Rule, RuleOutcome, decode_source
from .parsing import ParsedPython, TransformContext

KINDS = ("reference_rule",)


class _Refusal(Exception):
    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


def _json_escape(value: str) -> list[str]:
    return [json.dumps(value, ensure_ascii=False), json.dumps(value, ensure_ascii=True)]


def _rewrite_json_field(text: str, field_path: list[str], old: str, new: str, expected: int) -> str:
    pattern = r'"' + re.escape(field_path[0]) + r'"\s*:\s*'
    for segment in field_path[1:]:
        # each deeper segment sits inside a direct child object
        pattern += r'\{\s*"' + re.escape(segment) + r'"\s*:\s*'
    replacements = 0
    out = text
    for escaped_old in _json_escape(old):
        value_pattern = pattern + re.escape(escaped_old)
        matches = re.findall(value_pattern, out)
        if matches:
            replacements += len(matches)
            out = re.sub(value_pattern, lambda m: m.group(0)[: -len(escaped_old)] + _json_escape(new)[0], out)
    if replacements != expected:
        raise _Refusal(
            "JSON-FIELD-COUNT",
            f"json_field {field_path}: expected {expected} occurrence(s) of {old!r}, "
            f"found {replacements}",
        )
    return out


def _rewrite_json_entry_paths(text: str, match_path: str, new_path: str, expected: int) -> str:
    escaped = _json_escape(match_path)
    count = 0
    out = text
    for esc in escaped:
        pattern = r'"path"\s*:\s*' + re.escape(esc)
        found = re.findall(pattern, out)
        count += len(found)
        out = re.sub(
            pattern,
            lambda m: m.group(0)[: -len(esc)] + _json_escape(new_path)[0],
            out,
        )
    if count != expected:
        raise _Refusal(
            "JSON-ENTRY-COUNT",
            f"json_entry_path: expected {expected} entr(ies) with path {match_path!r}, found {count}",
        )
    return out


def _rewrite_toml_key(text: str, key_path: list[str], old: str, new: str, expected: int) -> str:
    last = re.escape(key_path[-1])
    pattern = re.compile(
        r'(?m)^(\s*)' + last + r'\s*=\s*"' + re.escape(old) + r'"\s*$'
    )
    found = pattern.findall(text)
    if len(found) != expected:
        raise _Refusal(
            "TOML-KEY-COUNT",
            f"toml_key {key_path}: expected {expected} occurrence(s), found {len(found)}",
        )
    return pattern.sub(lambda m: f'{m.group(1)}{key_path[-1]} = "{new}"', text)


def _python_string_ops(
    parsed: ParsedPython, ops: list[dict]
) -> tuple[str, int]:
    replacement: dict[int, cst.SimpleString] = {}
    counts: dict[int, int] = {id(op): 0 for op in ops}

    def op_for_raw(raw: str) -> dict | None:
        for op in ops:
            if op["op"] == "module_string" and op["old"] == raw:
                return op
        return None

    def maybe_replace(node: cst.SimpleString) -> None:
        raw = node.raw_value
        op = op_for_raw(raw)
        if op is None:
            return
        new_value = op["new"]
        quotes = node.value[len(node.prefix) :]
        quote = quotes[:3] if quotes[:3] in ('"""', "'''") else quotes[:1]
        if (
            quote in new_value
            or ("\n" in new_value and not quote.startswith('"""'))
            or (not node.prefix.startswith(("r", "rb", "br")) and "\\" in new_value)
        ):
            raise _Refusal(
                "STRING-ESCAPING-UNSUPPORTED",
                f"replacement for {op['old']!r} needs escaping the original quote "
                f"style {quote!r}; adjust the rule",
                parsed.line_of(node),
            )
        replacement[id(node)] = node.with_changes(value=node.prefix + quote + new_value + quote)
        counts[id(op)] += 1

    def contains_file(node: cst.CSTNode) -> bool:
        for child in node.children:
            if isinstance(child, cst.Name) and child.value == "__file__":
                return True
            if isinstance(child, cst.Attribute) and child.attr.value == "__file__":
                return True
            if contains_file(child):
                return True
        return False

    class Scan(cst.CSTTransformer):
        def leave_SimpleString(self, original_node, updated_node):
            maybe_replace(original_node)
            return replacement.get(id(original_node), updated_node)

        def leave_Subscript(self, original_node, updated_node):
            value = original_node.value
            if (
                isinstance(value, cst.Attribute)
                and value.attr.value == "parents"
                and contains_file(value)
            ):
                slice_nodes = original_node.slice
                if isinstance(slice_nodes, cst.SubscriptElement):
                    slice_nodes = [slice_nodes]
                index = None
                if len(slice_nodes) == 1:
                    index = slice_nodes[0].slice
                if isinstance(index, cst.Index) and isinstance(index.value, cst.Integer):
                    try:
                        depth = int(index.value.value)
                    except ValueError:
                        depth = None
                    if depth is not None:
                        for op in ops:
                            if op["op"] == "parents_depth_shift":
                                new_depth = depth + int(op["delta"])
                                counts[id(op)] += 1
                                return updated_node.with_changes(
                                    slice=[
                                        cst.SubscriptElement(
                                            slice=cst.Index(value=cst.Integer(str(new_depth)))
                                        )
                                    ]
                                )
            return updated_node

    new_module = parsed.module.visit(Scan())
    total = 0
    for op in ops:
        expected = op.get("count")
        actual = counts[id(op)]
        if expected is not None and actual != int(expected):
            raise _Refusal(
                "MATCH-COUNT",
                f"op {op['op']}: expected {expected} occurrence(s) but rewrote {actual}",
            )
        total += actual
    return new_module.code, total


def apply_rule(rule: Rule, ctx: TransformContext) -> RuleOutcome:
    ws = ctx.workspace
    source_path = str(rule.get("source_path"))
    file_format = str(rule.get("format"))
    operations = list(rule.get("operations") or [])
    outcome = RuleOutcome(
        rule_id=rule.rule_id,
        kind=rule.kind,
        stage=rule.stage,
        status="refused",
        expected_matches=rule.expected_matches,
    )
    if not ws.exists(source_path):
        outcome.diagnostics.append(
            Diagnostic("MISSING-INPUT", f"{source_path} does not exist", source_path, None, ERROR)
        )
        return outcome
    try:
        ctx.writable_strict(source_path)
    except Exception as exc:  # ToolError -> scope refusal
        outcome.diagnostics.append(
            Diagnostic("SCOPE-READ-ONLY", str(exc), source_path, None, ERROR)
        )
        return outcome
    data = ws.read(source_path)
    try:
        total = 0
        if file_format == "python":
            text, encoding = decode_source(data, source_path)
            parsed = ParsedPython(source_path, text, encoding)
            new_code, total = _python_string_ops(parsed, operations)
            if total:
                ws.write(source_path, new_code.encode(encoding))
        elif file_format == "json":
            before = json.loads(data.decode("utf-8"))
            text = data.decode("utf-8")
            for op in operations:
                if op["op"] == "json_field":
                    text = _rewrite_json_field(
                        text, op["field"], op["old"], op["new"], int(op.get("count", 1))
                    )
                    total += int(op.get("count", 1))
                elif op["op"] == "json_entry_path":
                    text = _rewrite_json_entry_paths(
                        text, op["match_path"], op["new_path"], int(op.get("count", 1))
                    )
                    total += int(op.get("count", 1))
                elif op["op"] == "text_span":
                    found = text.count(op["old"])
                    if found != int(op.get("count", 1)):
                        raise _Refusal(
                            "MATCH-COUNT",
                            f"text_span: expected {op.get('count', 1)} occurrence(s), found {found}",
                        )
                    text = text.replace(op["old"], op["new"])
                    total += found
                else:
                    raise _Refusal("UNSUPPORTED-OP", f"op {op['op']!r} unsupported for json")
            after = json.loads(text)
            if after != _patch_expected(before, operations):
                raise _Refusal(
                    "JSON-SEMANTIC-DRIFT",
                    "rewritten JSON does not match the expected semantic patch",
                )
            if total:
                ws.write(source_path, text.encode("utf-8"))
        elif file_format == "toml":
            before = tomllib.loads(data.decode("utf-8"))
            text = data.decode("utf-8")
            for op in operations:
                if op["op"] != "toml_key":
                    raise _Refusal("UNSUPPORTED-OP", f"op {op['op']!r} unsupported for toml")
                text = _rewrite_toml_key(
                    text, op["key"], op["old"], op["new"], int(op.get("count", 1))
                )
                total += int(op.get("count", 1))
            after = tomllib.loads(text)
            if after != _patch_expected(before, operations):
                raise _Refusal(
                    "TOML-SEMANTIC-DRIFT",
                    "rewritten TOML does not match the expected semantic patch",
                )
            if total:
                ws.write(source_path, text.encode("utf-8"))
        elif file_format == "text":
            text, encoding = decode_source(data, source_path)
            for op in operations:
                if op["op"] != "text_span":
                    raise _Refusal("UNSUPPORTED-OP", f"op {op['op']!r} unsupported for text")
                found = text.count(op["old"])
                if found != int(op.get("count", 1)):
                    raise _Refusal(
                        "MATCH-COUNT",
                        f"text_span: expected {op.get('count', 1)} occurrence(s), found {found}",
                    )
                text = text.replace(op["old"], op["new"])
                total += found
            if total:
                ws.write(source_path, text.encode(encoding))
        else:
            raise _Refusal("UNSUPPORTED-FORMAT", f"format {file_format!r} unsupported")
    except _Refusal as exc:
        outcome.diagnostics.append(
            Diagnostic(exc.code, exc.message, source_path, exc.line, ERROR)
        )
        return outcome

    outcome.matches = total
    if total == 0:
        if rule.expected_matches == 0:
            outcome.status = "already_done"
        else:
            outcome.diagnostics.append(
                Diagnostic(
                    "EXPECTED-MATCHES",
                    f"expected {rule.expected_matches} replacements in {source_path} but made 0",
                    source_path,
                    None,
                    ERROR,
                )
            )
        return outcome
    if total != rule.expected_matches:
        outcome.status = "refused"
        outcome.diagnostics.append(
            Diagnostic(
                "MATCH-DRIFT",
                f"expected {rule.expected_matches} replacements but made {total} in {source_path}",
                source_path,
                None,
                ERROR,
            )
        )
        return outcome
    outcome.status = "applied"
    outcome.changed_paths = [source_path]
    return outcome


def _patch_expected(before: object, operations: list[dict]) -> object:
    """Deep-copy `before` with the semantic patch the operations describe."""
    import copy

    result = copy.deepcopy(before)
    for op in operations:
        if op["op"] == "json_field":
            node = result
            for segment in op["field"][:-1]:
                node = node[segment]
            leaf = op["field"][-1]
            if node.get(leaf) != op["old"]:
                raise _Refusal(
                    "JSON-FIELD-MISSING",
                    f"field {'.'.join(op['field'])} does not hold {op['old']!r}",
                )
            node[leaf] = op["new"]
        elif op["op"] == "json_entry_path":
            count = 0
            for entry in _iter_entries(result):
                if isinstance(entry, dict) and entry.get("path") == op["match_path"]:
                    entry["path"] = op["new_path"]
                    count += 1
            if count != int(op.get("count", 1)):
                raise _Refusal(
                    "JSON-ENTRY-COUNT",
                    f"expected {op.get('count', 1)} entries with path {op['match_path']!r}, "
                    f"semantic pass found {count}",
                )
        elif op["op"] == "toml_key":
            node = result
            for segment in op["key"][:-1]:
                node = node[segment]
            leaf = op["key"][-1]
            if node.get(leaf) != op["old"]:
                raise _Refusal(
                    "TOML-KEY-MISSING",
                    f"key {'.'.join(op['key'])} does not hold {op['old']!r}",
                )
            node[leaf] = op["new"]
    return result


def _iter_entries(node: object):
    if isinstance(node, list):
        for item in node:
            yield item
    elif isinstance(node, dict):
        for value in node.values():
            if isinstance(value, list):
                for item in value:
                    yield item
            elif isinstance(value, dict):
                yield from _iter_entries(value)


__all__ = ["KINDS", "apply_rule"]
