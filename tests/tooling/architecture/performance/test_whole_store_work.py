"""Reject whole-store work from paths that should be proportional to one operation."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from collections import Counter
from typing import Iterable

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"

# Each spelling below is a whole-document operation, not a row/key lookup.
WHOLE_DOCUMENT_LOADERS = (
    "desired_state.load",  # Deserializes the daemon's complete desired-state document.
    "DesiredState.from_json",  # Parses a complete serialized desired-state document.
    "session_actor:self.store.load",  # Loads the session actor's complete store snapshot.
)

# Named from the PAC, lifecycle, and inbox schemas in src/hyprial.
GROWING_TABLES = (
    "notifications",
    "graphs",
    "nodes",
    "flag_events",
    "journal",
    "lifecycle_effects",
    "lifecycle_operations",
    "outbox",
    "inbox",
    "dlq",
    "custody",
    "system_notices",
    "progress_events",
)

# Each entry ends with the number of occurrences it permits: a second
# violation inside an allowlisted function is new work, not the old debt.
ALLOWLIST: tuple[tuple[str | int, ...], ...] = (
    # Card af23508b: the legacy delivery lane still filters after LIMIT 128.
    (
        "src/hyprial/daemon/impl/pac/graphs/authority/__init__.py",
        "_scan_deliveries",
        "R3",
        1,
    ),
    # 0.5.5.2 startup restores bindings from the one desired-state document;
    # the two loops call per-entity helpers that re-read the same snapshot.
    (
        "src/hyprial/daemon/impl/application/actors/agents_registry.py",
        "_seed_agent_bindings",
        "R1",
        2,
    ),
)

_SQL_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_SQL_WHERE = re.compile(
    r"\bWHERE\b(?P<where>.*?)(?:\bORDER\s+BY\b|\bGROUP\s+BY\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    function: str
    rule: str
    line: int
    detail: str
    fingerprint: str | None = None

    @property
    def allowlist_key(self) -> tuple[str, ...]:
        key = (self.path, self.function, self.rule)
        return (*key, self.fingerprint) if self.fingerprint is not None else key

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.function}:{self.rule}: {self.detail}"


def _attribute_parts(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _scoped_walk(nodes: Iterable[ast.AST]) -> Iterable[ast.AST]:
    """Walk executable nodes without entering a nested definition's body."""

    pending = list(reversed(tuple(nodes)))
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _straight_line_walk(nodes: Iterable[ast.AST]) -> Iterable[ast.AST]:
    """Walk a helper while leaving repeated work to the helper's own R1 scan."""

    repeated = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )
    pending = list(reversed(tuple(nodes)))
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, repeated):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _is_whole_document_loader(call: ast.Call, path: str) -> bool:
    parts = _attribute_parts(call.func)
    if len(parts) >= 2 and parts[-1] == "load":
        loader_name = f"{parts[-2].lstrip('_')}.{parts[-1]}"
        if loader_name == WHOLE_DOCUMENT_LOADERS[0]:
            return True
        if (
            parts[-2:] == ("store", "load")
            and parts[:1] == ("self",)
            and path.endswith("/daemon/impl/transport/session_actor.py")
            and WHOLE_DOCUMENT_LOADERS[2].endswith("self.store.load")
        ):
            return True
    if ".".join(parts[-2:]) == WHOLE_DOCUMENT_LOADERS[1]:
        return True
    return False

def _function_definitions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _direct_loader_functions(
    definitions: Iterable[ast.FunctionDef | ast.AsyncFunctionDef], path: str
) -> set[str]:
    return {
        definition.name
        for definition in definitions
        if any(
            isinstance(node, ast.Call)
            and _is_whole_document_loader(node, path)
            for node in _straight_line_walk(definition.body)
        )
    }


def _calls_same_module_loader(call: ast.Call, loader_functions: set[str]) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id in loader_functions
    if not isinstance(call.func, ast.Attribute) or call.func.attr not in loader_functions:
        return False
    return isinstance(call.func.value, ast.Name) and call.func.value.id in {"self", "cls"}


def _loop_repeated_nodes(node: ast.AST) -> Iterable[ast.AST]:
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return _scoped_walk(node.body)
    if isinstance(node, ast.DictComp):
        roots: list[ast.AST] = [node.key, node.value]
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        roots = [node.elt]
    else:
        return ()
    roots.extend(condition for generator in node.generators for condition in generator.ifs)
    # The first iterable is evaluated once. Later iterables are evaluated per
    # outer item and therefore belong to the repeated work.
    roots.extend(generator.iter for generator in node.generators[1:])
    return _scoped_walk(roots)


