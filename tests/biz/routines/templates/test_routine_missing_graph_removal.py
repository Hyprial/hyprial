"""Routine removal tolerates an absent graph without hiding other close failures."""

import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.identity import PacError, PAC_GRAPH_NOT_OWNER
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import default_database_path


@pytest.mark.parametrize("entry", ["helper", "ipc"])
@pytest.mark.parametrize("failure", [None, "owner", "io"])
def test_removal_joins_graph_owner_and_only_ignores_absence(
    tmp_path, monkeypatch, entry, failure,
):
    authority = PacGraphAuthority(default_database_path(tmp_path), NullSender())
    removed, cancelled, retired = [], [], []
    routine = {"owner": "user:test", "enabled": False, "inFlight": [{"runId": "missing"}]}
    service = SimpleNamespace(
        status=lambda **kw: dict(routine),
        reserve_remove=lambda **kw: dict(routine),
        remove=lambda **kw: removed.append(kw) or {"removed": True},
        cancel_remove=lambda **kw: cancelled.append(kw),
    )
    # Exercise the uncomposed compatibility entry points with their actual
    # typed PAC owner. No daemon constructor or unrelated domains are needed.
    app = object.__new__(DaemonApplication)
    app._restore_done = threading.Event()
    app._restore_done.set()
    app._inbox = app._harnesses = app._presence = object()
    app.owner = "test"
    app._workflow_caller = lambda params: "user:test"
    app._routine_service = service
    app._routine_coordinator = None
    app._transport = None
    app._routine_legacy_lock = threading.Lock()
    app._pac_graph_authority = authority
    app._retire_routine_coordinator = lambda row: retired.append(row)
    error = (
        PacError(PAC_GRAPH_NOT_OWNER, "not owner") if failure == "owner"
        else OSError("storage unavailable") if failure == "io" else None
    )
    if error is not None:
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(authority, "close_graph", fail)

    def remove():
        if entry == "helper":
            return app._remove_registered_routine("routine", enforce_last=True)
        return app.handle("routine.remove", {"name": "routine"})

    try:
        if error is None:
            assert remove()["removed"] is True
            assert len(removed) == len(retired) == 1
            assert cancelled == []
        else:
            with pytest.raises(type(error)) as caught:
                remove()
            assert caught.value is error
            assert removed == retired == []
            assert len(cancelled) == 1
    finally:
        assert authority.close(3.0)
