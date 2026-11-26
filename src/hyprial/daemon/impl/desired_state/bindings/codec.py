"""Closed, immutable JSON projection of binding authority."""

from .models import Binding, Cleanup, Operation, invalid

ROOTS = ("modelBindings", "modelBindingOperations", "modelAgentLifecycle")
ROOT_FIELDS = {
    "schemaVersion",
    "asMailbox",
    "providers",
    "adapters",
    "interactiveSessions",
    "pendingSessionAgentEffects",
    "lifecycleResources",
    "lifecycleReceipts",
    "deprecatedSharedChannels",
    "zenoh",
    "serviceConnections",
    "serviceRegistry",
    *ROOTS,
}


def parse(document):
    version = document.get("schemaVersion", 3)
    if version < 4:
        if any(key in document for key in ROOTS):
            invalid()
        return (), (), ()
    if document.keys() - ROOT_FIELDS or any(key not in document for key in ROOTS):
        invalid()
    result = []
    for key, model in zip(ROOTS, (Binding, Operation, Cleanup), strict=True):
        rows = document[key]
        if not isinstance(rows, list):
            invalid()
        items = tuple(model.from_json(row) for row in rows)

        def identity(item):
            if isinstance(item, Cleanup):
                return (item.agent_id,)
            return (
                (item.agent_id, item.member_id, item.operation_id)
                if isinstance(item, Operation)
                else (item.agent_id, item.member_id)
            )

        if len({identity(item) for item in items}) != len(items):
            invalid()
        result.append(tuple(sorted(items, key=identity)))
    bindings, operations, _ = result
    prepared = [
        (item.agent_id, item.member_id)
        for item in operations
        if item.phase == "prepared"
    ]
    if len(set(prepared)) != len(prepared) or len(
        {item.binding_ref for item in bindings}
    ) != len(bindings):
        invalid()
    by_key = {(item.agent_id, item.member_id): item for item in bindings}
    by_op = {
        (item.agent_id, item.member_id, item.operation_id): item for item in operations
    }
    for item in bindings:
        op = by_op.get((item.agent_id, item.member_id, item.operation_id))
        if (
            op is None
            or op.phase != "finalized"
            or op.result_revision != item.binding_revision
            or op.binding_ref != item.binding_ref
            or op.candidate != item.configuration
        ):
            invalid()
    for item in operations:
        current = by_key.get((item.agent_id, item.member_id))
        if item.phase == "prepared" and item.expected_revision != (
            current.binding_revision if current else 0
        ):
            invalid()
    return tuple(result)


def project(bindings=(), operations=(), lifecycle=()):
    return {
        key: [item.to_json() for item in items]
        for key, items in zip(ROOTS, (bindings, operations, lifecycle), strict=True)
    }
