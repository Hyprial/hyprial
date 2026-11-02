"""Correlated v6 membership commands use operation-specific exchange budgets.

Redeem includes two Go control exchanges. Late replies remain correlated and
are aged out by the shared response demultiplexer, never misattributed.
Transport diagnostics follow contract/sidecar-tailcat: byte counts include at
most one overflow-detection byte beyond the control frame maximum, not content.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from hyprial.kernel import ipc_errors

from ..policy import FORWARD_PROTOCOL_VERSION, ForwardingSidecarError, membership_wait_seconds

OPERATIONS = frozenset({
    "membership-init", "roster-fetch", "member-announce", "member-revoke",
    "membership-status", "invite-create", "invite-redeem", "authority-repin",
})
CAPABILITIES = frozenset({"tailnet-membership-v1", "tailnet-control-v1", "unix-inbound-v1"})
CONTROL_FRAME_MAX_BYTES = 1 << 20


def _diagnostic_node(value: object) -> bool:
    if not isinstance(value, str) or ':' not in value:
        return False
    host, port = value.rsplit(':', 1)
    if not re.fullmatch(r'[0-9]{1,5}', port) or not 0 < int(port) <= 65535:
        return False
    if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', host):
        return True
    if host.startswith('[') and host.endswith(']'):
        try:
            ipaddress.IPv6Address(host[1:-1])
            return '%' not in host
        except ValueError:
            pass
    return False


def transport_diagnostic(value: object) -> dict[str, Any] | None:
    """Accept closed transport classifications and bounded counts, never payloads."""
    required = {'stage', 'cause', 'proxy'}
    if (not isinstance(value, dict) or not required <= value.keys()
            or not value.keys() <= required | {'attempts', 'bytesReceived'}):
        return None
    vocabulary = {
        'stage': ('control', 'derp', 'sidecar'),
        'cause': ('proxy-rejected', 'connection-refused', 'dns-failed', 'tls-failed',
                  'deadline-exceeded', 'transport-failed', 'response-timeout'),
        'proxy': ('configured', 'not-configured'),
    }
    if any(value[key] not in allowed for key, allowed in vocabulary.items()):
        return None
    if 'bytesReceived' in value:
        count = value['bytesReceived']
        if (type(count) is not int or not 0 <= count <= CONTROL_FRAME_MAX_BYTES + 1
                or value['stage'] != 'control'
                or value['cause'] not in ('deadline-exceeded', 'transport-failed')):
            return None
    if 'attempts' in value:
        attempts = value['attempts']
        if not isinstance(attempts, list):
            return None
        for attempt in attempts:
            if (not isinstance(attempt, dict)
                    or set(attempt) not in ({'node', 'cause'}, {'node', 'cause', 'source'})
                    or not _diagnostic_node(attempt['node'])
                    or attempt['cause'] not in vocabulary['cause']
                    or ('source' in attempt and attempt['source'] != 'diagnostic-probe')):
                return None
        return dict(value, attempts=[dict(attempt) for attempt in attempts])
    return dict(value)


def diagnostic_message(data: dict[str, Any]) -> str:
    message = f"{data['stage']} unreachable: {data['cause']} (proxy {data['proxy']})."
    return message + ''.join(
        f" {attempt['node']}: {attempt['cause']}"
        + (' (diagnostic probe).' if attempt.get('source') else '.')
        for attempt in data.get('attempts', [])
    )


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
        if len(json.dumps(request).encode()) > CONTROL_FRAME_MAX_BYTES:
            raise ForwardingSidecarError("Membership frame is too large.", code="MEMBERSHIP_LIMIT_EXCEEDED")
        wait = membership_wait_seconds(op, timeout)
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
                    details={"operation": op, "leg": op, "budgetSeconds": wait},
                ) from error
            raise
        if event.get("op") != op:
            raise ForwardingSidecarError("Membership response operation mismatch.", code="BAD_REQUEST")
        if event.get("event") == "error":
            data = transport_diagnostic(event.get('data'))
            message = diagnostic_message(data) if data else "Tailnet membership command failed."
            code = str(event.get("code") or "BAD_REQUEST")
            details = data or {}
            if code == ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT:
                details = details | {"operation": op, "leg": op, "budgetSeconds": wait}
            raise ForwardingSidecarError(message, code=code, details=details)
        if (event.get("event") != "membership-result"
                or not isinstance(event.get("data"), dict)
                or set(event) != {"v", "event", "requestId", "op", "data"}):
            raise ForwardingSidecarError("Invalid membership response.", code="BAD_REQUEST")
        return event["data"]