def _r1_findings(
    definition: ast.FunctionDef | ast.AsyncFunctionDef,
    path: str,
    loader_functions: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    loop_types = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp)
    loop_types += (ast.DictComp, ast.GeneratorExp)
    for loop in _scoped_walk(definition.body):
        if not isinstance(loop, loop_types):
            continue
        for node in _loop_repeated_nodes(loop):
            if not isinstance(node, ast.Call):
                continue
            direct = _is_whole_document_loader(node, path)
            indirect = _calls_same_module_loader(node, loader_functions)
            if direct or indirect:
                detail = "whole-document loader runs once per loop item"
                if indirect and not direct:
                    detail = "same-module helper with a whole-document loader runs in a loop"
                findings.append(
                    Finding(
                        path,
                        definition.name,
                        "R1",
                        node.lineno,
                        detail,
                    )
                )
    return findings


def _literal_sql(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _execute_sql(node: ast.AST) -> str | None:
    current = node
    if (
        isinstance(current, ast.Call)
        and isinstance(current.func, ast.Attribute)
        and current.func.attr == "fetchall"
    ):
        current = current.func.value
    if not isinstance(current, ast.Call) or not isinstance(current.func, ast.Attribute):
        return None
    if current.func.attr != "execute" or not current.args:
        return None
    return _literal_sql(current.args[0])


def _queries_by_name(
    definition: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, str]:
    queries: dict[str, str] = {}
    for node in _scoped_walk(definition.body):
        target: ast.AST | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and value is not None:
            sql = _execute_sql(value)
            if sql is not None:
                queries[target.id] = sql
    return queries


def _iteration_sql(node: ast.AST, queries: dict[str, str]) -> str | None:
    direct = _execute_sql(node)
    if direct is not None:
        return direct
    if isinstance(node, ast.Name):
        return queries.get(node.id)
    return None


def _touches_growing_table(sql: str) -> bool:
    tables = {match.group(1).lower() for match in _SQL_TABLE.finditer(sql)}
    return bool(tables.intersection(GROWING_TABLES))


def _json_loaded_columns(nodes: Iterable[ast.AST], row_name: str) -> set[str]:
    columns: set[str] = set()
    for root in nodes:
        for call in _scoped_walk((root,)):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            if _attribute_parts(call.func)[-2:] != ("json", "loads"):
                continue
            for node in _scoped_walk((call.args[0],)):
                if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
                    continue
                if node.value.id != row_name:
                    continue
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    columns.add(node.slice.value)
    return columns


def _compared_columns(nodes: Iterable[ast.AST], row_name: str) -> set[str]:
    columns: set[str] = set()
    for root in nodes:
        for comparison in _scoped_walk((root,)):
            if not isinstance(comparison, ast.Compare):
                continue
            for node in _scoped_walk((comparison,)):
                if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
                    continue
                if node.value.id != row_name:
                    continue
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    columns.add(node.slice.value)
    return columns


def _where_clause(sql: str) -> str | None:
    match = _SQL_WHERE.search(sql)
    return match.group("where") if match is not None else None


def _python_filter_is_sql_constrained(
    sql: str, filters: tuple[ast.AST, ...], row_name: str
) -> bool:
    where = _where_clause(sql)
    if where is None:
        return False
    lowered = where.lower()
    json_columns = _json_loaded_columns(filters, row_name)
    if json_columns:
        if any(column.lower() not in lowered for column in json_columns):
            return False
        return "json_extract" in lowered
    columns = _compared_columns(filters, row_name)
    return bool(columns) and all(
        re.search(rf"\b{re.escape(column.lower())}\b", lowered) for column in columns
    )


def _conditional_continue_tests(loop: ast.For | ast.AsyncFor) -> tuple[ast.AST, ...]:
    tests: list[ast.AST] = []

    def visit(nodes: Iterable[ast.AST]) -> None:
        for node in nodes:
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, ast.If):
                if any(isinstance(item, ast.Continue) for item in _scoped_walk(node.body)):
                    tests.append(node.test)
            visit(ast.iter_child_nodes(node))

    visit(loop.body)
    return tuple(tests)


def _row_name(target: ast.AST) -> str | None:
    return target.id if isinstance(target, ast.Name) else None


