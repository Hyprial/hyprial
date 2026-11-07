#!/usr/bin/env python3
"""Prove liveliness token appearance/disappearance across isolated peers."""

from __future__ import annotations

from importlib.metadata import version
from queue import Queue
import sys
import time
import uuid

import zenoh

from _common import connected_loopback_sessions, emit


def main() -> int:
    key = f"hyprial/spike/{uuid.uuid4().hex}/liveliness/mailbox/local-node"
    events: Queue[zenoh.SampleKind] = Queue()
    with connected_loopback_sessions() as (owner, observer):
        subscriber = observer.liveliness().declare_subscriber(
            key,
            lambda sample: events.put(sample.kind),
            history=True,
        )
        try:
            time.sleep(0.5)  # allow the remote liveliness subscription to propagate
            token = owner.liveliness().declare_token(key)
            appeared = events.get(timeout=3.0)
            token.undeclare()
            disappeared = events.get(timeout=3.0)
        finally:
            subscriber.undeclare()
    if appeared != zenoh.SampleKind.PUT:
        raise AssertionError(f"expected PUT appearance, got {appeared!r}")
    if disappeared != zenoh.SampleKind.DELETE:
        raise AssertionError(f"expected DELETE disappearance, got {disappeared!r}")
    emit({
        "capability": "liveliness",
        "eclipse_zenoh": version("eclipse-zenoh"),
        "isolated_transport": "explicit-loopback-tcp",
        "token_appearance": "PASS",
        "token_disappearance": "PASS",
        "status": "AVAILABLE_IN_PYTHON_BINDING",
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        emit({"capability": "liveliness", "status": "FAIL", "error": str(error)})
        sys.exit(1)
