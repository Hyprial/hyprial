"""``--port 0``: the host binds the port and the launcher reports what it bound.

Decision record (2026-11-16, PR 6a): the plan keeps ``port == 0`` until the host's
ready line names the bound port; the session then rebinds the plan so the start
record, the health probe and the handoff URL carry the real origin.  The browser
never talks to the backend directly.  The contract mock is passed no origin for
a plan without a known port and uses its own DEFAULT_ORIGINS (see
backend/config.py).  Decision (2822, B4): the daemon driver has no request-http
origin allowlist any more, so there is nothing to give it for an unbound port.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import source as backend_source
from hyprial.shell.impl.gui.errors import GuiError
from tests.shell.gui.backend import test_source_session as fixtures

BOUND = 41234


def _unbound(plan: launcher.GuiLaunchPlan) -> launcher.GuiLaunchPlan:
    return replace(plan, port=0, url="http://127.0.0.1:0/")


def _announce(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    original = fixtures._ready_line

    def line(plan: launcher.GuiLaunchPlan) -> str:
        document = json.loads(original(plan)[len(backend_source.HOST_READY_PREFIX) :])
        document.update({"port": port, "url": f"http://127.0.0.1:{port}"})
        return backend_source.HOST_READY_PREFIX + json.dumps(document)

    monkeypatch.setattr(fixtures, "_ready_line", line)


def test_the_start_record_and_handoff_use_the_port_the_host_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _announce(monkeypatch, BOUND)
    plan = _unbound(fixtures._plan(tmp_path))
    session, _events, captured = fixtures._start(monkeypatch, plan, open_browser=False)
    try:
        payload = launcher.startup_payload(session)
        assert payload["port"] == BOUND
        assert payload["url"] == f"http://127.0.0.1:{BOUND}/"
        assert session.plan.port == BOUND
        assert session.handoff_path is not None
        launch = session.handoff_path.read_text(encoding="utf-8").strip()
        assert launch.startswith(f"http://127.0.0.1:{BOUND}/#grant=")
        assert fixtures.GRANT not in json.dumps(payload)
    finally:
        session.stop()


def test_an_announced_port_is_checked_when_the_plan_asked_for_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _unbound(fixtures._plan(tmp_path))
    for bad in (0, 70000, True, "41234"):
        line = backend_source.HOST_READY_PREFIX + json.dumps(
            {
                "service": "hyprial-gui",
                "mode": "production",
                "dataMode": "hosted",
                "driver": plan.driver,
                "backendConfigured": True,
                "session": "launch-required",
                "host": "127.0.0.1",
                "port": bad,
                "url": f"http://127.0.0.1:{bad}",
            }
        )
        with pytest.raises(GuiError) as caught:
            backend_source.parse_host_ready(line, plan)
        assert caught.value.code == "GUI_BACKEND_CONTRACT"


def test_a_fixed_port_still_has_to_match_the_ready_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _announce(monkeypatch, BOUND)
    plan = fixtures._plan(tmp_path)
    events, _captured = fixtures._install_success_seams(monkeypatch, plan)
    with pytest.raises(GuiError) as caught:
        fixtures._run_start(replace(plan, open_browser=False), events, None)
    assert caught.value.code == "GUI_BACKEND_CONTRACT"


def test_dev_mode_reports_the_host_port_and_not_the_vite_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _announce(monkeypatch, BOUND)
    plan = _unbound(fixtures._dev_plan(tmp_path))
    session, _events, captured = fixtures._start(monkeypatch, plan, open_browser=False)
    try:
        payload = launcher.startup_payload(session)
        assert payload["port"] == BOUND
        assert payload["url"] == f"http://127.0.0.1:{BOUND}/"
        assert captured["frontend"]["argv"][-2:] == ("--live-upstream", "http://127.0.0.1:5173")
        assert payload["assets_pid"] == 103
        assert session.handoff_path is not None
        assert session.handoff_path.read_text(encoding="utf-8").startswith(
            f"http://127.0.0.1:{BOUND}/#grant="
        )
    finally:
        session.stop()


def test_unbound_port_gives_the_mock_its_own_default_origin(
    tmp_path: Path,
) -> None:
    """Mock: no ``--allow-origin`` is passed, so ``serve.ts`` falls back to its
    DEFAULT_ORIGINS (not "no origin")."""
    mock = fixtures._plan(tmp_path)
    argv, _cwd, _env = backend_source.mock_invocation(_unbound(mock))
    assert "--allow-origin" not in argv
    argv, _cwd, _env = backend_source.mock_invocation(mock)
    assert argv[argv.index("--allow-origin") + 1] == "http://127.0.0.1:4173"
