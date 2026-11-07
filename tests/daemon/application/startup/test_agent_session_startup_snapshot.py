"""Agent callbacks must be usable while the domain constructor is running."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import hyprial.daemon.impl.application as application_module
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


def test_agent_worker_callback_has_snapshot_state_before_domains_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_domains = application_module.AgentSessionDomains
    original_snapshot = application_module.DaemonApplication._current_worker_snapshot
    observed_locals = []
    callback_outcomes = []
    constructor_returned = threading.Event()

    def observe_snapshot(app):
        assert not constructor_returned.is_set()
        result = original_snapshot(app)
        observed_locals.append(app._worker_snapshot_local)
        return result

    def construct_domains(**kwargs):
        domains = original_domains(**kwargs)

        def run_callback():
            try:
                callback_outcomes.append(kwargs["worker_running"](
                    "agent:probe:isolated:worker"
                ))
            except BaseException as error:
                callback_outcomes.append(error)

        # The real Agent actor is already started, but the application has
        # not received the domains. Force the startup scheduling window.
        worker = threading.Thread(target=run_callback, name="probe-agent-startup")
        worker.start()
        worker.join(5.0)  # Orchestration slack, not a product latency assertion.
        if worker.is_alive() or not callback_outcomes or isinstance(callback_outcomes[0], BaseException):
            domains.close()
            assert not worker.is_alive(), "startup callback did not return"
            if callback_outcomes and isinstance(callback_outcomes[0], BaseException):
                raise callback_outcomes[0]
            raise AssertionError("startup callback produced no outcome")
        constructor_returned.set()
        return domains

    monkeypatch.setattr(application_module, "AgentSessionDomains", construct_domains)
    monkeypatch.setattr(
        application_module.DaemonApplication, "_current_worker_snapshot", observe_snapshot
    )
    home = tmp_path / "home"
    home.mkdir()
    app = application_module.DaemonApplication(
        state_dir=home / "state", socket_path=home / "state" / "daemon.sock",
        node_id="isolated", owner="probe", hyprial_home=home,
        zenoh_listen=(), zenoh_connect=(),
    **_deep_module_daemon_dependencies())
    try:
        assert constructor_returned.is_set()
        assert callback_outcomes == [None]
        assert len(observed_locals) == 1
        # Later __init__ code must not replace the state visible to actors.
        assert app._worker_snapshot_local is observed_locals[0]
    finally:
        app._close()
        app._close_home_lease()
