"""A file fence must retain the cause, without exposing exception values."""

from __future__ import annotations

import errno
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.configuration.home_guard import ActiveDaemonHeartbeat
from hyprial.daemon.impl.configuration.home_lease_actor import HomeLeaseAuthority


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        ("write", "OSError[ENOSPC]"),
        ("read", "OSError[EIO]"),
        ("absent", "record-absent"),
        ("mismatch", "record-mismatch"),
    ],
)
def test_file_fence_retains_failure_cause(tmp_path, monkeypatch, failure, detail):
    lost = threading.Event()
    owner = HomeLeaseAuthority(
        tmp_path, keepalive_duration=0.05,
        identity_reader=lambda _pid: "synthetic-identity", ownership_lost=lost.set,
    )
    try:
        owner.claim()
        if failure == "write":
            def fail_write(self):
                raise OSError(errno.ENOSPC, "private exception content")
            monkeypatch.setattr(ActiveDaemonHeartbeat, "_write_record", fail_write)
        else:
            original = Path.read_text

            def read(path, *args, **kwargs):
                if path != owner.path:
                    return original(path, *args, **kwargs)
                if failure == "read":
                    raise OSError(errno.EIO, "private exception content")
                if failure == "absent":
                    raise FileNotFoundError(str(path))
                return '{"generation": "foreign-owner"}'

            # Exercise the actual _read_record exception handling, not a mock
            # which raises outside that handler.
            monkeypatch.setattr(Path, "read_text", read)
        assert lost.wait(2)
        status = owner.status()
        assert status["closing"] and not status["claimed"]
        assert status["fenceReason"] == f"ownership-record-lost:{detail}"
        assert "private exception content" not in str(status)
    finally:
        monkeypatch.undo()
        assert owner.close(2)


@pytest.mark.parametrize("logger_raises", [False, True])
def test_fence_warn_is_structured_and_cannot_prevent_stop(tmp_path, monkeypatch, logger_raises):
    lost = threading.Event()
    events = []

    def log(level, event, **fields):
        events.append((level, event, fields))
        if logger_raises:
            raise OSError(errno.ENOSPC, "log disk is full too")

    owner = HomeLeaseAuthority(
        tmp_path, keepalive_duration=0.05,
        identity_reader=lambda _pid: "synthetic-identity", ownership_lost=lost.set,
        logger=log,
    )
    try:
        owner.claim()

        def fail_write(self):
            raise OSError(errno.ENOSPC, "private exception content")

        monkeypatch.setattr(ActiveDaemonHeartbeat, "_write_record", fail_write)
        assert lost.wait(2), "diagnostics prevented the ownership-lost callback"
        assert len(events) == 1
        level, event, fields = events[0]
        assert (level, event) == ("warn", "daemon.home_lease.fenced")
        assert fields["fenceReason"] == "ownership-record-lost:OSError[ENOSPC]"
        assert fields["generation"] == owner.generation
        assert fields["heartbeatAgeSeconds"] >= 0
    finally:
        monkeypatch.undo()
        assert owner.close(2)
