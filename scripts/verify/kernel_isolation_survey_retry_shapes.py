"""Control-flow shape analysis for kernel isolation retry evidence."""

from __future__ import annotations

import ast
from collections.abc import Iterable

DELAY_CALLS = {"sleep", "wait", "schedule", "timer"}
OBSERVATION_CALL_WORDS = {
    "await",
    "check",
    "exists",
    "get",
    "is_alive",
    "is_set",
    "monotonic",
    "observe",
    "poll",
    "qsize",
    "read",
    "receive",
    "snapshot",
    "status",
    "time",
    "wait",
}
REATTEMPT_CALL_WORDS = {
    "admit",
    "begin",
    "connect",
    "deliver",
    "execute",
    "flush",
    "load",
    "lock",
    "open",
    "publish",
    "reconcile",
    "recover",
    "request",
    "send",
    "start",
    "submit",
    "sync",
    "tell",
    "tick",
    "write",
}
VALIDATION_EXCEPTIONS = {
    "AttributeError",
    "IndexError",
    "JSONDecodeError",
    "KeyError",
    "SyntaxError",
    "TypeError",
    "UnicodeDecodeError",
    "UnicodeError",
    "ValueError",
}
UTILITY_CALLS = {
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "float",
    "getattr",
    "hasattr",
    "int",
    "isinstance",
    "len",
    "list",
    "max",
    "min",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = _dotted(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    return None


def walk_local(node: ast.AST) -> Iterable[ast.AST]:
    """Walk one lexical scope without absorbing nested definitions."""

    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        children = list(ast.iter_child_nodes(current))
        for child in reversed(children):
            if child is not node and isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
            ):
                continue
            stack.append(child)


def _call_words(node: ast.AST) -> set[str]:
    return {
        (_dotted(item.func) or "").rsplit(".", 1)[-1].lower()
        for item in walk_local(node)
        if isinstance(item, ast.Call)
    }


def _has_word(names: set[str], words: set[str]) -> bool:
    return any(
        token in words
        for name in names
        for token in name.strip("_").split("_")
    )


def _is_operation_call(name: str) -> bool:
    return name not in {"run_sync", "to_thread"} and _has_word(
        {name}, REATTEMPT_CALL_WORDS
    )


def _try_call_words(node: ast.AST) -> set[str]:
    """Expose the callable passed to thread-wrapper operations."""

    words: set[str] = set()
    for item in walk_local(node):
        if not isinstance(item, ast.Call):
            continue
        name = (_dotted(item.func) or "").rsplit(".", 1)[-1].lower()
        if name in {"run_sync", "to_thread"} and item.args:
            wrapped = _dotted(item.args[0])
            if wrapped:
                words.add(wrapped.rsplit(".", 1)[-1].lower())
                continue
        words.add(name)
    return words


def _consumes_queue(loop: ast.AST) -> bool:
    for item in walk_local(loop):
        if not isinstance(item, ast.Call):
            continue
        name = (_dotted(item.func) or "").rsplit(".", 1)[-1].lower()
        if name in {
            "get_nowait",
            "popleft",
            "pop",
            "popitem",
            "next",
            "partition",
            "readline",
            "recv",
        }:
            return True
        if name == "get" and not (
            item.args
            and isinstance(item.args[0], ast.Constant)
            and isinstance(item.args[0].value, str)
        ):
            return True
    return False


def _target_names(target: ast.AST) -> set[str]:
    return {
        item.id
        for item in ast.walk(target)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
    }


def _assigned_names(statements: list[ast.stmt]) -> set[str]:
    return {
        item.id
        for statement in statements
        for item in ast.walk(statement)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
    }


def _exception_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    return {
        item.id
        for item in ast.walk(handler.type)
        if isinstance(item, ast.Name)
    } | {
        item.attr
        for item in ast.walk(handler.type)
        if isinstance(item, ast.Attribute)
    }


def _while_alternative_names(loop: ast.While) -> set[str]:
    """Find names that select or hold one item from a while-loop collection."""

    names: set[str] = set()
    for item in walk_local(loop):
        if not isinstance(item, ast.Subscript):
            continue
        names.update(
            child.id for child in ast.walk(item) if isinstance(child, ast.Name)
        )
    for statement in loop.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value = statement.value
        if not isinstance(value, ast.Subscript):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        names.update(name for target in targets for name in _target_names(target))
    return names


def _failover_calls(
    loop: ast.For | ast.AsyncFor | ast.While, outcome_names: set[str]
) -> set[str]:
    """Find operations retried against successive loop alternatives."""

    targets = (
        _target_names(loop.target)
        if isinstance(loop, (ast.For, ast.AsyncFor))
        else _while_alternative_names(loop)
    )
    if not targets:
        return set()
    operations: set[str] = set()
    for item in walk_local(loop):
        if not isinstance(item, ast.Try) or not item.handlers:
            continue
        advances = any(
            handler.body
            and (
                isinstance(handler.body[-1], ast.Continue)
                or bool(_assigned_names(handler.body) & outcome_names)
            )
            and not (
                (caught := _exception_names(handler))
                and caught <= VALIDATION_EXCEPTIONS
            )
            for handler in item.handlers
        )
        if not advances:
            continue
        for call in (
            child
            for statement in item.body
            for child in ast.walk(statement)
            if isinstance(child, ast.Call)
        ):
            used = {
                child.id for child in ast.walk(call) if isinstance(child, ast.Name)
            }
            name = (_dotted(call.func) or "").rsplit(".", 1)[-1].lower()
            if targets & used and name not in UTILITY_CALLS:
                operations.add(name)
    return operations


def retry_shape(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, str]:
    """Classify loops as retry, observation, cadence, or lexical-only."""

    retry_ops: set[str] = set()
    failover_retry_ops: set[str] = set()
    observed: set[str] = set()
    cadences = 0
    outcome_names = {
        item.id
        for terminal in walk_local(node)
        if isinstance(terminal, (ast.Return, ast.Raise))
        for item in ast.walk(terminal)
        if isinstance(item, ast.Name)
    }
    loops = [
        item
        for item in walk_local(node)
        if isinstance(item, (ast.For, ast.AsyncFor, ast.While))
    ]
    for loop in loops:
        names = _call_words(loop)
        delayed = _has_word(names, DELAY_CALLS) or "capped_exponential" in names
        identifiers = {
            item.id.lower() for item in walk_local(loop) if isinstance(item, ast.Name)
        } | {
            item.attr.lower()
            for item in walk_local(loop)
            if isinstance(item, ast.Attribute)
        }
        handlers = [
            item for item in walk_local(loop) if isinstance(item, ast.ExceptHandler)
        ]
        nonterminal_handler = any(
            not handler.body
            or not isinstance(handler.body[-1], (ast.Raise, ast.Return, ast.Break))
            for handler in handlers
        )
        failed = nonterminal_handler or any(
            any(word in identifier for word in ("overload", "failure", "failed"))
            for identifier in identifiers
        )
        operations = {name for name in names if _is_operation_call(name)}
        observations = {
            name for name in names if _has_word({name}, OBSERVATION_CALL_WORDS)
        }
        queue_consumer = _consumes_queue(loop)
        generic_try_calls: set[str] = set()
        for item in walk_local(loop):
            if not isinstance(item, ast.Try) or not item.handlers:
                continue
            if all(
                handler.body
                and isinstance(handler.body[-1], (ast.Raise, ast.Return, ast.Break))
                for handler in item.handlers
            ):
                continue
            generic_try_calls.update(
                name
                for name in _try_call_words(
                    ast.Module(body=item.body, type_ignores=[])
                )
                if not _has_word({name}, DELAY_CALLS)
                and not name.startswith(("log", "_log"))
            )
        bounded_attempt_loop = (
            isinstance(loop, (ast.For, ast.AsyncFor))
            and isinstance(loop.iter, ast.Call)
            and (_dotted(loop.iter.func) or "").rsplit(".", 1)[-1] == "range"
        )
        failover_ops = (
            _failover_calls(loop, outcome_names)
            if not bounded_attempt_loop
            else set()
        )
        retry_trigger = bool(failover_ops) or delayed or (
            failed and (isinstance(loop, ast.While) or bounded_attempt_loop)
        )
        if not queue_consumer and (
            operations or generic_try_calls or failover_ops
        ) and retry_trigger:
            retry_ops.update(failover_ops or operations or generic_try_calls)
            failover_retry_ops.update(failover_ops)
        elif delayed and names and not operations:
            observed.update(observations or names)
        elif delayed:
            cadences += 1
    if retry_ops:
        shape = "retry-failover" if failover_retry_ops else "retry"
        return shape, ",".join(sorted(retry_ops))
    if observed and not cadences:
        return "observation", ",".join(sorted(observed))
    if cadences:
        return "cadence", "-"
    return "lexical", "-"
