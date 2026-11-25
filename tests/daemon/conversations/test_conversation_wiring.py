"""Composition of the conversation recorder: start, resolver binding and shutdown.

``OwnerHomePlacement.authority()`` is pinned here: it answers from the facade
the placement holds with the one rule the cell runtime's author also uses
(``composition.cells.local_author``).  In production the placement's facade
is the app's OrgFS runtime facade, so the recorder's authority and the
runtime's author are the same principal; a test that injects a different
facade pins the placement's behaviour, not a supported split.
"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.application.shutdown import _CLOSE_UNBUDGETED_STEPS
from hyprial.daemon.impl.application.wiring import services
from hyprial.daemon.impl.composition.cells import params as cells_params
from hyprial.daemon.impl.composition.conversations import (
    RECORDER_ENV,
    close_conversation_recorder,
    compose_conversation_recorder,
)
from hyprial.daemon.impl.composition.conversations.placement import OwnerHomePlacement
from hyprial.daemon.impl.conversations import (
    RECORDER_THREAD,
    RECORDS_DATABASE,
    ConversationBodies,
    conversation_cell_id,
)
from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX, BodyRef
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import LEGACY_BUSINESS_DATABASES
from hyprial.daemon.impl.conversations.keys import direct_key
from tests.daemon.conversations.conversation_support import (
    OWNER_PRINCIPAL,
    Log,
    cell_runtime_for,
    home_space,
    record_lines,
)
from tests.daemon.delivery.message.test_message_pending_wait import (
    READER,
    _message,
    app as app,
    owned_applications as owned_applications,
)

CELL = conversation_cell_id(
    OWNER_PRINCIPAL, direct_key("agent:op:node-1:driver", READER).id, "conversation-1"
)


def _threads() -> list[str]:
    return [thread.name for thread in threading.enumerate()]


def _recorded(fs: Any) -> int:
    """Lines recorded so far; a segment the thread is still writing reads as 0."""

    home = home_space(fs)
    if home is None:
        return 0
    try:
        return len(record_lines(fs, home, CELL))
    except OrgFsError:
        return 0


def _wait_for(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_started_recorder_records_new_rows_and_stops_at_close(app, tmp_path) -> None:
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    app._cell_runtime = cell_runtime_for(tmp_path, fs)
    recorder = compose_conversation_recorder(app, fs=fs)
    assert recorder is not None
    assert compose_conversation_recorder(app, fs=fs) is recorder
    assert RECORDER_THREAD in _threads()
    assert isinstance(app._inbox.bodies.resolver, ConversationBodies)

    app._inbox.receive(_message("m-1"))

    assert _wait_for(lambda: _recorded(fs) == 1)
    assert (app.state_dir / RECORDS_DATABASE).is_file()
    close_conversation_recorder(app)
    assert RECORDER_THREAD not in _threads()
    assert compose_conversation_recorder(app, fs=fs) is None
    close_conversation_recorder(app)


def test_daemon_close_stops_the_recorder(app, tmp_path) -> None:
    fs = LocalOrgFs(author=OWNER_PRINCIPAL)
    app._cell_runtime = cell_runtime_for(tmp_path, fs)
    assert compose_conversation_recorder(app, fs=fs) is not None

    app._close()

    assert RECORDER_THREAD not in _threads()
    assert getattr(app, "_conversation_recorder", None) is None


def test_the_recorder_step_is_classified_and_runs_before_the_cell_runtime() -> None:
    steps = [name for name, _ in _CLOSE_UNBUDGETED_STEPS]
    assert "conversation-recorder" in steps
    assert steps.index("conversation-recorder") < steps.index("cell-runtime")


def test_daemon_start_composes_the_recorder() -> None:
    source = inspect.getsource(services._WiringServicesMixin._startup_adopt_domain_services)
    assert "compose_conversation_recorder(self)" in source


def test_the_records_database_joins_legacy_business_databases() -> None:
    assert RECORDS_DATABASE == "conversation-records.sqlite3"
    assert RECORDS_DATABASE in LEGACY_BUSINESS_DATABASES


def test_a_recorder_without_recordable_rows_never_composes_the_cell_runtime(app, monkeypatch) -> None:
    composed: list[Any] = []
    monkeypatch.setattr(cells_params, "cell_runtime", composed.append)
    reads: list[str] = []
    real = app._inbox.interaction_rows_after

    def counted(source: str, after: int, **kwargs: Any) -> Any:
        reads.append(source)
        return real(source, after, **kwargs)

    monkeypatch.setattr(app._inbox, "interaction_rows_after", counted)
    app._inbox.receive(replace(_message("m-peerless"), sender="system:hyprial:node-1"))
    recorder = compose_conversation_recorder(
        app, fs=LocalOrgFs(author=OWNER_PRINCIPAL), start=False, logger=Log()
    )
    assert recorder is not None

    result = recorder.run_once()
    recorder.start()

    assert result.failed is False and result.recorded == 0
    assert _wait_for(lambda: len(reads) >= 4)
    close_conversation_recorder(app)
    assert composed == []


def test_the_recorder_switch_off_binds_the_resolver_and_records_nothing(app, tmp_path) -> None:
    log = Log()
    recorder = compose_conversation_recorder(
        app, fs=LocalOrgFs(author=OWNER_PRINCIPAL), logger=log,
        environ={RECORDER_ENV: "off"},
    )

    assert recorder is None
    assert isinstance(app._inbox.bodies.resolver, ConversationBodies)
    assert RECORDER_THREAD not in _threads()
    assert "conversation.recorder_disabled" in log.names()
    assert not (app.state_dir / RECORDS_DATABASE).exists()


def test_a_closed_recorder_never_revives_orgfs(app, monkeypatch) -> None:
    calls: list[str] = []

    def orgfs() -> Any:
        calls.append("orgfs")
        raise RuntimeError("orgfs closed")

    monkeypatch.setattr(app, "_require_orgfs_runtime", orgfs, raising=False)
    assert compose_conversation_recorder(app, start=False, logger=Log()) is not None
    resolver = app._inbox.bodies.resolver
    close_conversation_recorder(app)
    calls.clear()

    ref = BodyRef(SOURCE_INBOX, 1, "m-1", "0" * 64, f"rec1 space-1 cells/x.jsonl inbox:1:{'0' * 64}")
    assert resolver.resolve(ref) is None
    assert calls == []


def test_the_placement_authority_prefers_the_facade_author(app) -> None:
    placement = OwnerHomePlacement(app, fs=LocalOrgFs(author="user:facade"))

    assert placement.authority() == "user:facade"


def test_the_placement_authority_falls_back_to_the_bare_owner(app) -> None:
    placement = OwnerHomePlacement(app, fs=SimpleNamespace())

    assert placement.authority() == "user:op"


def test_the_placement_authority_keeps_a_user_shaped_owner() -> None:
    placement = OwnerHomePlacement(SimpleNamespace(owner="user:other"), fs=SimpleNamespace())

    assert placement.authority() == "user:other"


def test_the_placement_authority_answers_from_the_injected_facade(app) -> None:
    runtime_facade = LocalOrgFs(author="user:runtime")
    app._require_orgfs_runtime = lambda: SimpleNamespace(facade=runtime_facade)
    placement = OwnerHomePlacement(app, fs=LocalOrgFs(author="user:injected"))

    assert placement.authority() == "user:injected"