def _r2_r3_findings(
    definition: ast.FunctionDef | ast.AsyncFunctionDef, path: str
) -> list[Finding]:
    # Schema migrations are deliberately whole-store transforms, not
    # per-operation paths; treating them as runtime scans obscures this guard.
    if "/migrations/" in path:
        return []
    findings: list[Finding] = []
    queries = _queries_by_name(definition)
    for node in _scoped_walk(definition.body):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if not node.generators:
                continue
            generator = node.generators[0]
            sql = _iteration_sql(generator.iter, queries)
            row_name = _row_name(generator.target)
            filters = tuple(generator.ifs)
            line = node.lineno
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            sql = _iteration_sql(node.iter, queries)
            row_name = _row_name(node.target)
            filters = _conditional_continue_tests(node)
            line = node.lineno
        else:
            continue
        if sql is None or row_name is None or not _touches_growing_table(sql):
            continue
        if "LIMIT" in sql.upper() and filters:
            findings.append(
                Finding(path, definition.name, "R3", line, "SQL LIMIT precedes Python continue filtering")
            )
            continue
        # Row comparisons after keyed SQL are often ordinary business
        # decisions. Keep the comparison variant to truly unfiltered scans;
        # JSON decoding remains specific enough to judge its key separately.
        python_filter = bool(_json_loaded_columns(filters, row_name)) or (
            _where_clause(sql) is None and bool(_compared_columns(filters, row_name))
        )
        if python_filter and not _python_filter_is_sql_constrained(sql, filters, row_name):
            findings.append(
                Finding(path, definition.name, "R2", line, "growing-table scan is filtered in Python")
            )
    return findings


def scan_whole_store_work(source_root: Path) -> list[Finding]:
    findings: set[Finding] = set()
    for source in sorted(source_root.rglob("*.py")):
        path = f"src/hyprial/{source.relative_to(source_root).as_posix()}"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        definitions = _function_definitions(tree)
        loader_functions = _direct_loader_functions(definitions, path)
        for definition in definitions:
            findings.update(_r1_findings(definition, path, loader_functions))
            findings.update(_r2_r3_findings(definition, path))
    return sorted(findings)


def _keys(findings: Iterable[Finding]) -> Counter[tuple[str, ...]]:
    return Counter(finding.allowlist_key for finding in findings)


def _allowed_counts() -> dict[tuple[str, ...], int]:
    allowed: dict[tuple[str, ...], int] = {}
    for *key, count in ALLOWLIST:
        assert isinstance(count, int) and count > 0, key
        allowed[tuple(str(part) for part in key)] = count
    return allowed


def test_repository_matches_the_shrinking_allowlist() -> None:
    findings = scan_whole_store_work(SRC)
    actual = _keys(findings)
    allowed = _allowed_counts()
    unexpected = [
        finding.render()
        for finding in findings
        if actual[finding.allowlist_key] > allowed.get(finding.allowlist_key, 0)
    ]
    stale = sorted(key for key, count in allowed.items() if actual[key] < count)
    errors = [*(f"new whole-store work: {item}" for item in unexpected)]
    errors.extend(
        "stale whole-store allowance must shrink now: " + ":".join(item)
        for item in stale
    )
    assert not errors, "whole-store architecture guard failed:\n" + "\n".join(errors)


def test_r1_detects_direct_and_same_module_indirect_loads(tmp_path: Path) -> None:
    source = tmp_path / "agents_registry.py"
    source.write_text(
        "def helper(self):\n"
        "    return self.desired_state.load()\n"
        "def direct(self, rows):\n"
        "    for row in rows:\n"
        "        self.desired_state.load()\n"
        "def indirect(self, rows):\n"
        "    for row in rows:\n"
        "        self.helper()\n",
        encoding="utf-8",
    )
    findings = scan_whole_store_work(tmp_path)
    assert {(finding.function, finding.rule) for finding in findings} == {
        ("direct", "R1"),
        ("indirect", "R1"),
    }


def test_r1_does_not_treat_single_harness_lookup_as_whole_store_work(
    tmp_path: Path,
) -> None:
    source = tmp_path / "agents_registry.py"
    source.write_text(
        "def literal(self, rows):\n"
        "    for row in rows:\n"
        "        self._canonical_harness_uri(row, spec=None)\n"
        "def nullable(self, rows, desired=None):\n"
        "    for row in rows:\n"
        "        self._canonical_harness_uri(row, desired=desired)\n"
        "def initialized(self, rows, desired=None):\n"
        "    desired = self.desired_state.load()\n"
        "    for row in rows:\n"
        "        self._canonical_harness_uri(row, desired=desired)\n",
        encoding="utf-8",
    )
    assert scan_whole_store_work(tmp_path) == []


