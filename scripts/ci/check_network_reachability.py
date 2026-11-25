#!/usr/bin/env python3
"""Conservative AST reachability gate from daemon IPC roots to network effects.

The gate resolves local/imported calls exactly and conservatively resolves
mixin and recognized injected-port calls when receiver types are unknown.
Existing root/caller/boundary paths live in a reviewed baseline; only a newly
reachable path fails CI. Runtime owner tracing covers dynamic protocol dispatch
that this stdlib-only graph cannot resolve.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


DEFAULT_BASELINE = Path("scripts/ci/network-reachability-baseline.json")
IPC_MODULE = "hyprial.daemon.impl.ipc.router"
IPC_ROOT_SUFFIX = "._IpcDispatchMixin.handle"

# Exact effect ports from network-daemon-decoupling §8.2.  Calls through an
# injected receiver resolve conservatively to every matching method name.
BOUNDARIES = frozenset(
    {
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.put",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.get",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.query",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.reconfigure_connect",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.subscribe",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.declare_queryable",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.declare_query_handler",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.get_liveliness",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.declare_liveliness",
        "hyprial.daemon.impl.transport.zenoh.transport.ZenohTransport.observe_liveliness",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority._call",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.put",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.get",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.query",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.reconfigure_connect",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.subscribe",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.declare_queryable",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.declare_query_handler",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.get_liveliness",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.declare_liveliness",
        "hyprial.daemon.impl.transport.session_actor.TransportSessionAuthority.observe_liveliness",
        "hyprial.daemon.impl.forwarding.controller.ForwardingControllerAuthority._call",
        "hyprial.daemon.impl.forwarding.controller.ForwardingSidecarController._send",
        "hyprial.daemon.impl.forwarding.controller.ForwardingSidecarController._request",
        "hyprial.daemon.impl.dispatch.remote.wire.RemoteWire.call",
        "hyprial.daemon.impl.org.network.casdoor.client.HttpCasdoorGroups._bearer",
        "hyprial.daemon.impl.org.network.casdoor.client.HttpCasdoorGroups._call",
        "hyprial.daemon.impl.org.network.casdoor.client.HttpCasdoorGroups.member_state",
        "hyprial.daemon.impl.org.network.casdoor.client.HttpCasdoorGroups.require_member",
        "hyprial.daemon.impl.org.network.casdoor.client.HttpCasdoorGroups.add_member",
        "hyprial.daemon.impl.org.network.casdoor.client.HttpCasdoorGroups.remove_member",
        "hyprial.daemon.impl.adapters.lark.outbound.gateway.GatewayIoAuthority._call",
        "hyprial.daemon.impl.adapters.lark.outbound.gateway.GatewayIoAuthority.send_chat",
        "hyprial.daemon.impl.adapters.lark.outbound.gateway.GatewayIoAuthority.send_chat_file",
        "hyprial.daemon.impl.adapters.lark.outbound.gateway.GatewayIoAuthority.send_chat_image",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.bot_info",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.list_chats_page",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.list_chat_members_page",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.add_reaction",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.send_chat",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.send_chat_file",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.send_chat_image",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.clear_reaction",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.reply",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.get_message",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.get_inbound_message",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.download_message_resource",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk.LarkSdkGateway.list_chat_messages",
    }
)


@dataclass(frozen=True, slots=True)
class Function:
    name: str
    module: str
    path: str
    line: int
    calls: tuple[ast.expr, ...]


class _Calls(ast.NodeVisitor):
    def __init__(self) -> None:
        self.values: list[ast.expr] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.values.append(node.func)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


class _Definitions(ast.NodeVisitor):
    def __init__(self, module: str, path: str) -> None:
        self.module = module
        self.path = path
        self.scope: list[str] = []
        self.functions: list[Function] = []
        self.imports: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports[alias.asname or alias.name.split(".")[0]] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        module = _absolute_import(self.module, node.module, node.level)
        for alias in node.names:
            if alias.name != "*":
                self.imports[alias.asname or alias.name] = f"{module}.{alias.name}"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified = ".".join((self.module, *self.scope, node.name))
        calls = _Calls()
        for child in node.body:
            calls.visit(child)
        self.functions.append(
            Function(qualified, self.module, self.path, node.lineno, tuple(calls.values))
        )
        self.scope.append(node.name)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(child)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)


def _absolute_import(current: str, imported: str, level: int) -> str:
    if level == 0:
        return imported
    parts = current.split(".")[:-level]
    return ".".join((*parts, imported)) if imported else ".".join(parts)


def _module(root: Path, path: Path) -> str:
    relative = path.relative_to(root / "src")
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _attribute(value: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _load(root: Path) -> tuple[dict[str, Function], dict[str, dict[str, str]], list[dict[str, Any]]]:
    functions: dict[str, Function] = {}
    imports: dict[str, dict[str, str]] = {}
    errors: list[dict[str, Any]] = []
    source = root / "src" / "hyprial" / "daemon"
    if not source.is_dir():
        return functions, imports, [{"kind": "missing-source", "path": str(source)}]
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        module = _module(root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as error:
            errors.append({"kind": "parse-error", "path": relative, "detail": str(error)})
            continue
        collector = _Definitions(module, relative)
        collector.visit(tree)
        imports[module] = collector.imports
        for function in collector.functions:
            functions[function.name] = function
    return functions, imports, errors


def _resolve_calls(
    function: Function,
    *,
    functions: dict[str, Function],
    imports: dict[str, dict[str, str]],
    by_short_name: dict[str, set[str]],
) -> set[str]:
    edges: set[str] = set()
    aliases = imports.get(function.module, {})
    boundary_names = {name.rsplit(".", 1)[-1] for name in BOUNDARIES}
    ambiguous_names = {"_call", "_request", "call", "get", "put", "query"}
    network_receiver_names = {
        "casdoor", "forwarding", "gateway", "lark", "mesh", "remote",
        "route", "session", "sidecar", "transport", "wire", "zenoh",
    }
    unambiguous_receiver_names = {
        "casdoor", "forwarding", "gateway", "mesh", "sidecar", "transport", "wire",
    }
    for call in function.calls:
        if isinstance(call, ast.Name):
            imported = aliases.get(call.id)
            local = f"{function.module}.{call.id}"
            if imported in functions:
                edges.add(imported)
            if local in functions:
                edges.add(local)
            continue
        parts = _attribute(call)
        if parts is None:
            continue
        imported = aliases.get(parts[0])
        if imported is not None:
            exact = ".".join((imported, *parts[1:]))
            if exact in functions:
                edges.add(exact)
        candidates = by_short_name.get(parts[-1], set())
        if len(parts) == 2 and parts[0] in {"self", "cls"}:
            same_owner = f"{function.name.rsplit('.', 1)[0]}.{parts[-1]}"
            if same_owner in functions:
                edges.add(same_owner)
            elif parts[-1] not in ambiguous_names:
                edges.update(candidates)
            continue
        receiver_names = {
            component.lower().strip("_") for component in parts[:-1]
        }
        owner_receiver = parts[0] in {"self", "cls"} and (
            receiver_names & network_receiver_names
        )
        specific_receiver = receiver_names & unambiguous_receiver_names
        typed = _typed_receiver(parts, candidates)
        if typed is not None:
            edges.add(typed)
        if parts[-1] in boundary_names and (owner_receiver or specific_receiver):
            edges.update(candidates & BOUNDARIES)
        elif parts[-1] not in ambiguous_names and len(candidates) <= 8:
            edges.update(candidates)
    return edges


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _typed_receiver(parts: tuple[str, ...], candidates: set[str]) -> str | None:
    """Resolve ``self._remote_workflow.admit`` to ``RemoteWorkflow.admit``.

    An owned attribute named after its class is resolved exactly, whatever
    the number of same-named methods elsewhere: the short-name fallback
    below drops any name with more than eight definitions, so a merge that
    only adds unrelated ``admit`` methods would otherwise erase a reviewed
    IPC -> network path (release/v0.5.5.4 x intg/cell-model added three).
    """

    if len(parts) < 3 or parts[0] not in {"self", "cls"}:
        return None
    attribute = parts[-2].strip("_").lower()
    typed = {
        candidate
        for candidate in candidates
        if _snake(candidate.rsplit(".", 2)[-2]) == attribute
    }
    return next(iter(typed)) if len(typed) == 1 else None


def _fingerprint(root: str, caller: str, boundary: str) -> str:
    return hashlib.sha256(f"{root}\0{caller}\0{boundary}".encode()).hexdigest()


def _load_baseline(path: Path) -> frozenset[str]:
    if not path.is_file():
        return frozenset()
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("violations", []) if isinstance(value, dict) else []
    return frozenset(
        row["fingerprint"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("fingerprint"), str)
    )


def scan(
    root: Path,
    *,
    baseline: frozenset[str] | None = None,
    baseline_path: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    functions, imports, errors = _load(root)
    by_short_name: dict[str, set[str]] = defaultdict(set)
    for name in functions:
        by_short_name[name.rsplit(".", 1)[-1]].add(name)
    graph = {
        name: _resolve_calls(
            function,
            functions=functions,
            imports=imports,
            by_short_name=by_short_name,
        )
        for name, function in functions.items()
    }
    roots = sorted(
        name
        for name in functions
        if name.startswith(IPC_MODULE + ".") and name.endswith(IPC_ROOT_SUFFIX)
    )
    configured_boundaries = BOUNDARIES & functions.keys()
    violations: list[dict[str, Any]] = []
    for root_name in roots:
        pending: deque[tuple[str, tuple[str, ...]]] = deque([(root_name, (root_name,))])
        visited = {root_name}
        while pending:
            current, path = pending.popleft()
            for called in sorted(graph.get(current, ())):
                if called in configured_boundaries:
                    function = functions[called]
                    violations.append(
                        {
                            "kind": "network-reachable-from-ipc",
                            "fingerprint": _fingerprint(root_name, current, called),
                            "root": root_name,
                            "caller": current,
                            "boundary": called,
                            "boundarySite": f"{function.path}:{function.line}",
                            "path": [*path, called],
                        }
                    )
                elif called not in visited:
                    visited.add(called)
                    pending.append((called, (*path, called)))
    violations.sort(key=lambda row: (row["root"], row["boundary"], row["caller"]))
    if baseline is None:
        selected_path = baseline_path or root / DEFAULT_BASELINE
        baseline = _load_baseline(selected_path)
    known = [row for row in violations if row["fingerprint"] in baseline]
    new = [row for row in violations if row["fingerprint"] not in baseline]
    stale = sorted(baseline - {row["fingerprint"] for row in violations})
    stale_failures = [
        {"kind": "stale-baseline", "fingerprint": fingerprint}
        for fingerprint in stale
    ]
    failures = [*errors, *new, *stale_failures]
    return {
        "status": "PASS" if not failures else "FAIL",
        "filesScanned": len({function.path for function in functions.values()}),
        "functionsScanned": len(functions),
        "ipcRoots": roots,
        "configuredBoundaryCount": len(configured_boundaries),
        "knownViolationCount": len(known),
        "newViolationCount": len(new),
        "staleBaselineCount": len(stale),
        "staleBaseline": stale,
        "violations": failures,
        "knownViolations": known,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    baseline_path = args.baseline
    if baseline_path is not None and not baseline_path.is_absolute():
        baseline_path = args.root / baseline_path
    report = scan(args.root, baseline_path=baseline_path)
    output = {key: value for key, value in report.items() if key != "knownViolations"}
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
