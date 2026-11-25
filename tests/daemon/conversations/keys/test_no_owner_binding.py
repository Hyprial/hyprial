# Decision record (S3a red tests R3; no inline comments by repository rule):
# - A profile that still carries an owner thread binding in users.json does not
#   alias user:<owner> any more: the application's conversation stamp (the
#   send path) and the history key both answer the pure user key, and
#   direct_key has no owner_binding parameter.
# - S3c: the users profile is gone, so the application is built without one.
"""The user conversation key is never aliased to an owner thread."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from hyprial.daemon.impl.application.messaging.inbox_surface.history import _thread_key
from hyprial.daemon.impl.application.messaging.inbox_surface.send_receipts import (
    _SendReceiptsMixin,
)
from hyprial.daemon.impl.conversations import keys
from hyprial.daemon.impl.conversations.keys import direct_key

WORKER = "agent:alice:node:worker"


def test_a_send_to_the_owner_keeps_the_pure_user_key() -> None:
    app = SimpleNamespace(_log=None, node_id="node")
    derive = _thread_key(app)

    for recipient in ("user:alice", "user:alice-key"):
        pure = direct_key(WORKER, recipient)
        assert pure is not None and pure.kind == "user"
        assert derive(WORKER, recipient) == pure
        assert _SendReceiptsMixin._conversation_stamp(app, WORKER, recipient) == pure.id


def test_direct_key_has_no_owner_binding_parameter() -> None:
    assert "owner_binding" not in inspect.signature(direct_key).parameters
    assert not hasattr(keys, "owner_binding_key")