def test_r1_does_not_treat_repeated_single_harness_reads_as_whole_store_work(
    tmp_path: Path,
) -> None:
    source = tmp_path / "agents_registry.py"
    # Copied verbatim from db85f0bc; the synthetic class supplies its original
    # four-space class indentation while keeping the fixture parseable alone.
    source.write_text(
        "class Fixture:\n"
        '''    def _managed_worker_running(
        self, actor: str, desired: DesiredState | None = None
    ) -> bool | None:
        """Supervised-subprocess state for a daemon-managed worker, else None.

        This is the signal ``connectors[].running`` already reported on its
        own; feeding it into AgentLiveness is what collapses two of the three
        old models into one instead of adding a fourth.  ``desired`` is the
        snapshot's already-loaded desired state (card 259), threaded to
        ``_canonical_harness_uri`` so each row stops re-reading the store.
        """

        snapshot = self._current_worker_snapshot()
        if snapshot is not None:
            return snapshot.running_by_actor.get(actor)
        # Single-call paths (identity.whoami, session fencing) reach here
        # without a request snapshot and keep the live per-call query.
        # Batch paths must install _worker_status_snapshot instead: every
        # call below costs one supervisor round trip plus one desired-state
        # load per connector.
        # Unit-level application.handle seams stand in a bare object for the
        # supervisor; absent a real one there is simply no worker signal.
        report = getattr(self._harnesses, "status", None)
        if not callable(report):
            return None
        statuses = report()
        if not isinstance(statuses, tuple | list):
            return None
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name:
                continue
            if status.get("runtime") == "lark":
                continue
            if (
                self._canonical_harness_uri(name, desired=desired) == actor
                or name == actor
            ):
                return bool(status.get("running"))
        return None
''',
        encoding="utf-8",
    )
    assert scan_whole_store_work(tmp_path) == []


def test_r1_does_not_treat_a_comprehension_iterable_as_repeated_work(tmp_path: Path) -> None:
    source = tmp_path / "clean.py"
    source.write_text(
        "def once(self):\n"
        "    return [item.name for item in self.desired_state.load().harnesses]\n",
        encoding="utf-8",
    )
    assert scan_whole_store_work(tmp_path) == []


def test_r2_detects_json_filter_after_a_growing_table_scan(tmp_path: Path) -> None:
    source = tmp_path / "store.py"
    source.write_text(
        "def lookup(self, graph_id):\n"
        "    return [row for row in self.db.execute(\n"
        "        'SELECT * FROM notifications'\n"
        "    ) if json.loads(row['plan_json'])['graphId'] == graph_id]\n",
        encoding="utf-8",
    )
    findings = scan_whole_store_work(tmp_path)
    assert {(finding.function, finding.rule) for finding in findings} == {("lookup", "R2")}


def test_r3_detects_limit_before_conditional_continue(tmp_path: Path) -> None:
    source = tmp_path / "authority.py"
    source.write_text(
        "def scan(self):\n"
        "    rows = self.db.execute('SELECT * FROM notifications LIMIT 128').fetchall()\n"
        "    for row in rows:\n"
        "        if row['recipient'] != self.recipient:\n"
        "            continue\n",
        encoding="utf-8",
    )
    findings = scan_whole_store_work(tmp_path)
    assert {(finding.function, finding.rule) for finding in findings} == {("scan", "R3")}


def test_a_second_violation_in_an_allowlisted_function_is_new_work(tmp_path: Path) -> None:
    source = tmp_path / "authority.py"
    loop = (
        "    for row in self.db.execute('SELECT * FROM notifications LIMIT {n}'):\n"
        "        if row['kind'] != 'alert':\n"
        "            continue\n"
        "        yield row\n"
    )
    source.write_text(
        "def _scan_deliveries(self):\n" + loop.format(n=128) + loop.format(n=64),
        encoding="utf-8",
    )
    counts = _keys(scan_whole_store_work(tmp_path))
    # Two occurrences of one key: an allowance of 1 would report the second.
    assert list(counts.values()) == [2]

