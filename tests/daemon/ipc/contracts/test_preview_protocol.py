"""A-03 generic preview→execute binding: caller/method/digest/expiry/single claim."""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.kernel import (
    DaemonRequestError,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    PreviewSpec,
    ipc_errors,
    validate_method_specs,
)
from hyprial.daemon.impl.request_catalog.preview import PreviewRegistry, params_digest

MOVE_ID = "move-7f3a9c"


def _specs() -> tuple[MethodSpec, MethodSpec]:
    common_errors = (MethodError(ipc_errors.INVALID_ARGUMENT),)
    preview = MethodSpec(
        name="probe.remove.preview",
        owner="daemon.probe",
        params_schema={
            "type": "object",
            "properties": {"target": {"type": "string", "minLength": 1}},
            "required": ["target"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {
                "operationId": {"type": "string", "minLength": 1},
                "expiresAt": {"type": "string", "minLength": 1},
                "summary": {"type": "string", "minLength": 1},
            },
            "required": ["operationId", "expiresAt", "summary"],
            "additionalProperties": False,
        },
        errors=(
            MethodError(ipc_errors.PREVIEW_UNKNOWN),
            MethodError(ipc_errors.PREVIEW_EXPIRED),
            MethodError(ipc_errors.PREVIEW_STALE),
            MethodError(ipc_errors.OPERATION_IN_PROGRESS),
        )
        + common_errors,
        confirmation="preview",
        preview=PreviewSpec(ttl_seconds=300),
        audience=("local-operator",),
        idempotency=IdempotencySpec(mode="safe"),
    )
    execute = MethodSpec(
        name="probe.remove",
        owner="daemon.probe",
        params_schema={
            "type": "object",
            "properties": {"operationId": {"type": "string", "minLength": 1}},
            "required": ["operationId"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        errors=common_errors,
        mutates=True,
        retry="never",
        idempotency=IdempotencySpec(mode="never"),
        confirmation="execute",
        preview_of="probe.remove.preview",
        audience=("local-operator",),
    )
    validate_method_specs((preview, execute))
    return preview, execute


def _registry(
    now: list[int] | None = None, **kwargs: Any
) -> tuple[PreviewRegistry, Any]:
    clock = now if now is not None else [1_000]
    return (
        PreviewRegistry(epoch="epoch-1", now_ms=lambda: clock[0], **kwargs),
        clock,
    )


def test_preview_issue_binds_caller_method_digest_and_expiry() -> None:
    preview, _execute = _specs()
    registry, _clock = _registry()
    params = {"target": "agent:o:n:alice"}
    record = registry.issue(
        preview, params, caller="user:o", summary="remove alice's reference"
    )

    assert record.operation_id
    assert record.operation_id != MOVE_ID
    assert record.method == preview.name
    assert record.caller == "user:o"
    assert record.params_digest == params_digest(params)
    assert record.expires_at_ms == record.issued_at_ms + 300_000
    assert record.claimed is False


def test_preview_id_is_not_a_business_move_id() -> None:
    preview, execute = _specs()
    registry, _clock = _registry(id_factory=lambda: "verified-operation-id")
    record = registry.issue(preview, {"target": MOVE_ID}, caller="user:o", summary="s")

    # A business moveId is not an operation id and cannot claim the preview.
    with pytest.raises(DaemonRequestError) as caught:
        registry.claim(execute, {"operationId": MOVE_ID}, caller="user:o")
    assert caught.value.code == ipc_errors.PREVIEW_UNKNOWN

    claimed = registry.claim(
        execute, {"operationId": record.operation_id}, caller="user:o"
    )
    assert claimed.claimed is True


def test_other_caller_or_method_is_refused_without_target_details() -> None:
    preview, execute = _specs()
    registry, _clock = _registry()
    record = registry.issue(preview, {"target": "agent:o:n:alice"}, caller="user:o", summary="s")

    with pytest.raises(DaemonRequestError) as other_caller:
        registry.claim(execute, {"operationId": record.operation_id}, caller="user:other")
    assert other_caller.value.code == ipc_errors.PREVIEW_STALE

    with pytest.raises(DaemonRequestError) as other_method:
        registry.claim(
            MethodSpec(
                name="probe.other",
                owner="daemon.probe",
                params_schema=execute.params_schema,
                result_schema=execute.result_schema,
                errors=execute.errors,
                mutates=True,
                retry="never",
                idempotency=IdempotencySpec(mode="never"),
                confirmation="execute",
                preview_of="probe.other.preview",
                audience=("local-operator",),
            ),
            {"operationId": record.operation_id},
            caller="user:o",
        )
    assert other_method.value.code == ipc_errors.PREVIEW_STALE


def test_expiry_digest_and_double_claim_have_distinct_codes() -> None:
    preview, execute = _specs()
    registry, clock = _registry()
    record = registry.issue(preview, {"target": "agent:o:n:alice"}, caller="user:o", summary="s")

    with pytest.raises(DaemonRequestError) as changed:
        registry.claim(
            execute,
            {"operationId": record.operation_id},
            caller="user:o",
            expected_digest=params_digest({"target": "someone-else"}),
        )
    assert changed.value.code == ipc_errors.PREVIEW_STALE

    claimed = registry.claim(execute, {"operationId": record.operation_id}, caller="user:o")
    assert claimed.claimed is True
    with pytest.raises(DaemonRequestError) as second:
        registry.claim(execute, {"operationId": record.operation_id}, caller="user:o")
    assert second.value.code == ipc_errors.OPERATION_IN_PROGRESS

    other = registry.issue(preview, {"target": "agent:o:n:bob"}, caller="user:o", summary="s")
    clock[0] += 300_001
    with pytest.raises(DaemonRequestError) as expired:
        registry.claim(execute, {"operationId": other.operation_id}, caller="user:o")
    assert expired.value.code == ipc_errors.PREVIEW_EXPIRED


def test_missing_operation_id_and_other_epoch_are_refused() -> None:
    preview, execute = _specs()
    registry, _clock = _registry()
    record = registry.issue(preview, {"target": "agent:o:n:alice"}, caller="user:o", summary="s")

    with pytest.raises(DaemonRequestError) as missing:
        registry.claim(execute, {"operationId": ""}, caller="user:o")
    assert missing.value.code == ipc_errors.INVALID_ARGUMENT

    registry.epoch = "epoch-2"
    with pytest.raises(DaemonRequestError) as stale:
        registry.claim(execute, {"operationId": record.operation_id}, caller="user:o")
    assert stale.value.code == ipc_errors.PREVIEW_STALE


def test_session_binding_and_completed_result_replay() -> None:
    preview, execute = _specs()
    registry, _clock = _registry()
    record = registry.issue(
        preview,
        {"target": "agent:o:n:alice"},
        caller="user:o",
        session="session-1",
        summary="s",
    )

    with pytest.raises(DaemonRequestError) as other_session:
        registry.claim(
            execute,
            {"operationId": record.operation_id},
            caller="user:o",
            session="session-2",
        )
    assert other_session.value.code == ipc_errors.PREVIEW_STALE

    claimed = registry.claim(
        execute,
        {"operationId": record.operation_id},
        caller="user:o",
        session="session-1",
    )
    assert claimed.claimed is True
    with pytest.raises(DaemonRequestError) as in_progress:
        registry.claim(
            execute,
            {"operationId": record.operation_id},
            caller="user:o",
            session="session-1",
        )
    assert in_progress.value.code == ipc_errors.OPERATION_IN_PROGRESS

    result = {"ok": True, "removed": True}
    registry.complete(record.operation_id, result)
    replay = registry.claim(
        execute,
        {"operationId": record.operation_id},
        caller="user:o",
        session="session-1",
    )
    assert replay.completed is True
    assert replay.result == result


def test_replacement_params_resource_version_and_concurrent_claims() -> None:
    preview, execute = _specs()
    registry, _clock = _registry()
    record = registry.issue(
        preview,
        {"target": "agent:o:n:alice"},
        caller="user:o",
        resource_version="v1",
        summary="s",
    )

    with pytest.raises(DaemonRequestError) as replacement:
        registry.claim(
            execute,
            {"operationId": record.operation_id, "target": "agent:o:n:mallory"},
            caller="user:o",
        )
    assert replacement.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as version:
        registry.claim(
            execute,
            {"operationId": record.operation_id},
            caller="user:o",
            resource_version="v2",
        )
    assert version.value.code == ipc_errors.PREVIEW_STALE

    from concurrent.futures import ThreadPoolExecutor

    def attempt() -> str:
        try:
            registry.claim(
                execute,
                {"operationId": record.operation_id},
                caller="user:o",
                resource_version="v1",
            )
        except DaemonRequestError as error:
            return error.code
        return "claimed"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(8)))
    assert outcomes.count("claimed") == 1, outcomes
    assert outcomes.count(ipc_errors.OPERATION_IN_PROGRESS) == 7, outcomes


def test_concurrent_claim_is_atomic_even_with_a_barrier_clock() -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    class Clock:
        def __init__(self) -> None:
            self.barrier: threading.Barrier | None = None

        def __call__(self) -> int:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            return 1_000

    preview, execute = _specs()
    clock = Clock()
    registry = PreviewRegistry(epoch="epoch-1", now_ms=clock)
    record = registry.issue(preview, {"target": "agent:o:n:alice"}, caller="user:o", summary="s")
    clock.barrier = threading.Barrier(2)

    def attempt() -> str:
        try:
            registry.claim(execute, {"operationId": record.operation_id}, caller="user:o")
        except DaemonRequestError as error:
            return error.code
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))
    assert outcomes.count("claimed") == 1, outcomes
    assert outcomes.count(ipc_errors.OPERATION_IN_PROGRESS) == 1, outcomes


