#!/usr/bin/env python3
"""Node platform-idiom gate: POSIX-only idioms in first-party Node code.

Companion of ``check_node_net_apis.py`` (owner 2026-11-21, W1B + R2 lexical
hardening).  Scans ``.ts`` / ``.mjs`` / ``.js`` / ``.cjs`` files under
``src/hyprial`` (excluding ``_gui`` and ``node_modules``) over a real JS/TS
token stream that strips single-line comments, multi-line block comments, regex
literals, string literals and non-expression template text while tokenising
``${...}`` template interpolations at arbitrary nesting depth:

* N001 ``posix-mode-mask`` -- bitwise ``&`` against an octal permission mask
  (``0o077``, ``0O077``, ``0o77``, ``0o0_77``, ``0o777``, etc.) or a direct
  ``mode ==/===/!=/!== 0o600/0o700`` check.  Windows synthesises ``st_mode``;
  privacy is enforced at the Home root gate (umask / protected DACL).
* N002 ``negative-pid-signal`` -- ``process.kill(-pid, ...)``, including
  optional chaining (``process?.kill?.(-pid)``), computed member access
  (``process["kill"](-pid)``), and file-local aliases or destructuring from
  ``process`` (``const { kill } = process; kill(-pid)``).
* N003 ``bare-home-env`` -- reading ``env.HOME`` / ``process.env.HOME`` across
  dot, optional-chaining, bracket (``process["env"]["HOME"]``), alias and
  destructuring (``const { HOME } = process.env``) forms, while excluding write,
  update, delete, ``for-in``/``for-of``/``for await...of`` iteration and
  array/object destructuring assignment targets (``=``, compound
  ``+=``/``&&=``/``||=``/``??=``/..., prefix/postfix ``++``/``--``, prefix
  ``delete``, ``for (process.env.HOME in/of ...)``, ``[process.env.HOME] = ...``,
  ``({ HOME: process.env.HOME } = ...)``).  Windows resolves the user home via
  ``USERPROFILE`` / ``os.homedir()``, or explicit ``HYPRIAL_HOME``.

The baseline ``scripts/ci/node-platform-idioms-baseline.json`` is an only-out
``{path: {rule: {count, reason, removal}}}`` count map.  ``--base-ref`` allows
initial seeding only when the baseline file does not yet exist at ``base_ref``;
once the baseline file exists in ``base_ref``, any new ``(path, rule)`` key or
raised count fails closed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCAN_ROOTS = ("src/hyprial",)
SUFFIXES = frozenset({".ts", ".mjs", ".js", ".cjs"})
EXCLUDED_PARTS = frozenset({"_gui", "__pycache__", "node_modules"})
BASELINE_RELATIVE_PATH = "scripts/ci/node-platform-idioms-baseline.json"
BASELINE_VERSION = 1
ENTRY_FIELDS = frozenset({"count", "reason", "removal"})
RULES: dict[str, tuple[str, str]] = {
    "N001": ("posix-mode-mask", "the Home root gate (umask / protected DACL); no per-file mode check"),
    "N002": ("negative-pid-signal", "child.kill() or the process-tree owner (kernel seam); no POSIX group signal"),
    "N003": ("bare-home-env", "os.homedir() or an explicit HYPRIAL_HOME (Windows has USERPROFILE)"),
}
ADVISORY_RULES: frozenset[str] = frozenset()

OCTAL_RE = re.compile(r"0[oO][0-7][0-7_]*\Z")
NUMBER_RE = re.compile(
    r"(?:0[oO][0-7][0-7_]*|0[xX][0-9a-fA-F][0-9a-fA-F_]*|0[bB][01][01_]*"
    r"|\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?[\d_]+)?)n?"
)
IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
MULTI_OPS = (
    "===", "!==", ">>>=", "<<=", ">>=", "&&=", "||=", "??=", "**=", "...",
    "==", "!=", "<=", ">=", "&&", "||", "??", "=>", "++", "--",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<", ">>", "**",
)
PUNC_CHARS = frozenset("()[]{};,:")
OP_CHARS = frozenset(".&|^~+-*%<>!=?/")
REGEX_KEYWORDS = frozenset(
    {"return", "throw", "case", "delete", "void", "typeof", "instanceof", "in", "of", "new", "yield", "await", "else"}
)
GROUPING_PREFIX_KEYWORDS = frozenset(
    {"delete", "void", "typeof", "return", "throw", "case", "in", "of", "new", "yield", "await"}
)
EQ_OPS = frozenset({"==", "===", "!=", "!=="})
ASSIGN_OPS = frozenset(
    {"=", "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", ">>>=", "&=", "|=", "^=", "&&=", "||=", "??="}
)
UPDATE_OPS = frozenset({"++", "--"})
PAIRS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {")": "(", "]": "[", "}": "{"}
ARRAY_PATTERN_PREFIX_KEYWORDS = frozenset(
    {"else", "do", "return", "throw", "case", "default", "yield", "await", "of", "in", "instanceof"}
)
FOR_HEADER_KEYWORDS = frozenset({"if", "while", "for", "with"})
DECL_PREFIX_KEYWORDS = frozenset({"const", "let", "var", "using", "extends"})
Token = tuple[str, str, int]
BracketTree = tuple[dict[int, int], list[int]]


class GateFormatError(ValueError):
    """The baseline cannot be read or validated: fail closed."""


def _regex_allowed(previous: Token | None) -> bool:
    if previous is None:
        return True
    kind, value, _line = previous
    if kind == "TPL":
        return True
    if kind in {"OP", "PUNC"}:
        return value not in {")", "]", "}", "++", "--"}
    return kind == "IDENT" and value in REGEX_KEYWORDS


def _scan_regex(source: str, start: int) -> int | None:
    length = len(source)
    pos = start + 1
    in_class = False
    while pos < length and source[pos] not in "\r\n":
        ch = source[pos]
        if ch == "\\":
            if pos + 1 >= length or source[pos + 1] in "\r\n":
                return None
            pos += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]" and in_class:
            in_class = False
        elif ch == "/" and not in_class:
            pos += 1
            while pos < length and source[pos].isalpha():
                pos += 1
            return pos
        pos += 1
    return None


def _unescape_char(ch: str) -> str:
    return {"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(ch, ch)


def _scan_quoted(source: str, start: int, line: int) -> tuple[str, int, int]:
    quote = source[start]
    pos = start + 1
    length = len(source)
    chars: list[str] = []
    while pos < length:
        ch = source[pos]
        if ch == "\\":
            if pos + 1 >= length:
                pos += 1
                break
            nxt = source[pos + 1]
            if nxt == "\n":
                line += 1
            else:
                if nxt == "\r" and pos + 2 < length and source[pos + 2] == "\n":
                    line += 1
                    pos += 3
                    continue
                chars.append(_unescape_char(nxt))
            pos += 2
            continue
        if ch == quote:
            return "".join(chars), pos + 1, line
        if ch == "\n":
            line += 1
            pos += 1
            break
        chars.append(ch)
        pos += 1
    return "".join(chars), pos, line


def _scan_template_segment(source: str, pos: int, line: int) -> tuple[str, bool, int, int]:
    length = len(source)
    chars: list[str] = []
    while pos < length:
        ch = source[pos]
        if ch == "\\":
            if pos + 1 >= length:
                pos += 1
                break
            nxt = source[pos + 1]
            if nxt == "\n":
                line += 1
            elif nxt == "\r" and pos + 2 < length and source[pos + 2] == "\n":
                line += 1
                pos += 3
                continue
            else:
                chars.append(_unescape_char(nxt))
            pos += 2
            continue
        if ch == "`":
            return "".join(chars), False, pos + 1, line
        if ch == "$" and pos + 1 < length and source[pos + 1] == "{":
            return "".join(chars), True, pos + 2, line
        if ch == "\n":
            line += 1
        chars.append(ch)
        pos += 1
    return "".join(chars), False, pos, line


def tokenize_js(source: str) -> list[Token]:
    """Tokenise JS/TS into code tokens; comments and template text do not leak tokens."""

    tokens: list[Token] = []
    pos = 0
    line = 1
    length = len(source)
    tpl_depths: list[int] = []
    if source.startswith("#!"):
        newline = source.find("\n")
        pos = length if newline < 0 else newline
    while pos < length:
        ch = source[pos]
        if ch in " \t\r\f\v":
            pos += 1
            continue
        if ch == "\n":
            line += 1
            pos += 1
            continue
        if source.startswith("//", pos):
            newline = source.find("\n", pos + 2)
            pos = length if newline < 0 else newline
            continue
        if source.startswith("/*", pos):
            end = source.find("*/", pos + 2)
            segment_end = length if end < 0 else end + 2
            line += source.count("\n", pos, segment_end)
            pos = segment_end
            continue
        if ch in {"'", '"'}:
            start_line = line
            value, pos, line = _scan_quoted(source, pos, line)
            tokens.append(("STR", value, start_line))
            continue
        if ch == "`":
            start_line = line
            value, has_expr, pos, line = _scan_template_segment(source, pos + 1, line)
            if has_expr:
                tokens.append(("TPL", "", start_line))
                tpl_depths.append(1)
            else:
                tokens.append(("STR", value, start_line))
            continue
        if ch == "{" and tpl_depths:
            tpl_depths[-1] += 1
            tokens.append(("PUNC", "{", line))
            pos += 1
            continue
        if ch == "}" and tpl_depths:
            tpl_depths[-1] -= 1
            if tpl_depths[-1] == 0:
                tpl_depths.pop()
                start_line = line
                _value, has_expr, pos, line = _scan_template_segment(source, pos + 1, line)
                tokens.append(("TPL", "", start_line))
                if has_expr:
                    tpl_depths.append(1)
                continue
            tokens.append(("PUNC", "}", line))
            pos += 1
            continue
        if ch == "/" and _regex_allowed(tokens[-1] if tokens else None):
            regex_end = _scan_regex(source, pos)
            if regex_end is not None:
                tokens.append(("REGEX", source[pos:regex_end], line))
                pos = regex_end
                continue
        if source.startswith("?.", pos) and not (pos + 2 < length and source[pos + 2].isdigit()):
            tokens.append(("OP", "?.", line))
            pos += 2
            continue
        num_match = NUMBER_RE.match(source, pos)
        if num_match is not None:
            tokens.append(("NUM", num_match.group(0), line))
            pos = num_match.end()
            continue
        ident_match = IDENT_RE.match(source, pos)
        if ident_match is not None:
            tokens.append(("IDENT", ident_match.group(0), line))
            pos = ident_match.end()
            continue
        matched_op = next((op for op in MULTI_OPS if source.startswith(op, pos)), None)
        if matched_op is not None:
            tokens.append(("OP", matched_op, line))
            pos += len(matched_op)
            continue
        if ch in PUNC_CHARS:
            tokens.append(("PUNC", ch, line))
        elif ch in OP_CHARS:
            tokens.append(("OP", ch, line))
        pos += 1
    return tokens


def _member(tokens: list[Token], index: int) -> tuple[str, int] | None:
    count = len(tokens)
    if index + 1 < count and tokens[index][:2] in {("OP", "."), ("OP", "?.")} and tokens[index + 1][0] == "IDENT":
        return tokens[index + 1][1], index + 2
    if (
        index + 3 < count and tokens[index][:2] == ("OP", "?.") and tokens[index + 1][:2] == ("PUNC", "[")
        and tokens[index + 2][0] == "STR" and tokens[index + 3][:2] == ("PUNC", "]")
    ):
        return tokens[index + 2][1], index + 4
    if (
        index + 2 < count and tokens[index][:2] == ("PUNC", "[")
        and tokens[index + 1][0] == "STR" and tokens[index + 2][:2] == ("PUNC", "]")
    ):
        return tokens[index + 1][1], index + 3
    return None


def _not_member_target(tokens: list[Token], index: int) -> bool:
    return index == 0 or tokens[index - 1][:2] not in {("OP", "."), ("OP", "?.")}


def _process_end(tokens: list[Token], index: int, process_aliases: set[str]) -> int | None:
    if not _not_member_target(tokens, index) or index >= len(tokens) or tokens[index][0] != "IDENT":
        return None
    name = tokens[index][1]
    if name == "process" or name in process_aliases:
        return index + 1
    if name in {"globalThis", "global"}:
        step = _member(tokens, index + 1)
        if step is not None and step[0] == "process":
            return step[1]
    return None


def _parse_object_pattern(tokens: list[Token], brace_index: int) -> tuple[list[tuple[str, str, int]], int] | None:
    if brace_index >= len(tokens) or tokens[brace_index][:2] != ("PUNC", "{"):
        return None
    items: list[tuple[str, str, int]] = []
    pos = brace_index + 1
    count = len(tokens)
    while pos < count:
        if tokens[pos][:2] == ("PUNC", "}"):
            return items, pos + 1
        if tokens[pos][:2] in {("PUNC", ","), ("OP", "...")}:
            if tokens[pos][:2] == ("OP", "..."):
                pos += 2
            else:
                pos += 1
            continue
        if tokens[pos][0] not in {"IDENT", "STR"}:
            return None
        prop, prop_line = tokens[pos][1], tokens[pos][2]
        pos += 1
        local = prop
        if pos < count and tokens[pos][:2] == ("PUNC", ":"):
            pos += 1
            if pos < count and tokens[pos][:2] == ("PUNC", "{"):
                nested = _parse_object_pattern(tokens, pos)
                if nested is None:
                    return None
                sub_items, pos = nested
                for sub_prop, sub_local, sub_line in sub_items:
                    items.append((f"{prop}.{sub_prop}", sub_local, sub_line))
                local = ""
            elif pos < count and tokens[pos][0] == "IDENT":
                local = tokens[pos][1]
                pos += 1
            else:
                return None
        if local:
            items.append((prop, local, prop_line))
        depth = 0
        while pos < count:
            tk = tokens[pos][:2]
            if depth == 0 and tk in {("PUNC", ","), ("PUNC", "}")}:
                break
            if tk in {("PUNC", "("), ("PUNC", "["), ("PUNC", "{")}:
                depth += 1
            elif tk in {("PUNC", ")"), ("PUNC", "]"), ("PUNC", "}")} and depth > 0:
                depth -= 1
            pos += 1
    return None


def _skip_type_to_equals(tokens: list[Token], index: int) -> int | None:
    count = len(tokens)
    if index < count and tokens[index][:2] == ("OP", "="):
        return index + 1
    if index >= count or tokens[index][:2] != ("PUNC", ":"):
        return None
    pos = index + 1
    depth = 0
    while pos < count:
        tk = tokens[pos][:2]
        if depth == 0 and tk == ("OP", "="):
            return pos + 1
        if depth == 0 and tk == ("PUNC", ";"):
            return None
        if tk in {("PUNC", "("), ("PUNC", "["), ("PUNC", "{")}:
            depth += 1
        elif tk in {("PUNC", ")"), ("PUNC", "]"), ("PUNC", "}")} and depth > 0:
            depth -= 1
        pos += 1
    return None


def _collect_bindings(
    tokens: list[Token],
) -> tuple[set[str], set[str], set[str], list[tuple[int, str]]]:
    process_aliases: set[str] = set()
    kill_aliases: set[str] = set()
    env_aliases: set[str] = set()
    destructured_home: list[tuple[int, str]] = []
    count = len(tokens)
    for idx, tok in enumerate(tokens):
        if tok[0] == "IDENT" and tok[1] in {"const", "let", "var"} and idx + 1 < count:
            if tokens[idx + 1][0] == "IDENT":
                lhs = tokens[idx + 1][1]
                rhs = _skip_type_to_equals(tokens, idx + 2)
                if rhs is not None:
                    p_end = _process_end(tokens, rhs, process_aliases)
                    if p_end is not None:
                        step = _member(tokens, p_end)
                        if step is None:
                            process_aliases.add(lhs)
                        elif step[0] == "kill" and _member(tokens, step[1]) is None:
                            kill_aliases.add(lhs)
                        elif step[0] == "env" and _member(tokens, step[1]) is None:
                            env_aliases.add(lhs)
            elif tokens[idx + 1][:2] == ("PUNC", "{"):
                parsed = _parse_object_pattern(tokens, idx + 1)
                if parsed is not None:
                    entries, after_brace = parsed
                    rhs = _skip_type_to_equals(tokens, after_brace)
                    if rhs is not None:
                        p_end = _process_end(tokens, rhs, process_aliases)
                        if p_end is not None:
                            step = _member(tokens, p_end)
                            if step is None:
                                for prop, local, prop_line in entries:
                                    if prop == "kill":
                                        kill_aliases.add(local)
                                    elif prop == "env":
                                        env_aliases.add(local)
                                    elif prop == "env.HOME":
                                        destructured_home.append((prop_line, "{ env: { HOME } } = process"))
                            elif step[0] == "env":
                                for prop, _local, prop_line in entries:
                                    if prop == "HOME":
                                        destructured_home.append((prop_line, "{ HOME } = process.env"))
                        elif (
                            rhs < count and tokens[rhs][0] == "IDENT"
                            and tokens[rhs][1] in ({"env"} | env_aliases)
                        ):
                            for prop, _local, prop_line in entries:
                                if prop == "HOME":
                                    destructured_home.append((prop_line, f"{{ HOME }} = {tokens[rhs][1]}"))
    return process_aliases, kill_aliases, env_aliases, destructured_home


def _octal_mask(token: Token) -> int | None:
    if token[0] != "NUM" or not OCTAL_RE.match(token[1]):
        return None
    value = int(token[1][2:].replace("_", ""), 8)
    return value if 0 < value <= 0o7777 else None


def _ends_with_mode(tokens: list[Token], index: int) -> bool:
    if index < 0:
        return False
    if tokens[index][:2] == ("IDENT", "mode"):
        return True
    return (
        index >= 2 and tokens[index - 2][:2] == ("PUNC", "[")
        and tokens[index - 1][:2] == ("STR", "mode") and tokens[index][:2] == ("PUNC", "]")
    )


def _starts_with_mode(tokens: list[Token], index: int) -> bool:
    if index >= len(tokens):
        return False
    if tokens[index][:2] == ("IDENT", "mode"):
        return True
    if tokens[index][0] == "IDENT":
        step = _member(tokens, index + 1)
        return step is not None and step[0] == "mode"
    return False


def _negative_first_arg(tokens: list[Token], open_paren: int) -> bool:
    pos = open_paren + 1
    count = len(tokens)
    while pos < count and tokens[pos][:2] == ("PUNC", "("):
        pos += 1
    return pos + 1 < count and tokens[pos][:2] == ("OP", "-") and tokens[pos + 1][:2] != ("PUNC", ")")


def _can_end_expr(token: Token) -> bool:
    if token[0] in {"NUM", "STR", "REGEX"}:
        return True
    if token[:2] in {("PUNC", ")"), ("PUNC", "]"), ("PUNC", "}")}:
        return True
    return token[0] == "IDENT" and token[1] not in REGEX_KEYWORDS


def _bracket_tree(tokens: list[Token]) -> BracketTree:
    match: dict[int, int] = {}
    parent = [-1] * len(tokens)
    stack: list[int] = []
    for idx, (kind, val, _line) in enumerate(tokens):
        parent[idx] = stack[-1] if stack else -1
        if kind == "PUNC" and val in PAIRS:
            stack.append(idx)
        elif kind == "PUNC" and val in CLOSERS and stack and tokens[stack[-1]][1] == CLOSERS[val]:
            open_idx = stack.pop()
            match[open_idx] = idx
            match[idx] = open_idx
            parent[idx] = stack[-1] if stack else -1
    return match, parent


def _is_class_body(tokens: list[Token], brace_idx: int, parent: list[int]) -> bool:
    outer = parent[brace_idx]
    for pos in range(brace_idx - 1, outer, -1):
        if parent[pos] != outer:
            continue
        tok = tokens[pos][:2]
        if tok == ("IDENT", "class"):
            return True
        if tok in {("PUNC", ";"), ("PUNC", "{"), ("PUNC", "}"), ("OP", "="), ("OP", "=>")}:
            return False
    return False


def _can_start_array_pattern(
    tokens: list[Token], open_bracket: int, match: dict[int, int], parent: list[int]
) -> bool:
    outer = parent[open_bracket]
    if outer >= 0 and tokens[outer][:2] == ("PUNC", "{") and _is_class_body(tokens, outer, parent):
        return False
    if open_bracket == 0:
        return True
    prev = tokens[open_bracket - 1]
    if prev[:2] in {("OP", "?."), ("OP", "!"), ("OP", "++"), ("OP", "--"), ("PUNC", "]")}:
        return False
    if prev[0] in {"STR", "NUM", "REGEX", "TPL"}:
        return False
    if prev[0] == "IDENT":
        return prev[1] in ARRAY_PATTERN_PREFIX_KEYWORDS
    if prev[:2] == ("PUNC", ")"):
        open_p = match.get(open_bracket - 1)
        if open_p is None or open_p == 0:
            return False
        if tokens[open_p - 1][0] == "IDENT" and tokens[open_p - 1][1] in FOR_HEADER_KEYWORDS:
            return True
        return open_p >= 2 and tokens[open_p - 2][:2] == ("IDENT", "for") and tokens[open_p - 1][:2] == ("IDENT", "await")
    if prev[:2] == ("PUNC", "}"):
        open_b = match.get(open_bracket - 1)
        if open_b is None or open_b == 0:
            return True
        before = tokens[open_b - 1]
        return before[:2] not in {("PUNC", "("), ("PUNC", "["), ("PUNC", ","), ("PUNC", ":"), ("IDENT", "return")} and before[0] != "OP"
    return True


def _chain_start(tokens: list[Token], index: int, match: dict[int, int]) -> int:
    pos = index
    while pos > 0:
        if tokens[pos][:2] in {("PUNC", "["), ("PUNC", "(")}:
            if tokens[pos - 1][:2] == ("OP", "?."):
                pos -= 1
                continue
            if tokens[pos - 1][0] == "IDENT" and tokens[pos - 1][1] not in (REGEX_KEYWORDS | FOR_HEADER_KEYWORDS):
                pos -= 1
                continue
            if tokens[pos - 1][:2] in {("PUNC", "]"), ("PUNC", ")")} and (pos - 1) in match:
                pos = match[pos - 1]
                continue
        if pos >= 2 and tokens[pos - 1][:2] in {("OP", "."), ("OP", "?.")}:
            if tokens[pos - 2][0] == "IDENT":
                pos -= 2
                continue
            if tokens[pos - 2][:2] in {("PUNC", "]"), ("PUNC", ")")} and (pos - 2) in match:
                pos = match[pos - 2]
                continue
        break
    return pos


def _grouping_paren(tokens: list[Token], open_paren: int) -> bool:
    if open_paren == 0:
        return True
    prev = tokens[open_paren - 1]
    if prev[0] == "OP":
        return prev[1] not in UPDATE_OPS or (
            open_paren == 1 or tokens[open_paren - 2][2] < prev[2] or not _can_end_expr(tokens[open_paren - 2])
        )
    if prev[0] == "PUNC":
        return prev[1] in "([{;,:"
    return prev[0] == "IDENT" and prev[1] in GROUPING_PREFIX_KEYWORDS


def _unwrap_grouping_parens(tokens: list[Token], left: int, right: int, match: dict[int, int]) -> tuple[int, int]:
    count = len(tokens)
    while (
        left > 0 and right < count and tokens[left - 1][:2] == ("PUNC", "(")
        and match.get(left - 1) == right and _grouping_paren(tokens, left - 1)
    ):
        left -= 1
        right += 1
    return left, right


def _is_for_in_of_target(
    tokens: list[Token], left: int, right: int, match: dict[int, int], parent: list[int]
) -> bool:
    open_p = parent[left]
    close_p = match.get(open_p) if open_p >= 1 and open_p == left - 1 and tokens[open_p][:2] == ("PUNC", "(") else None
    if close_p is None or right + 1 >= close_p:
        return False
    for_await = open_p >= 2 and tokens[open_p - 2][:2] == ("IDENT", "for") and tokens[open_p - 1][:2] == ("IDENT", "await")
    if not (for_await or tokens[open_p - 1][:2] == ("IDENT", "for")):
        return False
    if tokens[right][0] != "IDENT" or tokens[right][1] not in ({"of"} if for_await else {"in", "of"}):
        return False
    return not any(parent[idx] == open_p and tokens[idx][:2] == ("PUNC", ";") for idx in range(open_p + 1, close_p))


def _has_ternary_question(tokens: list[Token], colon_idx: int, parent: list[int]) -> bool:
    outer = parent[colon_idx]
    depth = 0
    for pos in range(colon_idx - 1, outer, -1):
        if parent[pos] != outer:
            continue
        tok = tokens[pos][:2]
        if tok in {("PUNC", ";"), ("PUNC", ",")}:
            return False
        if tok == ("PUNC", ":"):
            depth += 1
        elif tok == ("OP", "?"):
            if pos == colon_idx - 1:
                return False
            if depth == 0:
                return True
            depth -= 1
    return False


def _is_pattern_assigned(tokens: list[Token], left: int, right: int, parent: list[int]) -> bool:
    if right >= len(tokens) or tokens[right][:2] != ("OP", "="):
        return False
    if left == 0:
        return True
    prev = tokens[left - 1]
    if prev[0] == "IDENT" and prev[1] in DECL_PREFIX_KEYWORDS:
        return False
    return _has_ternary_question(tokens, left - 1, parent) if prev[:2] == ("PUNC", ":") else True


def _starts_clause(tokens: list[Token], enc: int, idx: int, parent: list[int]) -> bool:
    return idx == enc + 1 or (parent[idx - 1] == enc and tokens[idx - 1][:2] == ("PUNC", ","))


def _is_object_prop_colon(
    tokens: list[Token], enc: int, colon: int, match: dict[int, int], parent: list[int]
) -> bool:
    if colon - 1 <= enc:
        return False
    if parent[colon - 1] == enc and tokens[colon - 1][0] in {"IDENT", "STR", "NUM"}:
        return _starts_clause(tokens, enc, colon - 1, parent)
    key_open = match.get(colon - 1) if tokens[colon - 1][:2] == ("PUNC", "]") else None
    if key_open is None or parent[key_open] != enc or key_open + 1 >= colon - 1:
        return False
    return _starts_clause(tokens, enc, key_open, parent)


def _valid_pattern_tail(
    tokens: list[Token], enc: int, enc_close: int, right: int, parent: list[int], is_rest: bool
) -> bool:
    if right == enc_close:
        return True
    if parent[right] != enc:
        return False
    if tokens[right][:2] == ("PUNC", ","):
        return True
    return not is_rest and tokens[right][:2] == ("OP", "=") and right + 1 < enc_close and tokens[right + 1][:2] != ("PUNC", ",")


def _step_enclosing_pattern(
    tokens: list[Token], left: int, right: int, match: dict[int, int], parent: list[int]
) -> tuple[int, int] | None:
    enc = parent[left]
    if enc < 0 or enc not in match or parent[right - 1] != enc:
        return None
    enc_close, kind = match[enc], tokens[enc][:2]
    if kind not in {("PUNC", "["), ("PUNC", "{")}:
        return None
    if kind == ("PUNC", "[") and not _can_start_array_pattern(tokens, enc, match, parent):
        return None
    is_rest = left - 1 > enc and parent[left - 1] == enc and tokens[left - 1][:2] == ("OP", "...")
    if is_rest:
        ok = _starts_clause(tokens, enc, left - 1, parent) and _valid_pattern_tail(tokens, enc, enc_close, right, parent, True)
        return (enc, enc_close + 1) if ok else None
    if kind == ("PUNC", "["):
        ok = _starts_clause(tokens, enc, left, parent) and _valid_pattern_tail(tokens, enc, enc_close, right, parent, False)
        return (enc, enc_close + 1) if ok else None
    colon = left - 1
    ok = (
        colon > enc and parent[colon] == enc and tokens[colon][:2] == ("PUNC", ":")
        and _is_object_prop_colon(tokens, enc, colon, match, parent)
        and _valid_pattern_tail(tokens, enc, enc_close, right, parent, False)
    )
    return (enc, enc_close + 1) if ok else None


def _is_write_target(tokens: list[Token], start: int, end: int, tree: BracketTree | None = None) -> bool:
    count = len(tokens)
    match, parent = _bracket_tree(tokens) if tree is None else tree
    left, right = _unwrap_grouping_parens(tokens, _chain_start(tokens, start, match), end, match)
    if right < count and (
        tokens[right][:2] in {("OP", "."), ("OP", "?."), ("PUNC", "["), ("PUNC", "(")} or tokens[right][0] == "TPL"
    ):
        return False
    if right < count and tokens[right][0] == "OP":
        if tokens[right][1] in ASSIGN_OPS:
            return True
        if tokens[right][1] in UPDATE_OPS and tokens[right][2] == tokens[right - 1][2]:
            return True
    if left > 0:
        prev = tokens[left - 1]
        if prev[:2] == ("IDENT", "delete"):
            return True
        if prev[0] == "OP" and prev[1] in UPDATE_OPS:
            if prev[2] == tokens[left][2] or left == 1 or tokens[left - 2][2] < prev[2] or not _can_end_expr(tokens[left - 2]):
                return True
    if _is_for_in_of_target(tokens, left, right, match, parent):
        return True
    cur_left, cur_right = left, right
    while (stepped := _step_enclosing_pattern(tokens, cur_left, cur_right, match, parent)) is not None:
        cur_left, cur_right = _unwrap_grouping_parens(tokens, stepped[0], stepped[1], match)
        if _is_pattern_assigned(tokens, cur_left, cur_right, parent) or _is_for_in_of_target(
            tokens, cur_left, cur_right, match, parent
        ):
            return True
    return False


def scan_source(source: str, relative: str) -> list[dict[str, Any]]:
    """Every rule match in one file over its JS/TS code tokens."""

    tokens = tokenize_js(source)
    process_aliases, kill_aliases, env_aliases, destructured_home = _collect_bindings(tokens)
    findings: list[dict[str, Any]] = [
        {"path": relative, "rule": "N003", "line": line, "symbol": symbol}
        for line, symbol in destructured_home
    ]
    seen_n001_ops: set[int] = set()
    consumed_env: set[int] = set()
    tree: BracketTree | None = None
    count = len(tokens)

    for idx, tok in enumerate(tokens):
        mask = _octal_mask(tok)
        if mask is not None:
            if idx > 0 and tokens[idx - 1][:2] == ("OP", "&") and (idx - 1) not in seen_n001_ops:
                seen_n001_ops.add(idx - 1)
                findings.append({"path": relative, "rule": "N001", "line": tok[2], "symbol": f"& {tok[1]}"})
            elif idx + 1 < count and tokens[idx + 1][:2] == ("OP", "&") and (idx + 1) not in seen_n001_ops:
                seen_n001_ops.add(idx + 1)
                findings.append({"path": relative, "rule": "N001", "line": tok[2], "symbol": f"{tok[1]} &"})
            elif (
                idx >= 2 and tokens[idx - 1][0] == "OP" and tokens[idx - 1][1] in EQ_OPS
                and _ends_with_mode(tokens, idx - 2) and (idx - 1) not in seen_n001_ops
            ):
                seen_n001_ops.add(idx - 1)
                findings.append(
                    {"path": relative, "rule": "N001", "line": tok[2], "symbol": f"mode {tokens[idx - 1][1]} {tok[1]}"}
                )
            elif (
                idx + 2 < count and tokens[idx + 1][0] == "OP" and tokens[idx + 1][1] in EQ_OPS
                and _starts_with_mode(tokens, idx + 2) and (idx + 1) not in seen_n001_ops
            ):
                seen_n001_ops.add(idx + 1)
                findings.append(
                    {"path": relative, "rule": "N001", "line": tok[2], "symbol": f"{tok[1]} {tokens[idx + 1][1]} mode"}
                )

        p_end = _process_end(tokens, idx, process_aliases)
        if p_end is not None:
            step = _member(tokens, p_end)
            if step is not None and step[0] == "kill":
                call_idx = step[1] + 1 if step[1] < count and tokens[step[1]][:2] == ("OP", "?.") else step[1]
                if call_idx < count and tokens[call_idx][:2] == ("PUNC", "(") and _negative_first_arg(tokens, call_idx):
                    findings.append(
                        {"path": relative, "rule": "N002", "line": tok[2], "symbol": "process.kill(-...)"}
                    )
            elif step is not None and step[0] == "env":
                if tokens[step[1] - 1][:2] == ("IDENT", "env"):
                    consumed_env.add(step[1] - 1)
                home_step = _member(tokens, step[1])
                if home_step is not None and home_step[0] == "HOME":
                    if tree is None:
                        tree = _bracket_tree(tokens)
                    if not _is_write_target(tokens, idx, home_step[1], tree):
                        findings.append(
                            {"path": relative, "rule": "N003", "line": tok[2], "symbol": "process.env.HOME"}
                        )
            continue

        if tok[0] == "IDENT" and tok[1] in kill_aliases and _not_member_target(tokens, idx):
            call_idx = idx + 2 if idx + 1 < count and tokens[idx + 1][:2] == ("OP", "?.") else idx + 1
            if call_idx < count and tokens[call_idx][:2] == ("PUNC", "(") and _negative_first_arg(tokens, call_idx):
                findings.append({"path": relative, "rule": "N002", "line": tok[2], "symbol": f"{tok[1]}(-...)"})

        if tok[0] == "IDENT" and tok[1] in ({"env"} | env_aliases) and idx not in consumed_env:
            if tok[1] != "env" and not _not_member_target(tokens, idx):
                continue
            home_step = _member(tokens, idx + 1)
            if home_step is not None and home_step[0] == "HOME":
                if tree is None:
                    tree = _bracket_tree(tokens)
                if not _is_write_target(tokens, idx, home_step[1], tree):
                    findings.append({"path": relative, "rule": "N003", "line": tok[2], "symbol": f"{tok[1]}.HOME"})

    findings.sort(key=lambda row: (row["line"], row["rule"]))
    return findings


def _script_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for scan_root in SCAN_ROOTS:
        base = root / scan_root
        if base.is_dir():
            files.extend(path for path in base.rglob("*") if path.suffix in SUFFIXES and path.is_file())
    return sorted(path for path in files if not EXCLUDED_PARTS.intersection(path.relative_to(root).parts))


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise GateFormatError(f"duplicate baseline key(s): {duplicates}")
    return dict(pairs)


def parse_baseline(text: str, where: str = BASELINE_RELATIVE_PATH) -> dict[tuple[str, str], int]:
    """``{(path, rule): count}``; any defect fails closed."""

    try:
        document = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as error:
        raise GateFormatError(f"{where}: unreadable baseline: {error}") from error
    if not isinstance(document, dict) or document.get("version") != BASELINE_VERSION:
        raise GateFormatError(f"{where}: version must be {BASELINE_VERSION}")
    entries = document.get("entries")
    if not isinstance(entries, dict):
        raise GateFormatError(f"{where}: entries must be a {{path: {{rule: entry}}}} map")
    result: dict[tuple[str, str], int] = {}
    for path, by_rule in entries.items():
        if not isinstance(by_rule, dict) or not by_rule:
            raise GateFormatError(f"{where}: {path}: must map rule ids to entries")
        for rule, entry in by_rule.items():
            key = f"{where}: {path} {rule}"
            if rule not in RULES:
                raise GateFormatError(f"{key}: unknown rule")
            if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
                raise GateFormatError(f"{key}: fields must be exactly {sorted(ENTRY_FIELDS)}")
            if not all(isinstance(entry[field], str) and entry[field].strip() for field in ("reason", "removal")):
                raise GateFormatError(f"{key}: reason and removal must be non-empty strings")
            if type(entry["count"]) is not int or entry["count"] < 1:
                raise GateFormatError(f"{key}: count must be a positive integer")
            result[(path, rule)] = entry["count"]
    return result


def load_baseline(path: Path) -> dict[tuple[str, str], int]:
    if not path.exists():
        return {}
    try:
        return parse_baseline(path.read_text(encoding="utf-8"), str(path))
    except OSError as error:
        raise GateFormatError(f"{path}: unreadable baseline: {error}") from error


def base_baseline(root: Path, ref: str) -> dict[tuple[str, str], int] | None:
    """The baseline at ``ref``; ``None`` when ``ref`` has no baseline file yet (first seed)."""

    if subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                      capture_output=True, check=False).returncode != 0:
        raise GateFormatError(f"base ref {ref!r} is not available (fetch it first)")
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", ref, "--", BASELINE_RELATIVE_PATH],
        capture_output=True, text=True, check=False,
    )
    if listed.returncode != 0:
        raise GateFormatError(f"git ls-tree {ref} failed: {listed.stderr.strip()}")
    if not listed.stdout.strip():
        return None
    shown = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{BASELINE_RELATIVE_PATH}"],
                           capture_output=True, text=True, check=False)
    if shown.returncode != 0:
        raise GateFormatError(f"cannot read {ref}:{BASELINE_RELATIVE_PATH}: {shown.stderr.strip()}")
    return parse_baseline(shown.stdout, f"{ref}:{BASELINE_RELATIVE_PATH}")


def compare_with_base(
    current: dict[tuple[str, str], int], base: dict[tuple[str, str], int] | None
) -> list[str]:
    if base is None:
        return []
    errors = []
    for key, count in sorted(current.items()):
        if key not in base:
            errors.append(f"new baseline key {key}: write the code the portable way instead")
        elif count > base[key]:
            errors.append(f"baseline key {key} raised {base[key]} -> {count}")
    return errors


def scan(root: Path, baseline: Path | None = None, base_ref: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    files = _script_files(root)
    findings: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            findings.extend(scan_source(path.read_text(encoding="utf-8"), relative))
        except (OSError, UnicodeError) as error:
            errors.append(f"{relative}: {error}")
    allowed: dict[tuple[str, str], int] = {}
    try:
        allowed = load_baseline(baseline or root / BASELINE_RELATIVE_PATH)
        if base_ref:
            errors.extend(compare_with_base(allowed, base_baseline(root, base_ref)))
    except GateFormatError as error:
        errors.append(str(error))
    advisory = [row for row in findings if row["rule"] in ADVISORY_RULES]
    enforced = [row for row in findings if row["rule"] not in ADVISORY_RULES]
    actual = Counter((row["path"], row["rule"]) for row in enforced)
    new = [
        {"path": key[0], "rule": key[1], "count": count, "baseline": allowed.get(key, 0),
         "lines": [row["line"] for row in enforced if (row["path"], row["rule"]) == key]}
        for key, count in sorted(actual.items()) if count > allowed.get(key, 0)
    ]
    stale = [
        {"path": key[0], "rule": key[1], "count": count, "actual": actual.get(key, 0)}
        for key, count in sorted(allowed.items()) if count > actual.get(key, 0)
    ]
    return {
        "status": "FAIL" if new or stale or errors else "PASS",
        "scanRoots": list(SCAN_ROOTS),
        "filesChecked": len(files),
        "findings": enforced,
        "advisoryFindings": advisory,
        "newFindings": new,
        "staleBaseline": stale,
        "errors": errors,
        "summary": {"findings": len(enforced), "advisory": len(advisory), "new": len(new), "stale": len(stale)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--base-ref", help="git ref of the PR base: no new key, no raised count")
    args = parser.parse_args(argv)
    report = scan(args.root, base_ref=args.base_ref)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "PASS" else 1
    summary = report["summary"]
    print(
        f"node-platform-idioms gate: {report['status']} files={report['filesChecked']} "
        f"findings={summary['findings']} new={summary['new']} stale={summary['stale']} "
        f"advisory={summary['advisory']}"
    )
    for row in report["advisoryFindings"]:
        name, remedy = RULES[row["rule"]]
        print(f"  ADVISORY {row['rule']} {name} {row['path']}:{row['line']} {row['symbol']}; use: {remedy}")
    for row in report["newFindings"]:
        name, remedy = RULES[row["rule"]]
        print(f"  NEW {row['rule']} {name} {row['path']} ({row['count']} > {row['baseline']}) "
              f"lines {row['lines']}; use: {remedy}")
    for row in report["staleBaseline"]:
        print(f"  STALE {row['rule']} {row['path']} baseline {row['count']} > actual {row['actual']}")
    for error in report["errors"]:
        print(f"  ERROR {error}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
