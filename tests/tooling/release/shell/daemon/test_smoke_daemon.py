"""``smoke_bundle.py`` with the daemon of the home: expected while the app runs, stopped gracefully, kill -9 then restart attaches to the same one.

A fake ``HyprialOS.app`` (``fakes.app``) starts the fake bundled CLI the way the
shell does; the CLI ensures a detached daemon and closing it never stops it.
``codesign`` is a stub on PATH. Roots live under a short ``/tmp`` directory
(AF_UNIX path limit); the ``parent`` fixture is in ``conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

from tests.tooling.release.shell.daemon import fakes
from tests.tooling.release.shell.support import load


def _smoke(tmp_path: Path, parent: Path, mode: str, *, stop: str, app_home: bool, second_start: bool = False, daemon_stop: float = 10.0) -> tuple[int, str, dict]:
    smoke = load("smoke_bundle")
    return smoke.run(fakes.app(tmp_path, mode), root_parent=parent, stop=stop, app_home=app_home, second_start=second_start, startup_timeout=30.0, keep=False, daemon_stop_timeout=daemon_stop)


def _step(evidence: dict, name: str) -> dict:
    return next(item for item in evidence["steps"] if item["name"] == name)


def test_sigterm_smoke_expects_the_daemon_stops_it_gracefully_and_finds_no_orphan(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _smoke(tmp_path, parent, "daemon", stop="sigterm", app_home=False, second_start=True)
    assert (code, line) == (0, "PASS"), evidence
    assert [item["name"] for item in evidence["steps"]] == ["preflight", "launch", "second-start", "daemon", "sigterm", "daemon-stop", "orphans", "integrity"]
    pid = evidence["daemonPid"]
    assert f"pid {pid} (record daemon.json)" in _step(evidence, "daemon")["detail"]
    assert evidence["daemonStop"]["graceful"] is True and evidence["daemonStop"]["fallbackKilled"] == []
    assert not fakes.alive(pid) and evidence["fallbackKilled"] == [] and evidence["leftAtExit"] == []
    assert list(parent.iterdir()) == []


def test_kill9_smoke_restart_attaches_to_the_same_daemon_and_exactly_one_exists(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _smoke(tmp_path, parent, "daemon", stop="kill9", app_home=True)
    assert (code, line) == (0, "PASS"), evidence
    assert [item["name"] for item in evidence["steps"]] == ["preflight", "launch", "daemon", "kill9", "daemon-survives", "restart", "restart-stop", "daemon-stop", "orphans", "integrity"]
    pid = evidence["daemonPid"]
    assert f"attached to the same daemon {pid}" in _step(evidence, "restart")["detail"]
    assert evidence["restartedAppPid"] != evidence["appPid"] and evidence["daemonStop"]["pid"] == pid
    assert evidence["daemonStop"]["graceful"] is True and evidence["daemonStop"]["fallbackKilled"] == []
    assert not fakes.alive(pid) and evidence["fallbackKilled"] == [] and evidence["leftAtExit"] == []


def test_a_restart_that_starts_a_second_daemon_instead_of_attaching_fails(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _smoke(tmp_path, parent, "fresh", stop="kill9", app_home=True)
    assert code == 1 and line.startswith("FAIL restart"), line
    assert "not the one its record names" in line or "not the earlier" in line, line
    assert _step(evidence, "daemon-survives")["status"] == "PASS"
    assert evidence["leftAtExit"] == [] and len(evidence["fallbackKilled"]) >= 2


def test_a_second_daemon_for_the_home_fails_the_daemon_step_while_the_app_runs(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _smoke(tmp_path, parent, "second", stop="sigterm", app_home=False)
    assert code == 1 and line.startswith("FAIL daemon"), line
    assert [item["name"] for item in evidence["steps"]][-1] == "daemon"
    assert evidence["leftAtExit"] == [] and evidence["fallbackKilled"]


def test_a_daemon_no_record_names_fails_even_when_it_is_the_only_one(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _smoke(tmp_path, parent, "unrecorded", stop="sigterm", app_home=False)
    assert code == 1 and line.startswith("FAIL daemon") and "not the one its record names" in line, line


def test_a_graceful_stop_that_fails_is_reported_with_its_fallback_never_silently_accepted(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _smoke(tmp_path, parent, "stubborn", stop="sigterm", app_home=False, daemon_stop=1.0)
    pid = evidence["daemonPid"]
    assert code == 1 and line.startswith("FAIL daemon-stop"), line
    assert "graceful stop failed" in line and "the fallback kill_carriers had to stop" in line
    assert evidence["daemonStop"]["fallbackKilled"] == [pid] and not fakes.alive(pid)
    assert evidence["fallbackKilled"] == [pid]
    assert _step(evidence, "orphans")["status"] == "PASS"


def test_any_other_process_left_on_the_root_still_fails_the_orphans_check(tmp_path: Path, parent: Path) -> None:
    code, line, evidence = _smoke(tmp_path, parent, "stray", stop="sigterm", app_home=False)
    assert code == 1 and line.startswith("FAIL orphans") and "left running" in line, line
    assert _step(evidence, "daemon-stop")["status"] == "PASS"
    assert evidence["leftAtExit"] == [] and len(evidence["fallbackKilled"]) == 1
