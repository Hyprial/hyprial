from __future__ import annotations

import ast
from pathlib import Path


def scan_workflow_inbox_adapter(path: Path) -> list[str]:
    return scan_source(path.read_text(encoding="utf-8"), filename=str(path))


def scan_delivery_receipt_gate(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    custody = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DeliveryCustody"
        ),
        None,
    )
    if custody is None:
        return ["DeliveryCustody is missing"]
    methods = {
        node.name: node
        for node in custody.body
        if isinstance(node, ast.FunctionDef)
    }
    stage = methods.get("_stage_submit")
    apply = methods.get("_apply_outcome")
    if stage is None or apply is None:
        return ["delivery receipt methods are missing"]
    stage_calls = [
        (_call_name(node), node.lineno)
        for node in ast.walk(stage)
        if isinstance(node, ast.Call)
    ]
    read_lines = [line for name, line in stage_calls if name == "_read_submission_receipt"]
    insert_lines = [line for name, line in stage_calls if name == "_insert_outbox"]
    errors: list[str] = []
    if not read_lines or not insert_lines or min(read_lines) >= min(insert_lines):
        errors.append("durable submission receipt must be read before outbox insertion")
    if not any(
        _call_name(node) == "_persist_submission_receipt_locked"
        for node in ast.walk(apply)
        if isinstance(node, ast.Call)
    ):
        errors.append("delivery outcome must persist its durable submission receipt")
    if "actor_submission_receipts" not in path.read_text(encoding="utf-8"):
        errors.append("D22 actor_submission_receipts table is missing")
    return errors


def scan_source(source: str, *, filename: str = "<workflow-inbox-io>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    errors: list[str] = []
    adapter = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "InboxDeliveryIoAdapter"
        ),
        None,
    )
    if adapter is None:
        return ["InboxDeliveryIoAdapter is missing"]
    deliver = next(
        (
            node
            for node in adapter.body
            if isinstance(node, ast.FunctionDef) and node.name == "deliver"
        ),
        None,
    )
    if deliver is None:
        return ["InboxDeliveryIoAdapter.deliver is missing"]
    calls = [node for node in ast.walk(deliver) if isinstance(node, ast.Call)]
    if any(_call_name(call) == "uuid4" for call in calls):
        errors.append("delivery message id must not use uuid4")
    if not any(
        _call_name(call) == "message_id"
        and any(isinstance(arg, ast.Name) and arg.id == "effect_id" for arg in call.args)
        for call in calls
    ):
        errors.append("delivery message id must derive from effect_id")
    submits = [call for call in calls if _call_name(call) == "SubmitMessageCommand"]
    if not any(
        any(
            keyword.arg == "correlation_id"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "effect_id"
            for keyword in call.keywords
        )
        for call in submits
    ):
        errors.append("SubmitMessageCommand correlation_id must be effect_id")
    if not any(
        isinstance(node, ast.JoinedStr)
        and any(
            isinstance(value, ast.FormattedValue)
            and isinstance(value.value, ast.Name)
            and value.value.id == "effect_id"
            for value in node.values
        )
        and any(
            isinstance(value, ast.Constant) and value.value == "workflow:"
            for value in node.values
        )
        for node in ast.walk(deliver)
    ):
        errors.append("delivery idempotency_key must include workflow:<effect_id>")
    if any(
        isinstance(node, ast.Delete)
        and any(isinstance(target, ast.Name) and target.id == "effect_id" for target in node.targets)
        for node in ast.walk(deliver)
    ):
        errors.append("delivery adapter must not delete effect_id")
    return errors


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None
