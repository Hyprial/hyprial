#!/usr/bin/env python3
"""Fail closed when CLI, GUI adapter, or Native code grows daemon-request drift.

The gate treats BFF and adapter processes as clients. It checks declared method
names, dynamic and repeated request orchestration, direct backend authorities,
local state mutations, and process launches. Existing findings live in an exact
shrink-only ledger. CI never regenerates that ledger to admit new findings.

Native scanning skips inline `#[cfg(test)]` BLOCK items (`mod name { ... }`,
`fn ... { ... }`, `impl`/`struct`/`enum`/`trait`/`union` with a body, including
stacked `#[test]` / `#[tokio::test]` attributes and `#[cfg(all(test, ...))]` /
`#[cfg(any(test, ...))]`). Test-only code is not a production process or
daemon-request surface. Semicolon-terminated `#[cfg(test)]` items (`mod name;`,
`use ...;`, `const ...;`, `static ...;`, `type ...;`, `struct NAME;`, bodyless
`fn ...;`) are retained verbatim and scanning resumes after the top-level
semicolon; retention can only over-count, never under-count. The declaration
form `#[cfg(test)] mod name;` stays in place, so a test module compiled from
another file is still scanned as its own file. Terminator-aware classification
fixes review finding P2-1 (`/private/tmp/eosr-arch-ratchet-review.md`), where a
semicolon-form `#[cfg(test)]` item caused following production items to be
deleted.

Production and smoke sites that legitimately launch a process or write a
checkpoint are tracked in `REVIEWED_NATIVE_EXCEPTIONS`, a bounded registry keyed
by the exact `(kind, path, subject)` triple and capped by a count. An occurrence
above the bound stays a violation; an entry must be lowered in a reviewed change
when code shrinks and deleted when the site disappears. The registry is applied
only in `check()`; `scan()` and `write_baseline()` keep reporting raw findings so
the ledger can never absorb new growth.

A native process invocation is `Command::new(` (std or tokio) or a direct
`posix_spawn(`/`posix_spawnp(` call. The shell launcher splits its OS process
launch sites out of `src/child.rs` (whose entry is removed) into
`src/platform/unix.rs` (count=2: Linux std `Command::new` and macOS
`posix_spawnp` with `POSIX_SPAWN_CLOEXEC_DEFAULT`; GUI line review, round 2)
and `src/platform/windows.rs` (count=1: Windows Job Object `Command::new`;
W1C-LAUNCHER, PR #3058).

Reviewed exceptions were approved by the repository owner (Allen, Lark,
2026-11-14T03:32:04Z, messageId 83ede7ac-9b7e-5639-b947-9c7ad6c5a6af;
Windows launcher Job Object split approved via t50 for PR #3058).

Reviewed CLI exceptions live in `REVIEWED_CLI_EXCEPTIONS`, a separate registry
with the same `(kind, path, subject)` keys and count bounds, applied in the same
`_reviewed_rows`.  It is kept apart from the native registry so its growth is
visible in review, and its exact contents are pinned by a test.  Every row is
debt with an exit condition, not a permanent allowance.

S3c (2026-11-24): `agent tiers probe` imports
`hyprial.daemon:probe_capabilities` in place of the retired
`probe_profile`/`UserProfileError` edges and no longer sends the `ps` request
of the deleted `deliveryAgentLive` check, so `tiers.py` reports one backend
import and one command without a request (the ledger loses three rows).
Both rows are bounded to one occurrence in `REVIEWED_CLI_EXCEPTIONS`.  Exit:
a future daemon method `agent.tiers.probe` (with the recorded view served by
a read method) runs the probe in the daemon; both rows are deleted then.
Approved by the repository owner: Allen 2026-11-24 11:34 (cm-coord3
TRACK-C-QUESTIONS-FOR-ALLEN.md).

Model configuration stage 4b retains the design's offline operation-capacity
setting. One config/settings.py import writes that private setting through its
public authority; no online business method, credential or binding write is
exempted. Remove this row if the offline setting is retired or its authority moves.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_RELATIVE_PATH = "scripts/ci/daemon-request-architecture-baseline.json"
REVIEWED_BASELINE_SHA256 = "37a29315ac33988c74e004f00e402256bb71342398d30a5aff3c1e06007ba4eb"
FIRST_SEED_BASES = frozenset({"dev", "main"})
PYTHON_ROOTS = (
    "src/hyprial/shell/impl/cli/commands",
    "src/hyprial/shell/impl/gui/live",
    "src/hyprial/shell/impl/gui/adapter",
    "src/hyprial/shell/impl/gui/backend/onboarding",
)
NATIVE_ROOT = "clients/native"
BACKEND_HEADS = ("hyprial.daemon", "hyprial.biz", "hyprial.identity", "hyprial.kernel.impl")
AUTHORITY_IMPORTS = frozenset({"sqlite3", "lark_oapi", "httpx", "zenoh", "casbin", "pykka", "pycrdt"})
DIRECT_REQUEST_NAMES = frozenset({"daemon_request", "daemon_request_once", "_daemon_request"})
REQUEST_ATTRIBUTES = frozenset({"request", "request_once", "daemon_request", "daemon_request_once"})
WRAPPER_NAMES = frozenset({"_call", "_call_result", "run_owner_key_request"})
FILE_MUTATIONS = frozenset({
    "write_text", "write_bytes", "mkdir", "unlink", "rename", "replace", "remove",
    "rmdir", "chmod", "open", "replace", "symlink_to", "link_to", "touch",
    "rmtree", "mkdtemp", "mkstemp", "TemporaryDirectory", "link",
})
PROCESS_ATTRIBUTES = frozenset({"Popen", "run", "call", "check_call", "check_output", "run_async"})
METHOD_PATTERN = re.compile(r"^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")
NATIVE_CLI_PATTERN = r"\b(?:invoke_json(?:_cancellable|_stdin)?|step)\s*\("
NATIVE_PROCESS_PATTERN = r"\b(?:(?:tokio::process::Command|std::process::Command|Command)::new|posix_spawnp?)\s*\("
NATIVE_CHECKPOINT_PATTERN = r"\b\w+\.write\s*\(\s*home\s*\)"
CFG_ATTRIBUTE = re.compile(r"#\s*\[\s*cfg\s*\(")
REVIEWED_NATIVE_EXCEPTIONS: dict[tuple[str, str, str], int] = {
    ("native-process-invocation", "clients/native/launcher/src/platform/unix.rs", NATIVE_PROCESS_PATTERN): 2,
    ("native-process-invocation", "clients/native/launcher/src/platform/windows.rs", NATIVE_PROCESS_PATTERN): 1,
}
REVIEWED_CLI_EXCEPTIONS: dict[tuple[str, str, str], int] = {
    ("backend-import", "src/hyprial/shell/impl/cli/commands/agent/runtime/tiers.py", "hyprial.daemon:probe_capabilities"): 1,
    ("command-without-request", "src/hyprial/shell/impl/cli/commands/agent/runtime/tiers.py", "tiers.py"): 1,
    ("backend-import", "src/hyprial/shell/impl/cli/commands/config/settings.py", "hyprial.daemon:write_model_binding_operation_limit"): 1,
}


@dataclass(frozen=True, order=True)
class Finding:
    kind: str
    path: str
    subject: str

    def key(self) -> tuple[str, str, str]:
        return self.kind, self.path, self.subject


@dataclass(frozen=True)
class RequestSite:
    node: ast.Call
    owner: str
    method: ast.AST | None
    dynamic: bool


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0


def _rows(findings: list[Finding]) -> list[dict[str, Any]]:
    counts = Counter(finding.key() for finding in findings)
    return [
        {"kind": kind, "path": path, "subject": subject, "count": count}
        for (kind, path, subject), count in sorted(counts.items())
    ]


def _summary(findings: list[dict[str, Any]]) -> dict[str, int]:
    result: Counter[str] = Counter()
    for row in findings:
        result[row["kind"]] += row["count"]
    return dict(sorted(result.items()))


def _baseline_document(findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "policy": "Exact client request-architecture findings with occurrence counts. Entries may only leave or shrink; new platform exceptions require a reviewed checker change, never ledger growth.",
        "findings": findings,
    }


def _load_baseline(path: Path) -> dict[tuple[str, str, str], int]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("baseline version must be 1")
        raw = document["findings"]
        if not isinstance(raw, list):
            raise ValueError("findings must be an array")
        result: dict[tuple[str, str, str], int] = {}
        for index, row in enumerate(raw):
            if not isinstance(row, dict) or set(row) != {"kind", "path", "subject", "count"}:
                raise ValueError(f"invalid finding #{index}")
            key = (row["kind"], row["path"], row["subject"])
            count = row["count"]
            if not all(isinstance(value, str) and value for value in key):
                raise ValueError(f"invalid finding key #{index}")
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise ValueError(f"invalid finding count #{index}")
            if key in result:
                raise ValueError(f"duplicate finding {key}")
            result[key] = count
        return result
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read baseline {path}: {error}") from error


def _load_git_baseline(root: Path, ref: str) -> dict[tuple[str, str, str], int] | None:
    if not _ref_exists(root, ref):
        raise ValueError(f"base ref does not exist: {ref}")
    exists = _git(root, "cat-file", "-e", f"{ref}:{BASELINE_RELATIVE_PATH}")
    if exists.returncode != 0:
        return None
    result = _git(root, "show", f"{ref}:{BASELINE_RELATIVE_PATH}")
    if result.returncode != 0:
        raise ValueError(f"cannot read {BASELINE_RELATIVE_PATH} from {ref}")
    temporary = root / ".daemon-request-architecture-base.json"
    try:
        temporary.write_text(result.stdout, encoding="utf-8")
        return _load_baseline(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _catalog(root: Path) -> set[str]:
    path = root / "contract/daemon-request/openapi.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return {
            row["name"]
            for row in document["x-hyprial-methods"]
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read daemon request catalog: {error}") from error


def _module_target(relative: str, module: str | None, level: int) -> str | None:
    if not level:
        return module
    parts = relative.removesuffix(".py").split("/")[:-1]
    try:
        return importlib.util.resolve_name("." * level + (module or ""), ".".join(parts))
    except (ImportError, ValueError):
        return None


def _owner_map(tree: ast.AST) -> dict[int, str]:
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    owners: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        names: list[str] = []
        current: ast.AST = node
        while id(current) in parents:
            current = parents[id(current)]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                names.append(current.name if not isinstance(current, ast.Lambda) else "<lambda>")
        owners[id(node)] = ".".join(reversed(names)) or "<module>"
    return owners


def _parameters(node: ast.AST) -> list[str]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return []
    args = node.args
    return [argument.arg for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _function_nodes(tree: ast.AST) -> list[ast.AST]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    ]


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _literal_method(node: ast.Call) -> tuple[str | None, ast.AST | None]:
    keyword = next((item.value for item in node.keywords if item.arg == "method"), None)
    raw = keyword if keyword is not None else node.args[0] if node.args else None
    if raw is None:
        return None, None
    if isinstance(raw, ast.Constant) and isinstance(raw.value, str):
        return raw.value, raw
    return None, raw


def _is_direct_request(node: ast.Call) -> bool:
    name = _call_name(node)
    if name in DIRECT_REQUEST_NAMES or name in WRAPPER_NAMES:
        return True
    return name in REQUEST_ATTRIBUTES


def _request_sites(tree: ast.AST) -> list[RequestSite]:
    owners = _owner_map(tree)
    functions: dict[str, ast.AST] = {}
    for function in _function_nodes(tree):
        first = next((item for item in ast.walk(function) if isinstance(item, ast.Call)), None)
        if first is not None:
            functions[owners[id(first)]] = function
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    called_names = {_call_name(node) for node in calls}
    wrappers: dict[str, int] = {}
    for _ in range(4):
        changed = False
        for function in _function_nodes(tree):
            if getattr(function, "name", "<lambda>") in wrappers or getattr(function, "name", "<lambda>") not in called_names:
                continue
            parameters = _parameters(function)
            if not parameters:
                continue
            for call in ast.walk(function):
                if not isinstance(call, ast.Call):
                    continue
                direct = _is_direct_request(call)
                known = _call_name(call) in wrappers
                if not direct and not known:
                    continue
                if call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == parameters[0]:
                    wrappers[getattr(function, "name", "<lambda>")] = 0
                    changed = True
                    break
        if not changed:
            break
    sites: list[RequestSite] = []
    for node in calls:
        direct = _is_direct_request(node)
        wrapper = _call_name(node) in wrappers
        if not direct and not wrapper:
            continue
        literal, raw = _literal_method(node)
        owner = owners.get(id(node), "<module>")
        function = functions.get(owner)
        wrapper_definition = bool(
            direct
            and function is not None
            and getattr(function, "name", "<lambda>") in wrappers
            and isinstance(raw, ast.Name)
            and _parameters(function)
            and raw.id == _parameters(function)[0]
        )
        dynamic = raw is not None and literal is None and not wrapper_definition
        sites.append(RequestSite(node, owner, raw, dynamic))
    return sites


def _inside_loop(tree: ast.AST, target: ast.Call) -> bool:
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    current: ast.AST = target
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, (ast.For, ast.AsyncFor, ast.While)):
            return True
    return False


def _scan_python(path: Path, relative: str, catalog: set[str]) -> tuple[list[Finding], bool]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError, UnicodeError):
        return [Finding("unreadable-source", relative, path.name)], False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _module_target(relative, node.module, node.level) or ""
            for alias in node.names:
                full = f"{target}.{alias.name}" if target else alias.name
                if target.startswith(BACKEND_HEADS) or full.startswith(BACKEND_HEADS):
                    findings.append(Finding("backend-import", relative, f"{target}:{alias.name}"))
                if target in AUTHORITY_IMPORTS or full in AUTHORITY_IMPORTS:
                    findings.append(Finding("authority-import", relative, full))
                if target == "subprocess" and full.rsplit(".", 1)[-1] in PROCESS_ATTRIBUTES:
                    findings.append(Finding("process-invocation", relative, full))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(BACKEND_HEADS):
                    findings.append(Finding("backend-import", relative, alias.name))
                if alias.name.split(".")[0] in AUTHORITY_IMPORTS:
                    findings.append(Finding("authority-import", relative, alias.name))
                if alias.name == "subprocess":
                    findings.append(Finding("process-invocation", relative, "subprocess"))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in FILE_MUTATIONS:
                findings.append(Finding("file-mutation", relative, name))
            if isinstance(node.func, ast.Attribute) and name in PROCESS_ATTRIBUTES:
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id == "subprocess":
                    findings.append(Finding("process-invocation", relative, f"subprocess.{name}"))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    target = node.args[0].value
                    if target.startswith(BACKEND_HEADS):
                        findings.append(Finding("backend-import", relative, target))
                else:
                    findings.append(Finding("dynamic-import", relative, "import_module"))
            if name == "__import__":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    target = node.args[0].value
                    if target.startswith(BACKEND_HEADS):
                        findings.append(Finding("backend-import", relative, target))
                else:
                    findings.append(Finding("dynamic-import", relative, "__import__"))
    requests = _request_sites(tree)
    grouped: Counter[str] = Counter()
    for site in requests:
        grouped[site.owner] += 1
        literal = None
        if isinstance(site.method, ast.Constant) and isinstance(site.method.value, str):
            literal = site.method.value
        if site.dynamic:
            findings.append(Finding("dynamic-method", relative, site.owner))
        elif literal is not None and (not METHOD_PATTERN.fullmatch(literal) or literal not in catalog):
            findings.append(Finding("undeclared-method", relative, f"{site.owner}:{literal}"))
        if _inside_loop(tree, site.node):
            findings.append(Finding("request-in-loop", relative, site.owner))
    for owner, count in grouped.items():
        if count > 1:
            findings.extend(Finding("request-fanout", relative, owner) for _ in range(count))
    has_command = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(decorator, ast.Call) and _call_name(decorator) == "command" for decorator in node.decorator_list)
        for node in tree.body
    )
    if has_command and not requests:
        findings.append(Finding("command-without-request", relative, path.name))
    return findings, True


def _rust_code(text: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(line for line in without_blocks.splitlines() if not line.lstrip().startswith("//"))


def _skip_ws_comments(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        break
    return i


def _skip_literal(text: str, i: int) -> int:
    n = len(text)
    ch = text[i]
    if ch == '"':
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"':
                return j + 1
            j += 1
        return n
    if ch == "'":
        j = i + 1
        if j < n and text[j] == "\\":
            j += 2
        else:
            j += 1
        if j < n and text[j] == "'":
            return j + 1
        return i
    if ch == "r":
        m = re.match(r'r(#*)"', text[i:])
        if m:
            marker = '"' + m.group(1)
            j = text.find(marker, i + m.end())
            return n if j < 0 else j + len(marker)
    return i


def _find_matching_brace(text: str, i: int) -> int:
    depth = 0
    n = len(text)
    while i < n:
        literal_end = _skip_literal(text, i)
        if literal_end != i:
            i = literal_end
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_item_terminator(text: str, i: int, *, group_braces: bool) -> tuple[str, int]:
    n = len(text)
    while i < n:
        i = _skip_ws_comments(text, i)
        if i >= n:
            break
        literal_end = _skip_literal(text, i)
        if literal_end != i:
            i = literal_end
            continue
        ch = text[i]
        if ch == ";":
            return ";", i
        if ch == "{":
            brace_end = _find_matching_brace(text, i)
            if brace_end < 0:
                return "", -1
            if not group_braces:
                return "{", i
            i = brace_end + 1
            continue
        i += 1
    return "", -1


def _strip_cfg_test_items(text: str) -> str:
    out = []
    i = 0
    n = len(text)
    while True:
        m = CFG_ATTRIBUTE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i : m.start()])
        depth = 0
        k = m.end() - 1
        while k < n:
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= n:
            out.append(text[m.start() :])
            break
        inner = text[m.end() : k]
        is_test = bool(
            re.fullmatch(r"\s*test\s*", inner)
            or re.match(r"\s*(?:all|any)\s*\(\s*test\b", inner)
        )
        if not is_test:
            out.append(text[m.start() : k + 1])
            i = k + 1
            continue
        j = k + 1
        while j < n and text[j].isspace():
            j += 1
        if j < n and text[j] == "]":
            j += 1
        while True:
            j = _skip_ws_comments(text, j)
            if text.startswith("#[", j):
                d = 0
                q = j + 1
                while q < n:
                    if text[q] == "[":
                        d += 1
                    elif text[q] == "]":
                        d -= 1
                        if d == 0:
                            break
                    q += 1
                j = q + 1
                continue
            break
        mm = re.match(
            r'(?:(?:pub(?:\([^)]*\))?|unsafe|async|const|extern(?:\s+"[^"]*")?)\s+)*',
            text[j:],
        )
        j2 = j + (mm.end() if mm else 0)
        item = re.match(
            r"(mod|fn|impl|struct|enum|trait|union|use|const|static|type)\b",
            text[j2:],
        )
        offset = j2
        if not item:
            item = re.match(
                r"(mod|fn|impl|struct|enum|trait|union|use|const|static|type)\b",
                text[j:],
            )
            offset = j
        if not item:
            out.append(text[m.start() : k + 1])
            i = k + 1
            continue
        terminator, position = _find_item_terminator(
            text,
            offset,
            group_braces=item.group(1) in {"use", "const", "static", "type"},
        )
        if terminator == ";":
            out.append(text[m.start() : position + 1])
            i = position + 1
            continue
        if terminator != "{":
            out.append(text[m.start() :])
            break
        end = _find_matching_brace(text, position)
        if end < 0:
            out.append(text[m.start() :])
            break
        i = end + 1
    return "".join(out)


def _scan_native(path: Path, relative: str) -> list[Finding]:
    try:
        code = _strip_cfg_test_items(_rust_code(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError):
        return [Finding("unreadable-source", relative, path.name)]
    findings: list[Finding] = []
    patterns = (
        ("native-cli-invocation", NATIVE_CLI_PATTERN),
        ("native-process-invocation", NATIVE_PROCESS_PATTERN),
        ("native-checkpoint-write", NATIVE_CHECKPOINT_PATTERN),
    )
    for kind, pattern in patterns:
        findings.extend(Finding(kind, relative, pattern) for _ in re.findall(pattern, code))
    return findings


def scan(root: Path) -> dict[str, Any]:
    root = root.resolve()
    try:
        catalog = _catalog(root)
    except ValueError as error:
        row = {"kind": "invalid-catalog", "path": "contract/daemon-request/openapi.json", "subject": str(error), "count": 1}
        return {"status": "FAIL", "findings": [row], "violations": [row], "summary": {"invalid-catalog": 1}}
    findings: list[Finding] = []
    for prefix in PYTHON_ROOTS:
        base = root / prefix
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            found, _ = _scan_python(path, relative, catalog)
            findings.extend(found)
    native = root / NATIVE_ROOT
    if native.exists():
        for path in sorted(native.rglob("*.rs")):
            relative = path.relative_to(root).as_posix()
            parts = path.relative_to(native).parts
            if "tests" in parts or path.name in {"tests.rs", "test.rs"} or path.name.endswith("_tests.rs"):
                continue
            findings.extend(_scan_native(path, relative))
    rows = _rows(findings)
    return {
        "status": "FAIL" if rows else "PASS",
        "findings": rows,
        "violations": rows,
        "summary": _summary(rows),
    }


def _current_problems(
    current: dict[tuple[str, str, str], int],
    baseline: dict[tuple[str, str, str], int],
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for key, count in sorted(current.items()):
        previous = baseline.get(key)
        kind, path, subject = key
        if previous is None:
            problems.append({"kind": "new-architecture-finding", "path": path, "subject": subject, "finding": kind, "count": count})
        elif count > previous:
            problems.append({"kind": "architecture-finding-grew", "path": path, "subject": subject, "finding": kind, "count": count, "baseline": previous})
        elif count < previous:
            problems.append({"kind": "stale-baseline-entry", "path": path, "subject": subject, "finding": kind, "count": count, "baseline": previous})
    for key, previous in sorted(baseline.items()):
        if key not in current:
            kind, path, subject = key
            problems.append({"kind": "stale-baseline-entry", "path": path, "subject": subject, "finding": kind, "count": 0, "baseline": previous})
    return problems


def _base_problems(
    current: dict[tuple[str, str, str], int],
    baseline: dict[tuple[str, str, str], int],
    base: dict[tuple[str, str, str], int],
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for key, count in sorted(current.items()):
        if count > base.get(key, 0):
            kind, path, subject = key
            problems.append({"kind": "base-architecture-growth", "path": path, "subject": subject, "finding": kind, "count": count, "base": base.get(key, 0)})
    for key, count in sorted(baseline.items()):
        if count > base.get(key, 0):
            kind, path, subject = key
            problems.append({"kind": "ledger-growth", "path": path, "subject": subject, "finding": kind, "count": count, "base": base.get(key, 0)})
    return problems


def _as_counts(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    return {(row["kind"], row["path"], row["subject"]): row["count"] for row in rows}


def _reviewed_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for row in rows:
        key = (row["kind"], row["path"], row["subject"])
        allowed = REVIEWED_NATIVE_EXCEPTIONS.get(key, 0) or REVIEWED_CLI_EXCEPTIONS.get(key, 0)
        if not allowed:
            kept.append(row)
            continue
        applied = min(row["count"], allowed)
        remaining = row["count"] - applied
        audit.append({
            "kind": row["kind"],
            "path": row["path"],
            "subject": row["subject"],
            "allowed": allowed,
            "observed": row["count"],
            "applied": applied,
        })
        if remaining:
            kept.append({**row, "count": remaining})
    return kept, audit


def _first_seed_problems(baseline: Path, base_branch: str) -> list[dict[str, Any]]:
    if base_branch not in FIRST_SEED_BASES:
        return [{"kind": "missing-base-ledger", "detail": "first seed is limited to dev/main"}]
    if _digest(baseline) != REVIEWED_BASELINE_SHA256:
        return [{"kind": "seed-digest-mismatch", "path": BASELINE_RELATIVE_PATH}]
    return []


def check(
    root: Path,
    baseline_path: Path,
    *,
    base_ref: str | None = None,
    base_branch: str | None = None,
    allow_first_seed: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    scanned = scan(root)
    reviewed, reviewed_audit = _reviewed_rows(scanned["findings"])
    current = _as_counts(reviewed)
    violations: list[dict[str, Any]] = []
    try:
        baseline = _load_baseline(baseline_path)
    except ValueError as error:
        return {
            "status": "FAIL",
            "violations": [{"kind": "invalid-baseline", "detail": str(error)}],
            "findings": scanned["findings"],
            "summary": scanned["summary"],
        }
    violations.extend(
        row
        for row in reviewed
        for key in [(row["kind"], row["path"], row["subject"])]
        if key not in baseline or row["count"] > baseline[key]
    )
    violations.extend(_current_problems(current, baseline))
    first_seed = False
    if base_ref is not None:
        try:
            base = _load_git_baseline(root, base_ref)
        except ValueError as error:
            violations.append({"kind": "invalid-base-ref", "detail": str(error)})
        else:
            if base is None:
                if allow_first_seed:
                    seed_problems = _first_seed_problems(baseline_path, base_branch or "")
                    if seed_problems:
                        violations.extend(seed_problems)
                    else:
                        first_seed = True
                else:
                    violations.append(
                        {
                            "kind": "missing-base-ledger",
                            "detail": f"{BASELINE_RELATIVE_PATH} is absent at {base_ref}",
                        }
                    )
            else:
                violations.extend(_base_problems(current, baseline, base))
    return {
        "status": "FAIL" if violations else "PASS",
        "baseRef": base_ref,
        "firstSeedUsed": first_seed,
        "findings": reviewed,
        "summary": _summary(reviewed),
        "rawSummary": scanned["summary"],
        "reviewedExceptions": reviewed_audit,
        "violations": violations,
    }


def write_baseline(root: Path, baseline_path: Path) -> dict[str, Any]:
    root = root.resolve()
    scanned = scan(root)
    current = _as_counts(scanned["findings"])
    if baseline_path.exists():
        try:
            previous = _load_baseline(baseline_path)
        except ValueError as error:
            return {"status": "FAIL", "violations": [{"kind": "invalid-baseline", "detail": str(error)}]}
        problems = [
            row for row in _current_problems(current, previous)
            if row["kind"] in {"new-architecture-finding", "architecture-finding-grew"}
        ]
        if problems:
            return {"status": "FAIL", "violations": problems}
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(_baseline_document(scanned["findings"]), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {"status": "PASS", "violations": [], "findings": scanned["findings"], "summary": scanned["summary"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--base-ref")
    parser.add_argument("--base-branch")
    parser.add_argument("--allow-first-seed", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    baseline = args.baseline or root / BASELINE_RELATIVE_PATH
    if args.write_baseline:
        report = write_baseline(root, baseline)
    else:
        report = check(
            root,
            baseline,
            base_ref=args.base_ref,
            base_branch=args.base_branch,
            allow_first_seed=args.allow_first_seed,
        )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{report['status']} daemon request architecture")
        for row in report["violations"]:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
