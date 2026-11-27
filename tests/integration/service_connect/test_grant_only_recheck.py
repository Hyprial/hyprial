"""A grant that arrives after startup still starts service-connect.

Decision (service grant integration fix, 2026-11-27): the startup question
"does this device hold an effective grant?" is asked again from the
maintenance tick every GRANT_RECHECK_SECONDS while no service manager exists.
It runs off the tick's thread, one at a time, through the same bounded check
startup uses, so a slow directory never holds up maintenance. A hit starts
the manager and the asking stops; daemon shutdown stops it first, so no
manager is started behind the shutdown's back.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.netendpoints import services as services_module
from tests.integration.service_connect.test_grant_only_startup import _application, _grant
from tests.support.runtime._owned_applications import owned_applications as owned_applications


def _settle(app) -> None:
    thread = app._grant_recheck_thread
    if thread is not None:
        thread.join(5.0)
        assert not thread.is_alive()


def test_a_first_grant_after_startup_starts_service_connect(tmp_path: Path) -> None:
    app, _controller = _application(tmp_path)
    app._require_orgfs_runtime()
    app._start_service_connect()
    assert app._service_manager is None

    _grant(app, "magpie")
    app._recheck_grant_only_start(1_000.0)
    _settle(app)
    try:
        assert app._service_manager is not None
        app._service_manager.reconcile()
        connected = app._handle_service_connect("service.connect", {"name": "magpie"})
        assert connected["service"]["state"] == "listening"
    finally:
        assert app._close_service_connect(2.0) is True


def test_the_recheck_runs_once_per_interval_and_stops_after_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _controller = _application(tmp_path)
    app._require_orgfs_runtime()
    asked: list[float] = []
    answer = [False]

    def counting(**_kwargs: object) -> bool:
        asked.append(time.monotonic())
        return answer[0]

    monkeypatch.setattr(services_module, "has_effective_grant", counting)
    interval = services_module.GRANT_RECHECK_SECONDS
    assert interval == 60.0

    for now in (1_000.0, 1_000.0 + interval / 2, 1_000.0 + interval - 1):
        app._recheck_grant_only_start(now)
        _settle(app)
    assert len(asked) == 1

    answer[0] = True
    app._recheck_grant_only_start(1_000.0 + interval)
    _settle(app)
    try:
        assert len(asked) == 2 and app._service_manager is not None
        app._recheck_grant_only_start(1_000.0 + 10 * interval)
        _settle(app)
        assert len(asked) == 2
    finally:
        assert app._close_service_connect(2.0) is True


def test_a_slow_check_never_holds_up_the_tick_and_runs_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _controller = _application(tmp_path)
    app._require_orgfs_runtime()
    release = threading.Event()
    entered: list[int] = []

    def slow(**_kwargs: object) -> bool:
        entered.append(1)
        release.wait(5.0)
        return False

    monkeypatch.setattr(services_module, "has_effective_grant", slow)
    started = time.monotonic()
    app._recheck_grant_only_start(1_000.0)
    app._recheck_grant_only_start(1_000.0 + 2 * services_module.GRANT_RECHECK_SECONDS)
    elapsed = time.monotonic() - started
    release.set()
    _settle(app)

    assert elapsed < 0.5
    assert len(entered) == 1
    assert app._service_manager is None


def test_shutdown_stops_the_recheck_before_it_can_start_a_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _controller = _application(tmp_path)
    app._require_orgfs_runtime()
    release = threading.Event()

    def held(**_kwargs: object) -> bool:
        release.wait(5.0)
        return True

    monkeypatch.setattr(services_module, "has_effective_grant", held)
    app._recheck_grant_only_start(1_000.0)
    stopper = threading.Thread(target=app._stop_grant_recheck, kwargs={"timeout": 0.1})
    stopper.start()
    stopper.join(5.0)
    release.set()
    _settle(app)

    assert app._service_manager is None
    app._recheck_grant_only_start(1_000.0 + 10 * services_module.GRANT_RECHECK_SECONDS)
    _settle(app)
    assert app._service_manager is None


def test_a_home_without_an_orgfs_runtime_is_not_rechecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _controller = _application(tmp_path)
    asked: list[object] = []
    monkeypatch.setattr(services_module, "has_effective_grant", lambda **kwargs: asked.append(kwargs))

    app._recheck_grant_only_start(1_000.0)
    _settle(app)

    assert asked == [] and app._orgfs_runtime is None


def test_the_recipient_side_shares_the_applications_device_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One quarantine per application: a bad device row is reported once, whoever reads it."""

    from hyprial.daemon.impl.org.network.ipc import invalid_device_diagnostics
    from hyprial.daemon.impl.service_grants import ipc as grant_ipc
    from hyprial.daemon.impl.service_grants.consumer import startup as startup_module

    app, _controller = _application(tmp_path)
    runtime = app._require_orgfs_runtime()
    quarantine = invalid_device_diagnostics(app)
    assert not hasattr(grant_ipc, "quarantine_for")
    assert app._grant_source()._store._invalid_device_quiet is quarantine.entries

    built: list[object] = []
    real = startup_module.OrgFsDirectoryStore

    def recording(fs, **kwargs):
        built.append(kwargs.get("invalid_devices"))
        return real(fs, **kwargs)

    monkeypatch.setattr(startup_module, "OrgFsDirectoryStore", recording)
    app._held_grant(runtime)
    assert built == [quarantine]
