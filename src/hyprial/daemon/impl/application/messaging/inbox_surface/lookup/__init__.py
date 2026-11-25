"""Authenticated, read-only resolution of one original GUI agent send."""

import sqlite3
import re

from hyprial.daemon.impl.ipc.params import _required_string
from hyprial.kernel import DaemonRequestError, ipc_errors, parse_agent_uri

_KEYS = frozenset({
    "from", "actor", "sessionRef", "target", "text", "conversationId", "idempotencyKey",
})
_KEY_LOOKUP_KEYS = frozenset({"from", "actor", "sessionRef", "operationKey"})
_OPERATION_KEY = re.compile(r"[A-Za-z0-9_-]{22,128}\Z")


class _MessageSendLookupMixin:
    def _ipc_message_send_lookup(self, method, params, _trusted_message_origin=None):
        caller = self._authenticated_message_caller(
            params, method=method, trusted_origin=_trusted_message_origin,
        )
        if _trusted_message_origin == "ipc" and caller.via == "daemon":
            raise self._sender_not_authenticated(caller.subject)
        if set(params) - _KEYS:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "unexpected message.send.lookup fields",
            )
        target = _required_string(params.get("target"), "target")
        if parse_agent_uri(target) is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "lookup target must be a canonical agent URI",
            )
        selectors = {
            "sender": caller.sender,
            "target": target,
            "text": _required_string(params.get("text"), "text"),
            "conversation_id": _required_string(params.get("conversationId"), "conversationId"),
            "idempotency_key": _required_string(params.get("idempotencyKey"), "idempotencyKey"),
        }
        try:
            return self._inbox.lookup_send(**selectors)
        except (sqlite3.Error, OSError):
            return {"status": "unknown", "reason": "unavailable"}

    def _ipc_message_send_key_lookup(self, method, params, _trusted_message_origin=None):
        caller = self._authenticated_message_caller(
            params, method=method, trusted_origin=_trusted_message_origin,
        )
        if _trusted_message_origin == "ipc" and caller.via == "daemon":
            raise self._sender_not_authenticated(caller.subject)
        if set(params) - _KEY_LOOKUP_KEYS:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "unexpected message.send.key_lookup fields",
            )
        operation_key = _required_string(params.get("operationKey"), "operationKey")
        if _OPERATION_KEY.fullmatch(operation_key) is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "operationKey must be a URL-safe 22-128 character key",
            )
        try:
            return self._inbox.key_lookup_send(
                sender=caller.sender, operation_key=operation_key,
            )
        except (sqlite3.Error, OSError):
            return {"status": "unknown", "reason": "unavailable"}
