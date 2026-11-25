"""Process-local transport orchestration; caller owns home lock and journal."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ..client import DeviceTransportClient, TransportClientError
from ..transport_credential import (
    commit_transport_credential,
    read_transport_credential,
    stage_transport_credential,
    transport_credential_status,
    validate_enrollment_response,
)
from ..transport_storage import _read_safe_optional_file_beneath
from .staging import discard
from .guards import (
    association,
    fail,
    fresh,
    public_gate,
    require_association,
    require_ready_tls,
)


class TailnetCredentialOperation:
    """Opaque identity token. Only its creating workflow owns operation state."""

    __slots__ = ()


@dataclass
class _Operation:
    kind: str
    operation_id: str
    record: object
    binding: object
    client: DeviceTransportClient
    staged: object = None
    target: object = None
    response: dict | None = None
    credential: object = None
    outcome: str = "NOT_SENT"
    phase: str = "PREPARED"
    error_code: str | None = None
    rejection: TransportClientError | None = None
    origin_proven: bool = False
    discard: object = None
    discard_receipt: dict | None = None


class TailnetCredentialWorkflow:
    """No durable recovery or cross-instance exclusivity is implied here."""

    def __init__(self, home, *, tailnet_id, context):
        self._home = Path(home)
        self._tailnet_id = tailnet_id
        self._context = context
        self._operations = {}

    def _idle(self, operation_id):
        for op in self._operations.values():
            pending = (
                op.phase not in ("COMMITTED", "DISCARDED")
                if op.kind != "retire"
                else op.outcome in ("NOT_SENT", "UNKNOWN")
            )
            if pending or op.operation_id == operation_id:
                fail("TAILNET_OPERATION_PENDING", "prepare", op.outcome)

    def _operation(self, handle, kind=None):
        if (
            type(handle) is not TailnetCredentialOperation
            or handle not in self._operations
        ):
            fail("TAILNET_OPERATION_INVALID", "handle")
        op = self._operations[handle]
        if kind is not None and op.kind != kind:
            fail("TAILNET_OPERATION_INVALID", "handle", op.outcome)
        return op

    def _current(self, op, phase, credential):
        fresh(self._home, self._tailnet_id, self._context, phase=phase, op=op)
        require_association(
            self._context,
            self._tailnet_id,
            op.record,
            op.binding,
            credential,
            phase=phase,
            outcome=op.outcome,
        )

    def _mutable(self, op):
        if op.discard is not None or op.phase == "DISCARDED":
            fail("TAILNET_DISCARD_STARTED", "handle", op.outcome)

    @public_gate
    def stage(self, operation_id, expected_credential_generation, *, kind="enroll"):
        self._idle(operation_id)
        record, binding = fresh(
            self._home, self._tailnet_id, self._context, phase="stage"
        )
        ready = read_transport_credential(self._home)
        if kind == "enroll":
            current = association(
                self._context, self._tailnet_id, record, binding, phase="stage"
            )
            if (
                current is not None
                or ready is not None
                or expected_credential_generation != 0
            ):
                fail("TAILNET_ASSOCIATION_CONFLICT", "stage")
        elif (
            kind == "renew"
            and ready is not None
            and expected_credential_generation == ready.credential_generation
        ):
            require_association(
                self._context, self._tailnet_id, record, binding, ready, phase="stage"
            )
            require_ready_tls(self._home, binding, phase="stage")
        else:
            fail("TAILNET_OPERATION_INVALID", "stage")
        existing = _read_safe_optional_file_beneath(
            self._home / "state/transport/enrollment.json", root=self._home, mode=0o600
        )
        staged = stage_transport_credential(
            self._home,
            operation_id=operation_id,
            owner=record.owner,
            device_uid=record.device_uid,
            expected_credential_generation=expected_credential_generation,
        )
        op = _Operation(
            kind,
            operation_id,
            record,
            binding,
            DeviceTransportClient(binding.config),
            staged=staged,
            target=ready,
            origin_proven=existing is None,
        )
        handle = TailnetCredentialOperation()
        self._operations[handle] = op
        return handle

    @public_gate
    def discard_staged(self, handle, *, confirmed=False):
        op = self._operation(handle)
        return discard(self._home, op, confirmed=confirmed)

    @public_gate
    def enroll(self, handle):
        return self._send(handle, "enroll")

    @public_gate
    def renew(self, handle):
        return self._send(handle, "renew")

    def _send(self, handle, kind):
        op = self._operation(handle, kind)
        self._mutable(op)
        if op.response is not None:
            return deepcopy(op.response)
        if op.rejection is not None:
            raise op.rejection
        fresh(self._home, self._tailnet_id, self._context, phase="send", op=op)
        if op.outcome == "NOT_SENT":
            if op.kind == "enroll":
                current = association(
                    self._context, self._tailnet_id, op.record, op.binding, phase="send"
                )
                if (
                    current is not None
                    or read_transport_credential(self._home) is not None
                ):
                    fail("TAILNET_ASSOCIATION_CONFLICT", "send")
            else:
                require_association(
                    self._context,
                    self._tailnet_id,
                    op.record,
                    op.binding,
                    op.target,
                    phase="send",
                )
        previous_outcome = op.outcome
        # A delegate can propagate an interrupt after writing the request.
        # Only a definitive typed NOT_SENT can restore a previously unsent op.
        op.outcome = "UNKNOWN"
        try:
            if kind == "retire":
                response = op.client.retire(
                    operation_id=op.operation_id,
                    binding_id=op.target.binding_id,
                    expected_credential_generation=op.target.credential_generation,
                )
            else:
                response = (
                    op.client.enroll(op.staged)
                    if kind == "enroll"
                    else op.client.renew(op.staged, operation_id=op.operation_id)
                )
        except TransportClientError as error:
            op.error_code = error.code
            if previous_outcome == "UNKNOWN" and error.outcome == "NOT_SENT":
                from .ports import TailnetCredentialError

                raise TailnetCredentialError(
                    "TAILNET_ATTEMPT_FAILED",
                    phase="send",
                    remote_outcome="UNKNOWN",
                    attempt_outcome=error.outcome,
                    transport_code=error.code,
                    retryable=error.retryable,
                    http_status=error.http_status,
                    operation_id=op.operation_id,
                    reason_code=error.reason_code,
                    exception_class=error.exception_class,
                ) from None
            op.outcome = error.outcome
            if error.outcome == "REJECTED":
                op.rejection = error
            raise
        op.response = deepcopy(response)
        op.outcome = "SUCCESS"
        op.phase = "RECEIVED"
        return deepcopy(response)

    @public_gate
    def validate(self, handle):
        op = self._operation(handle)
        self._mutable(op)
        if op.phase == "COMMITTED":
            return
        if op.kind == "retire" or op.response is None:
            fail("TAILNET_OPERATION_INVALID", "validate", op.outcome)
        fresh(self._home, self._tailnet_id, self._context, phase="validate", op=op)
        credential = validate_enrollment_response(
            op.response,
            staged=op.staged,
            pinned_trust_bundle_pem=op.binding.config.trust_bundle_pem,
            effective_owner=op.record.owner,
        )
        self._current(op, "validate", credential)
        op.credential = credential
        op.phase = "VALIDATED"

    @public_gate
    def commit(self, handle):
        op = self._operation(handle)
        self._mutable(op)
        if op.phase == "COMMITTED":
            return
        if op.kind == "retire" or op.credential is None:
            fail("TAILNET_OPERATION_INVALID", "commit", op.outcome)
        self._current(op, "commit", op.credential)
        resumed = stage_transport_credential(
            self._home,
            operation_id=op.operation_id,
            owner=op.record.owner,
            device_uid=op.record.device_uid,
            expected_credential_generation=op.staged.expected_credential_generation,
        )
        if resumed.spki_sha256 != op.staged.spki_sha256:
            fail("TAILNET_ASSOCIATION_MISMATCH", "commit", op.outcome)
        op.staged = resumed
        commit_transport_credential(
            self._home,
            staged=op.staged,
            credential=op.credential,
            pinned_trust_bundle_pem=op.binding.config.trust_bundle_pem,
        )
        op.phase = "COMMITTED"

    @public_gate
    def prepare_retire(self, operation_id):
        try:
            valid_id = (
                isinstance(operation_id, str)
                and UUID(operation_id).version == 4
                and str(UUID(operation_id)) == operation_id
            )
        except ValueError:
            valid_id = False
        if not valid_id:
            fail("TAILNET_OPERATION_INVALID", "prepare")
        self._idle(operation_id)
        record, binding = fresh(
            self._home, self._tailnet_id, self._context, phase="retire"
        )
        target = read_transport_credential(self._home)
        if target is None:
            fail("TAILNET_OPERATION_INVALID", "retire")
        require_association(
            self._context, self._tailnet_id, record, binding, target, phase="retire"
        )
        require_ready_tls(self._home, binding, phase="retire")
        op = _Operation(
            "retire",
            operation_id,
            record,
            binding,
            DeviceTransportClient(binding.config),
            target=target,
        )
        handle = TailnetCredentialOperation()
        self._operations[handle] = op
        return handle

    @public_gate
    def retire_remote(self, handle):
        return self._send(handle, "retire")

    @public_gate
    def operation_status(self, handle):
        op = self._operation(handle)
        return {
            "operationId": op.operation_id,
            "kind": op.kind,
            "remoteOutcome": op.outcome,
            "phase": op.phase,
            "errorCode": op.error_code,
            "hasResponse": op.response is not None,
        }

    @public_gate
    def read(self):
        return read_transport_credential(self._home)

    @public_gate
    def status(self):
        credential = self.read()
        return (
            transport_credential_status(credential) if credential is not None else None
        )