def test_saved_params_terminal_result_and_authorization_callback() -> None:
    preview, execute = _specs()
    registry, _clock = _registry()
    original = {"target": "agent:o:n:alice"}
    record = registry.issue(preview, original, caller="user:o", summary="s")
    original["target"] = "agent:o:n:mallory"

    claimed = registry.claim(
        execute, {"operationId": record.operation_id}, caller="user:o"
    )
    first = registry.complete(claimed.operation_id, {"value": 1})
    assert first.result == {"value": 1}
    replay = registry.claim(
        execute, {"operationId": record.operation_id}, caller="user:o"
    )
    assert replay.params == {"target": "agent:o:n:alice"}
    replay.params["target"] = "mutated"
    assert registry.claim(
        execute, {"operationId": record.operation_id}, caller="user:o"
    ).params == {"target": "agent:o:n:alice"}

    returned = replay.result
    assert returned == {"value": 1}
    returned["value"] = 999
    assert registry.claim(
        execute, {"operationId": record.operation_id}, caller="user:o"
    ).result == {"value": 1}
    with pytest.raises(DaemonRequestError) as overwrite:
        registry.complete(record.operation_id, {"value": 2})
    assert overwrite.value.code == ipc_errors.PREVIEW_STALE

    other = registry.issue(preview, {"target": "agent:o:n:bob"}, caller="user:o", summary="s")

    def deny(_record: Any) -> None:
        raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "denied")

    with pytest.raises(DaemonRequestError) as denied:
        registry.claim(
            execute,
            {"operationId": other.operation_id},
            caller="user:o",
            authorize=deny,
        )
    assert denied.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    with pytest.raises(ValueError, match="execute method"):
        registry.claim(preview, {"operationId": other.operation_id}, caller="user:o")
