"""Unit tests for ``contract.e2e._gui_browser.session`` cleanup, fencing, and probes.

Decision record (2026-11-21):
- Strict TDD suite covering critic2 and critic3 fixes:
  1. Detached Chromium browser PGID tracking and signalling when leader birth
     identity is live and verified.
  2. Non-object JSON probe output coercion in ``missing_prerequisites``.
  3. Strict fail-closed birth-identity fencing: ``_group_state`` returns
     ``"unverified"`` (never signals) whenever leader ``observed is None``,
     including when the leader exited while PGID descendants remain.
  4. Converting ``ValueError`` from ``urlsplit`` / ``parsed.port`` into ``AssertionError``.
  5. Strict PID integer bounds (``<= 2_147_483_647``) and ``ValueError``/``OverflowError``
     guards across ``_probe_pid_file``, ``_record_frontend_pid``, and
     ``_record_browser_pid``, plus failing ``survivors()`` when a dead
     child-reported PID has no observed birth identity or provenance.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "contract" / "e2e"))

from _gui_browser import session as browser_session  # noqa: E402


class _FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        returncode: int | None = None,
        stdout_text: str = "",
        communicate_side_effect: Any = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout_text)
        self._communicate_side_effect = communicate_side_effect
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self._communicate_side_effect is not None:
            return self._communicate_side_effect(timeout)
        self.returncode = 0
        return ('{"ok": true}\n', "")


def _make_session(tmp_path: Path) -> browser_session.GuiSession:
    root = tmp_path / "root"
    root.mkdir(parents=True)
    return browser_session.GuiSession(tmp_path / "hyprial", root, "e2e-org-switch")


def test_missing_prerequisites_handles_non_dict_json_probe_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_root = tmp_path / "gui"
    pkg = gui_root / "node_modules" / "playwright-core" / "package.json"
    pkg.parent.mkdir(parents=True)
    pkg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(browser_session, "GUI_ROOT", gui_root)
    monkeypatch.setattr(browser_session.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    monkeypatch.setattr(
        browser_session.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 17, stdout="[]\n", stderr=""),
    )
    nonzero_missing = browser_session.missing_prerequisites()
    assert nonzero_missing
    assert "exited 17" in nonzero_missing[0]

    monkeypatch.setattr(
        browser_session.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="null\n", stderr=""),
    )
    zero_missing = browser_session.missing_prerequisites()
    assert zero_missing
    assert "no browser" in zero_missing[0]


@pytest.mark.parametrize("bad_url", ["http://[::1", "http://127.0.0.1:bad"])
def test_validated_loopback_origin_converts_value_error_to_assertion_error(bad_url: str) -> None:
    with pytest.raises(AssertionError, match="could not be parsed"):
        browser_session._validated_loopback_origin(bad_url, 8080)


def test_run_browser_tracks_detached_browser_pgid_and_signals_surviving_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    node_pid = 41001
    browser_pid = 42002
    live_groups = {browser_pid}
    signalled_groups: list[tuple[int, int]] = []
    signalled_pids: list[tuple[int, int]] = []

    def fake_communicate(timeout: float | None = None) -> tuple[str, str]:
        pid_file = gui_session.root / "browser.pid"
        tmp_file = Path(f"{pid_file}.tmp")
        tmp_file.write_text(f"{browser_pid}\n", encoding="utf-8")
        tmp_file.replace(pid_file)
        fake_node.returncode = 0
        return ('{"ok": true}\n', "")

    fake_node = _FakeProcess(node_pid, communicate_side_effect=fake_communicate)
    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: fake_node)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: {node_pid: "b:node", browser_pid: "b:browser"}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_safe_getpgid",
        lambda pid: {node_pid: node_pid, browser_pid: browser_pid}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_process_ppid",
        lambda pid: {browser_pid: node_pid}.get(pid),
    )
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: pgid in live_groups)
    monkeypatch.setattr(browser_session, "_alive", lambda pid: False)
    monkeypatch.setattr(
        browser_session,
        "_signal_group",
        lambda pgid, sig: signalled_groups.append((pgid, sig)),
    )
    monkeypatch.setattr(
        browser_session,
        "_signal_pid",
        lambda pid, sig: signalled_pids.append((pid, sig)),
    )

    result = gui_session.run_browser(tmp_path / "browser.mjs", {"orgIds": ["a", "b"]}, 30.0)
    assert result == {"ok": True}
    assert browser_pid in gui_session.groups
    assert any(
        group.label == "browser" and group.pgid == browser_pid and group.birth == "b:browser"
        for group in gui_session._tracked_groups
    )
    assert gui_session.reported_pids["browser"] == browser_pid

    assert f"group {browser_pid}" in gui_session.survivors()

    monkeypatch.setattr(gui_session, "_wait_all_gone", lambda timeout: None)
    gui_session.stop()
    assert (browser_pid, signal.SIGTERM) in signalled_groups
    assert (browser_pid, signal.SIGKILL) in signalled_groups
    assert signalled_pids == []

    signalled_groups.clear()
    survivors_after = gui_session.force_kill_survivors(timeout=0.05)
    assert (browser_pid, signal.SIGKILL) in signalled_groups
    assert f"group {browser_pid}" in survivors_after


def test_group_state_is_fail_closed_unverified_when_leader_disappears_or_birth_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    browser_pid = 43001
    group = browser_session.TrackedGroup("browser", browser_pid, browser_pid, "b:browser")
    gui_session._tracked_groups.append(group)

    signalled_groups: list[tuple[int, int]] = []
    signalled_pids: list[tuple[int, int]] = []
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: pgid == browser_pid)
    monkeypatch.setattr(browser_session, "process_birth_identity", lambda pid: None)

    def raise_no_such_process(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(browser_session.os, "kill", raise_no_such_process)
    monkeypatch.setattr(
        browser_session,
        "_signal_group",
        lambda pgid, sig: signalled_groups.append((pgid, sig)),
    )
    monkeypatch.setattr(
        browser_session,
        "_signal_pid",
        lambda pid, sig: signalled_pids.append((pid, sig)),
    )

    assert browser_session._group_state(group) == "unverified"
    assert f"unverified group {browser_pid}" in gui_session.survivors()
    gui_session.stop()
    survivors_after = gui_session.force_kill_survivors(timeout=0.05)
    assert f"unverified group {browser_pid}" in survivors_after
    assert signalled_groups == []
    assert signalled_pids == []


def test_birth_identity_fencing_blocks_signalling_reused_pids_and_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    reused_pgid = 51001
    reused_pid = 51002
    gui_session._tracked_groups.append(
        browser_session.TrackedGroup("daemon", reused_pgid, reused_pgid, "b:old-group")
    )
    gui_session._tracked_pids["host"] = browser_session.TrackedPid("host", reused_pid, "b:old-pid")

    signalled_groups: list[tuple[int, int]] = []
    signalled_pids: list[tuple[int, int]] = []
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: True)
    monkeypatch.setattr(browser_session, "_alive", lambda pid: True)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: {reused_pgid: "b:new-group", reused_pid: "b:new-pid"}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_signal_group",
        lambda pgid, sig: signalled_groups.append((pgid, sig)),
    )
    monkeypatch.setattr(
        browser_session,
        "_signal_pid",
        lambda pid, sig: signalled_pids.append((pid, sig)),
    )

    assert gui_session.survivors() == []
    gui_session.stop()
    assert gui_session.force_kill_survivors(timeout=0.05) == []
    assert signalled_groups == []
    assert signalled_pids == []
    assert gui_session._tracked_groups[0].verified_dead is True
    assert gui_session._tracked_pids["host"].verified_dead is True


def test_unverified_or_forged_identities_are_never_signalled_and_fail_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    unreadable_pgid = 61001
    gui_session._tracked_groups.append(
        browser_session.TrackedGroup("gui", unreadable_pgid, unreadable_pgid, "b:gui")
    )

    signalled_groups: list[tuple[int, int]] = []
    signalled_pids: list[tuple[int, int]] = []
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: True)
    monkeypatch.setattr(browser_session, "_alive", lambda pid: True)
    monkeypatch.setattr(browser_session, "process_birth_identity", lambda pid: None)
    monkeypatch.setattr(browser_session.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        browser_session,
        "_signal_group",
        lambda pgid, sig: signalled_groups.append((pgid, sig)),
    )
    monkeypatch.setattr(
        browser_session,
        "_signal_pid",
        lambda pid, sig: signalled_pids.append((pid, sig)),
    )

    assert f"unverified group {unreadable_pgid}" in gui_session.survivors()
    gui_session.stop()
    survivors_after = gui_session.force_kill_survivors(timeout=0.05)
    assert f"unverified group {unreadable_pgid}" in survivors_after
    assert signalled_groups == []
    assert signalled_pids == []

    forged_session = _make_session(tmp_path / "forged")
    node_pid = 62001
    forged_browser_pid = 62999

    def fake_forged_communicate(timeout: float | None = None) -> tuple[str, str]:
        (forged_session.root / "browser.pid").write_text(f"{forged_browser_pid}\n", encoding="utf-8")
        fake_node.returncode = 0
        return ('{"ok": true}\n', "")

    fake_node = _FakeProcess(node_pid, communicate_side_effect=fake_forged_communicate)
    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: fake_node)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: {node_pid: "b:node", forged_browser_pid: "b:forged"}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_safe_getpgid",
        lambda pid: {node_pid: node_pid, forged_browser_pid: forged_browser_pid}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_process_ppid",
        lambda pid: {forged_browser_pid: 1}.get(pid),
    )

    monkeypatch.setattr(
        browser_session,
        "_group_has_live_members",
        lambda pgid: pgid in {unreadable_pgid, forged_browser_pid},
    )
    forged_session.run_browser(tmp_path / "browser.mjs", {"orgIds": ["a", "b"]}, 30.0)
    assert forged_browser_pid not in forged_session.groups
    assert forged_session.unverified_identities
    assert any(str(forged_browser_pid) in item for item in forged_session.survivors())
    forged_session.stop()
    forged_session.force_kill_survivors(timeout=0.05)
    assert signalled_groups == []
    assert signalled_pids == []
    assert signalled_pids == []


def test_start_gui_rejects_unverified_frontend_pid_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    handoff = gui_session.root / "launch.url"
    handoff.write_text("http://127.0.0.1:43210/#grant=secret\n", encoding="utf-8")
    os.chmod(handoff, 0o600)

    gui_pid = 71001
    wrong_pgid_host_pid = 71999
    startup_line = json.dumps({
        "ok": True,
        "handoff_path": str(handoff),
        "url": "http://127.0.0.1:43210/",
        "port": 43210,
        "backend_driver": "daemon",
        "mode": "production",
        "frontend_pid": wrong_pgid_host_pid,
    }) + "\n"
    fake_gui = _FakeProcess(gui_pid, stdout_text=startup_line)
    signalled_pids: list[tuple[int, int]] = []

    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: fake_gui)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: {gui_pid: "b:gui", wrong_pgid_host_pid: "b:other"}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_safe_getpgid",
        lambda pid: {gui_pid: gui_pid, wrong_pgid_host_pid: 99999}.get(pid),
    )
    monkeypatch.setattr(browser_session, "_process_ppid", lambda pid: 1)
    monkeypatch.setattr(browser_session, "_alive", lambda pid: True)
    monkeypatch.setattr(
        browser_session,
        "_signal_pid",
        lambda pid, sig: signalled_pids.append((pid, sig)),
    )

    gui_session.start_gui()
    assert "host" not in gui_session.reported_pids
    assert gui_session.unverified_identities
    assert any(str(wrong_pgid_host_pid) in item for item in gui_session.survivors())
    gui_session.force_kill_survivors(timeout=0.05)
    assert signalled_pids == []


def test_start_gui_accepts_detached_frontend_pid_owned_by_gui_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    handoff = gui_session.root / "launch.url"
    handoff.write_text("http://127.0.0.1:43211/#grant=secret\n", encoding="utf-8")
    os.chmod(handoff, 0o600)

    gui_pid = 72001
    host_pid = 72002
    startup_line = json.dumps({
        "ok": True,
        "handoff_path": str(handoff),
        "url": "http://127.0.0.1:43211/",
        "port": 43211,
        "backend_driver": "daemon",
        "mode": "production",
        "frontend_pid": host_pid,
    }) + "\n"
    fake_gui = _FakeProcess(gui_pid, stdout_text=startup_line)

    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: fake_gui)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: {gui_pid: "b:gui", host_pid: "b:host"}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_safe_getpgid",
        lambda pid: {gui_pid: gui_pid, host_pid: host_pid}.get(pid),
    )
    monkeypatch.setattr(
        browser_session,
        "_process_ppid",
        lambda pid: {host_pid: gui_pid}.get(pid),
    )
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: False)
    monkeypatch.setattr(browser_session, "_alive", lambda pid: False)

    gui_session.start_gui()
    assert gui_session.reported_pids.get("host") == host_pid
    assert host_pid in gui_session.groups
    assert gui_session.unverified_identities == []
    assert gui_session.survivors() == []


def test_oversized_or_unparseable_reported_pids_fail_closed_without_overflow_or_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os_kill_calls: list[tuple[int, int]] = []
    os_killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        browser_session.os,
        "kill",
        lambda pid, sig: os_kill_calls.append((pid, sig)),
    )
    monkeypatch.setattr(
        browser_session.os,
        "killpg",
        lambda pgid, sig: os_killpg_calls.append((pgid, sig)),
    )

    frontend_session = _make_session(tmp_path / "frontend")
    frontend_session._record_frontend_pid(10**100)
    frontend_session._record_frontend_pid(2_147_483_648)
    frontend_session._record_frontend_pid(True)
    assert len(frontend_session.unverified_identities) == 3
    assert frontend_session.survivors() == frontend_session.unverified_identities
    assert "host" not in frontend_session.reported_pids
    frontend_session.stop()
    assert frontend_session.force_kill_survivors(timeout=0.05) == frontend_session.unverified_identities

    for idx, bad_text in enumerate(("1" * 5000, str(10**100))):
        browser_case = _make_session(tmp_path / f"browser-{idx}")
        node_pid = 81000 + idx

        def fake_communicate(
            timeout: float | None = None,
            *,
            target_root: Path = browser_case.root,
            content: str = bad_text,
        ) -> tuple[str, str]:
            (target_root / "browser.pid").write_text(f"{content}\n", encoding="utf-8")
            return ('{"ok": true}\n', "")

        fake_node = _FakeProcess(node_pid, returncode=0, communicate_side_effect=fake_communicate)
        monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, proc=fake_node, **kwargs: proc)
        monkeypatch.setattr(
            browser_session,
            "process_birth_identity",
            lambda pid, npid=node_pid: "b:node" if pid == npid else None,
        )
        monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: False)

        result = browser_case.run_browser(tmp_path / "browser.mjs", {"orgIds": ["a", "b"]}, 30.0)
        assert result == {"ok": True}
        assert "browser" not in browser_case.reported_pids
        assert browser_case.unverified_identities
        assert browser_case.survivors()
        browser_case.stop()
        assert browser_case.force_kill_survivors(timeout=0.05)

    assert os_kill_calls == []
    assert os_killpg_calls == []


def test_child_reported_dead_pid_without_observed_birth_fails_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    node_pid = 82001
    unobserved_browser_pid = 82999
    signalled_groups: list[tuple[int, int]] = []
    signalled_pids: list[tuple[int, int]] = []

    def fake_communicate(timeout: float | None = None) -> tuple[str, str]:
        (gui_session.root / "browser.pid").write_text(f"{unobserved_browser_pid}\n", encoding="utf-8")
        fake_node.returncode = 0
        return ('{"ok": true}\n', "")

    fake_node = _FakeProcess(node_pid, communicate_side_effect=fake_communicate)
    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: fake_node)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: "b:node" if pid == node_pid else None,
    )
    monkeypatch.setattr(browser_session, "_safe_getpgid", lambda pid: None)
    monkeypatch.setattr(browser_session, "_process_ppid", lambda pid: None)
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: False)
    monkeypatch.setattr(browser_session, "_alive", lambda pid: False)
    monkeypatch.setattr(
        browser_session,
        "_signal_group",
        lambda pgid, sig: signalled_groups.append((pgid, sig)),
    )
    monkeypatch.setattr(
        browser_session,
        "_signal_pid",
        lambda pid, sig: signalled_pids.append((pid, sig)),
    )

    result = gui_session.run_browser(tmp_path / "browser.mjs", {"orgIds": ["a", "b"]}, 30.0)
    assert result == {"ok": True}
    assert "browser" not in gui_session.reported_pids
    assert "browser" not in gui_session._tracked_pids
    assert gui_session.unverified_identities
    assert any(str(unobserved_browser_pid) in item for item in gui_session.survivors())
    gui_session.stop()
    assert any(str(unobserved_browser_pid) in item for item in gui_session.force_kill_survivors(timeout=0.05))
    assert signalled_groups == []
    assert signalled_pids == []


def test_safe_pid_repr_handles_oversized_int_and_unrepresentable_values() -> None:
    oversized = 10**5000
    rendered = browser_session._safe_pid_repr(oversized)
    assert rendered == f"<oversized-int bits={oversized.bit_length()}>"
    assert len(rendered) <= 80
    assert "100000" not in rendered

    class _Unrepresentable:
        def __repr__(self) -> str:
            raise ValueError("unrepresentable")

    assert browser_session._safe_pid_repr(_Unrepresentable()) == "<_Unrepresentable unrepresentable>"


def test_record_frontend_pid_handles_oversized_int_without_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os_kill_calls: list[tuple[int, int]] = []
    os_killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(browser_session.os, "kill", lambda pid, sig: os_kill_calls.append((pid, sig)))
    monkeypatch.setattr(browser_session.os, "killpg", lambda pgid, sig: os_killpg_calls.append((pgid, sig)))
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: False)

    gui_session = _make_session(tmp_path)
    oversized = 10**5000
    gui_session._record_frontend_pid(oversized)
    assert gui_session.unverified_identities == [
        f"invalid host pid <oversized-int bits={oversized.bit_length()}>"
    ]
    assert "host" not in gui_session.reported_pids
    assert gui_session.survivors() == gui_session.unverified_identities
    assert os_kill_calls == []
    assert os_killpg_calls == []


def test_start_gui_converts_oversized_json_integer_value_error_to_assertion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    handoff = gui_session.root / "launch.url"
    handoff.write_text("http://127.0.0.1:43212/#grant=secret\n", encoding="utf-8")
    os.chmod(handoff, 0o600)

    gui_pid = 73001
    huge_digits = "9" * 5000
    startup_line = (
        '{"ok": true, "handoff_path": '
        + json.dumps(str(handoff))
        + ', "url": "http://127.0.0.1:43212/", "port": 43212, '
        + '"backend_driver": "daemon", "mode": "production", '
        + f'"frontend_pid": {huge_digits}'
        + "}\n"
    )
    fake_gui = _FakeProcess(gui_pid, stdout_text=startup_line)
    os_kill_calls: list[tuple[int, int]] = []
    os_killpg_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: fake_gui)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: "b:gui" if pid == gui_pid else None,
    )
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: False)
    monkeypatch.setattr(browser_session.os, "kill", lambda pid, sig: os_kill_calls.append((pid, sig)))
    monkeypatch.setattr(browser_session.os, "killpg", lambda pgid, sig: os_killpg_calls.append((pgid, sig)))

    with pytest.raises(AssertionError, match="hyprial gui startup record could not be parsed"):
        gui_session.start_gui()

    assert gui_session.unverified_identities == [
        "unverified host pid: startup record integer exceeded string conversion limit"
    ]
    assert "host" not in gui_session.reported_pids
    assert gui_session.survivors() == gui_session.unverified_identities
    assert os_kill_calls == []
    assert os_killpg_calls == []


def test_missing_prerequisites_handles_oversized_json_integer_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_root = tmp_path / "gui"
    pkg = gui_root / "node_modules" / "playwright-core" / "package.json"
    pkg.parent.mkdir(parents=True)
    pkg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(browser_session, "GUI_ROOT", gui_root)
    monkeypatch.setattr(browser_session.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    oversized_json = '{"browser": ' + "1" * 5000 + "}\n"
    monkeypatch.setattr(
        browser_session.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=oversized_json, stderr=""),
    )
    missing = browser_session.missing_prerequisites()
    assert missing
    assert "no browser" in missing[0]


def test_run_browser_handles_oversized_json_integer_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gui_session = _make_session(tmp_path)
    node_pid = 83001
    oversized_json = '{"ok": ' + "1" * 5000 + "}\n"
    fake_node = _FakeProcess(
        node_pid,
        returncode=0,
        communicate_side_effect=lambda timeout=None: (oversized_json, ""),
    )
    monkeypatch.setattr(browser_session.subprocess, "Popen", lambda *args, **kwargs: fake_node)
    monkeypatch.setattr(
        browser_session,
        "process_birth_identity",
        lambda pid: "b:node" if pid == node_pid else None,
    )
    monkeypatch.setattr(browser_session, "_group_has_live_members", lambda pgid: False)

    with pytest.raises(AssertionError, match="browser script gave no JSON result"):
        gui_session.run_browser(tmp_path / "browser.mjs", {"orgIds": ["a", "b"]}, 30.0)
