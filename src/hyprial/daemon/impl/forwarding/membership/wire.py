"""Correlated stdio v6 membership commands; the Go exchange timeout is 30 seconds.

The wire awaits a reply for 31 seconds (just past the Go exchange); an IPC
request path may pass a shorter ``timeout``.  A reply that arrives after the
wait is buffered by the shared response demultiplexer and aged out, never
misattributed.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

from hyprial.kernel import ipc_errors

from ..policy import FORWARD_PROTOCOL_VERSION, ForwardingSidecarError

WIRE_MEMBERSHIP_WAIT_SECONDS = 31.0

OPERATIONS = frozenset({
    "membership-init", "roster-fetch", "member-announce", "member-revoke",
    "membership-status", "invite-create", "invite-redeem", "authority-repin",
})
CAPABILITIES = frozenset({"tailnet-membership-v1", "tailnet-control-v1", "unix-inbound-v1"})


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class MembershipWireMixin:
    def _validate_running_membership(self, event):
        readiness = event.get('readiness')
        if (event.get('requestId') != self._up['requestId']
                or not isinstance(readiness, dict)
                or set(readiness) != {'sidecar', 'control', 'member'}
                or any(type(value) is not bool for value in readiness.values())
                or not readiness['sidecar'] or not readiness['control']):
            raise ForwardingSidecarError('Invalid v6 startup readiness/correlation.', code='SIDECAR_PROTOCOL_MISMATCH')
        self.readiness = dict(readiness)

    def membership_request(
        self, op: str, params: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        if op not in OPERATIONS or not isinstance(params, Mapping):
            raise ForwardingSidecarError("Invalid membership command.", code="BAD_REQUEST")
        request_id = uuid.uuid4().hex
        request = {"v": FORWARD_PROTOCOL_VERSION, "op": op,
                   "requestId": request_id, "params": dict(params)}
        if len(json.dumps(request).encode()) > 1024 * 1024:
            raise ForwardingSidecarError("Membership frame is too large.", code="MEMBERSHIP_LIMIT_EXCEEDED")
        wait = WIRE_MEMBERSHIP_WAIT_SECONDS if timeout is None else min(float(timeout), WIRE_MEMBERSHIP_WAIT_SECONDS)
        with self._write_lock:
            self._send(request)
        deadline = time.monotonic() + wait
        try:
            event = self._await_response(
                lambda candidate: candidate.get("requestId") == request_id,
                deadline=deadline,
            )
        except ForwardingSidecarError as error:
            if error.code == "FORWARD_PROTOCOL" and time.monotonic() >= deadline:
                raise ForwardingSidecarError(
                    f"Tailnet membership command {op} timed out after {wait:g}s.",
                    code=ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
                ) from error
            raise
        if event.get("op") != op:
            raise ForwardingSidecarError("Membership response operation mismatch.", code="BAD_REQUEST")
        if event.get("event") == "error":
            # Never echo a remote secret-bearing message in daemon diagnostics.
            raise ForwardingSidecarError("Tailnet membership command failed.",
                                         code=str(event.get("code") or "BAD_REQUEST"))
        if (event.get("event") != "membership-result"
                or not isinstance(event.get("data"), dict)
                or set(event) != {"v", "event", "requestId", "op", "data"}):
            raise ForwardingSidecarError("Invalid membership response.", code="BAD_REQUEST")
        return event["data"]
