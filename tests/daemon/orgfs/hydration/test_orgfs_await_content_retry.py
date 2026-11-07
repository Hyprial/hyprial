"""await_content keeps provisioning until its deadline, not only one attempt.

CI task 38819 (test_cold_cache_blob_unavailable_then_fetches_when_holder_returns):
the first fetch attempt completed without the blob (the returning holder was not
routable yet), request_blob's event fired, and await_content returned False long
before its deadline.  The event only means "this attempt ended".
"""

from __future__ import annotations

from threading import Event
import time
from types import SimpleNamespace

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

DIGEST = "d" * 64


def _runtime_with_stub_mesh(tmp_path, monkeypatch, deliver_on_attempt: int):
    runtime = OrgFsRuntime(tmp_path / "reader", node_id="node-reader", author="user:r")
    held: set[str] = set()
    attempts: list[float] = []

    def request_blob(digest: str) -> Event:
        attempts.append(time.monotonic())
        if len(attempts) >= deliver_on_attempt:
            held.add(digest)
        done = Event()
        done.set()  # the attempt ended, with or without the blob
        return done

    monkeypatch.setattr(runtime, "_mesh", lambda _space_id: SimpleNamespace(request_blob=request_blob))
    monkeypatch.setattr(
        runtime.facade,
        "stat",
        lambda _space, _node: SimpleNamespace(
            kind="blob", content_state="pending", blob_hash=DIGEST
        ),
    )
    monkeypatch.setattr(runtime.blobs, "contains", lambda digest: digest in held)
    return runtime, attempts


def test_await_content_retries_until_the_blob_arrives(tmp_path, monkeypatch) -> None:
    runtime, attempts = _runtime_with_stub_mesh(tmp_path, monkeypatch, deliver_on_attempt=3)
    try:
        assert runtime.await_content(
            "space", "node", deadline_monotonic=time.monotonic() + 5
        )
        assert len(attempts) == 3
    finally:
        runtime.close()


def test_await_content_gives_up_at_the_deadline_without_spinning(
    tmp_path, monkeypatch
) -> None:
    runtime, attempts = _runtime_with_stub_mesh(tmp_path, monkeypatch, deliver_on_attempt=10**9)
    try:
        started = time.monotonic()
        assert not runtime.await_content(
            "space", "node", deadline_monotonic=started + 0.5
        )
        elapsed = time.monotonic() - started
        assert 0.45 <= elapsed < 1.5
        # Backoff between attempts: no hot loop on an immediately-set event.
        assert len(attempts) <= 20
    finally:
        runtime.close()
