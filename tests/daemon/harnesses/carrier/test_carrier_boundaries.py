"""G1-G3, G7: the carrier contract stays harness-neutral and layered.

G1 and G7 pin the import surface of the two new daemon modules.  G2 proves
the message authority never imports the carrier package.  G3 proves the only
places that build an ``intent="reply"`` inbox message are the two mechanism
answer functions (ruling 58/59); the ordinary model path never replies.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"

CONTRACT = SRC / "daemon/impl/harnesses/carrier/contract.py"
SETTLEMENT = SRC / "daemon/impl/inbox/contracts/settlement.py"
TURNS = SRC / "daemon/impl/application/actors/sessions/turns.py"

_CONTRACT_ALLOWED = {
    "hyprial.kernel",
    "hyprial.daemon.impl.links.ports",
    "hyprial.daemon.impl.inbox.contracts.api",
    "hyprial.daemon.impl.inbox.contracts.summary",
}
_SETTLEMENT_ALLOWED = {
    "hyprial.kernel",
    "hyprial.daemon.impl.links.ports",
    "hyprial.daemon.impl.inbox.contracts.api",
}

_MECHANISM_FUNCTIONS = {"_answer_hook_request", "_answer_mechanism_result"}
_FORBIDDEN_STRINGS = ("message.reply",)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _stdlib(module: str) -> bool:
    return module.split(".", 1)[0] in sys.stdlib_module_names


def test_contract_and_settlement_import_only_allowed_modules() -> None:
    for path, allowed in ((CONTRACT, _CONTRACT_ALLOWED), (SETTLEMENT, _SETTLEMENT_ALLOWED)):
        for module in _imported_modules(path):
            if _stdlib(module):
                continue
            assert module in allowed, f"{path.name} imports {module}"


def _iter_python(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_links_inbox_application_never_import_carriers() -> None:
    forbidden = "hyprial.daemon.impl.harnesses.carrier"
    for directory in ("links", "inbox", "application"):
        for path in _iter_python(SRC / "daemon/impl" / directory):
            for module in _imported_modules(path):
                assert not module.startswith(forbidden), f"{path} imports {module}"


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _iter_calls(func: ast.AST):
    stack = list(getattr(func, "body", ()))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _functions(path: Path) -> list[ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_only_mechanism_functions_build_a_reply() -> None:
    runtime = SRC / "daemon/impl/runtime"
    carrier = SRC / "daemon/impl/harnesses/carrier"
    reply_builders: list[str] = []
    answer_callers: list[str] = []
    settle_calls = 0
    for path in list(_iter_python(runtime)) + list(_iter_python(carrier)):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("_reply_and_ack", "hyprial-agent-sdk-reply"):
            assert forbidden not in text, f"{path} still contains {forbidden}"
        for func in _functions(path):
            if not hasattr(func, "name"):
                continue
            for call in _iter_calls(func):
                name = _callee_name(call)
                if name == "InboxMessage" and any(
                    keyword.arg == "intent"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "reply"
                    for keyword in call.keywords
                ):
                    reply_builders.append(func.name)
                if name in _MECHANISM_FUNCTIONS:
                    answer_callers.append(func.name)
                if name == "_settle_mechanism_answer" and path == (
                    SRC / "daemon/impl/runtime/results.py"
                ):
                    settle_calls += 1
    assert reply_builders, "no mechanism answer constructs a reply"
    assert set(reply_builders) <= _MECHANISM_FUNCTIONS, reply_builders
    assert set(answer_callers) <= {"_settle_mechanism_answer"}, answer_callers
    assert settle_calls == 2, settle_calls
    for path in list(_iter_python(runtime)) + list(_iter_python(carrier)):
        for forbidden in _FORBIDDEN_STRINGS:
            assert forbidden not in path.read_text(encoding="utf-8"), path


def test_contract_names_no_harness() -> None:
    forbidden = (
        "pi_interactive",
        "codex_interactive",
        "claude_interactive",
        "claude-channel",
        "CARRIER_RUNTIMES",
    )
    for path in (CONTRACT, SETTLEMENT, TURNS):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, f"{path.name} contains {name}"
