#!/usr/bin/env python3
"""Prove queryable request/reply across two isolated Python peer sessions."""

from __future__ import annotations

from importlib.metadata import version
import sys
import time
import uuid

import zenoh

from _common import connected_loopback_sessions, emit


def main() -> int:
    key = f"hyprial/spike/{uuid.uuid4().hex}/registry/node"
    expected = b'{"actor":"agent:spike:local:worker"}'
    with connected_loopback_sessions() as (server, client):
        queryable = server.declare_queryable(
            key,
            lambda query: query.reply(key, expected, encoding="application/json"),
            complete=True,
        )
        try:
            time.sleep(0.5)  # allow the remote queryable declaration to propagate
            replies = client.get(key, target=zenoh.QueryTarget.ALL, timeout=3.0)
            reply = replies.recv()
            sample = reply.ok
            if sample is None:
                raise AssertionError(f"query returned error: {reply.err}")
            actual = sample.payload.to_bytes()
            if actual != expected:
                raise AssertionError(f"payload mismatch: {actual!r}")
        finally:
            queryable.undeclare()
    emit({
        "capability": "queryable",
        "eclipse_zenoh": version("eclipse-zenoh"),
        "isolated_transport": "explicit-loopback-tcp",
        "request_reply": "PASS",
        "status": "AVAILABLE_IN_PYTHON_BINDING",
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        emit({"capability": "queryable", "status": "FAIL", "error": str(error)})
        sys.exit(1)
