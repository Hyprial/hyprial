"""Desired-state I/O errors cross the Harness actor as immutable facts."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor, HarnessRuntimeFacade
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessDesiredStateManaged

from tests.daemon.lifecycle.lifecycle.ports.support import _Launcher


def test_permission_error_reaches_management_facade_with_errno_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    events: list[object] = []
    actor = HarnessRuntimeActor(_Launcher(), desired_state=store, event_sink=events.append)
    facade = HarnessRuntimeFacade(actor)
    snapshot = facade.snapshot_adapter_registration("lark-main")
    path = str(tmp_path / "desired.json")

    def refuse(*_args: object, **_kwargs: object) -> bool:
        raise PermissionError(errno.EACCES, "write denied", path)

    monkeypatch.setattr(store, "remove_adapter_registration", refuse)
    try:
        with pytest.raises(PermissionError) as caught:
            facade.remove_adapter_registration(snapshot)
        assert caught.value.errno == errno.EACCES
        assert caught.value.filename == path
        assert caught.value.strerror == "write denied"
        failures = [
            event for event in events
            if isinstance(event, HarnessDesiredStateManaged)
            and event.error_code is not None
        ]
        assert len(failures) == 1
        assert failures[0].error is None
        assert failures[0].error_code == "PermissionError"
        assert failures[0].error_is_oserror is True
        assert failures[0].error_filename == path
    finally:
        assert actor.close_runtime(3.0)


def test_direct_snapshot_error_is_frozen_without_changing_public_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    events: list[object] = []
    actor = HarnessRuntimeActor(_Launcher(), desired_state=store, event_sink=events.append)
    facade = HarnessRuntimeFacade(actor)
    original_load = store.load
    path = str(tmp_path / "desired.json")

    def refuse():
        raise PermissionError(errno.EACCES, "read denied", path)

    monkeypatch.setattr(store, "load", refuse)
    try:
        with pytest.raises(PermissionError) as caught:
            facade.snapshot_adapter_registration("lark-main")
        assert caught.value.errno == errno.EACCES
        assert caught.value.filename == path
        failures = [
            event for event in events
            if isinstance(event, HarnessDesiredStateManaged)
            and event.error_code is not None
        ]
        assert len(failures) == 1
        assert failures[0].error is None
        assert failures[0].error_is_oserror is True
    finally:
        monkeypatch.setattr(store, "load", original_load)
        assert actor.close_runtime(3.0)


def test_missing_store_management_error_keeps_plain_runtime_message() -> None:
    events: list[object] = []
    actor = HarnessRuntimeActor(_Launcher(), event_sink=events.append)
    facade = HarnessRuntimeFacade(actor)
    try:
        with pytest.raises(RuntimeError) as caught:
            facade.snapshot_adapter_registration("lark-main")
        assert str(caught.value) == (
            "Harness desired-state management requires DesiredStateStore"
        )
        failures = [
            event for event in events
            if isinstance(event, HarnessDesiredStateManaged)
            and event.error_detail is not None
        ]
        assert len(failures) == 1
        assert failures[0].error is None
    finally:
        assert actor.close_runtime(3.0)
