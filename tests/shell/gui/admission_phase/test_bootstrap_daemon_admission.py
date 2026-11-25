"""Z-6a: the launcher ensures the daemon in every state and admits by its ping phase.

The transport is a fake daemon answering like the wire bootstrap host (same
dispatcher, wire policy) or like a serving daemon.  What is pinned: the hook
runs for an ownerless home, a bootstrap daemon is admitted as ``bootstrap``
whatever the settings say, and an ownerless home with no hook is still probed:
a bootstrap answer admits it, no answer is ``GUI_DAEMON_UNAVAILABLE``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog
from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import (
    WIRE_POLICY,
    BootstrapDispatcher,
)
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.gui.backend import admission
from hyprial.shell.impl.gui.errors import GuiError

OWNER = "admission-owner"
NODE = "admission-node"


class FakeDaemon:
    def __init__(self, home: Path, *, mode: str | None = "bootstrap") -> None:
        self.home = home
        self.mode = mode
        self.requests: list[str] = []
        self.node = NODE
        self.socket = home / "state" / "daemon.sock"

    def start(self) -> None:
        self.requests.append("start")

    def __call__(self, method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.requests.append(method)
        if self.mode is None:
            raise ipc_errors.DaemonUnavailableError("not running")
        if self.mode == "bootstrap":
            runtime = SimpleNamespace(
                socket_path=self.socket,
                view=SimpleNamespace(record=SimpleNamespace(epoch="epoch-1")),
            )
            dispatcher = BootstrapDispatcher(app=None, runtime=runtime, context=None, policy=WIRE_POLICY)
            dispatcher._promotion_enabled = True
            if method == "ping":
                return {**dispatcher.ping(), "nodeId": self.node, "pid": 123}
            return dispatcher.catalog()
        if method == "ping":
            return {
                "running": True, "pid": 123, "version": __version__, "nodeId": self.node,
                "owner": OWNER, "phase": "serving", "restorePending": False,
                "epoch": "epoch-1", "socket": str(self.socket),
            }
        entries = daemon_catalog()["methods"]
        for entry in entries:
            entry.update(declared=True, state="available", callable=True, reason=None)
        return {
            "schemaVersion": 1, "catalogVersion": "1", "epoch": "epoch-1",
            "packageVersion": __version__, "methods": entries,
        }


def _home(tmp_path: Path, *, owner: str | None) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    settings: dict[str, Any] = {"version": 1}
    if owner is not None:
        settings["owner"] = owner
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def _environ(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home.parent / "user"),
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_NODE_ID": NODE,
        "HYPRIAL_GUI_BACKEND_DRIVER": "daemon",
    }


def _admit(home: Path, daemon: FakeDaemon, *, hook: bool = True, environ: dict[str, str] | None = None):
    return admission.admit_source(
        environ or _environ(home),
        request_once=daemon,
        ensure_daemon=daemon.start if hook else None,
        restore_wait=0.0,
        sleep=lambda seconds: None,
    )


def test_an_ownerless_home_runs_the_hook_once_and_is_admitted_as_bootstrap(tmp_path):
    home = _home(tmp_path, owner=None)
    daemon = FakeDaemon(home)
    result = _admit(home, daemon)
    assert result.phase == "bootstrap" and result.owner is None and result.principal is None
    assert daemon.requests.count("start") == 1
    assert daemon.requests[0] == "start"


def test_a_bootstrap_daemon_wins_over_an_owner_in_the_settings(tmp_path):
    home = _home(tmp_path, owner=OWNER)
    daemon = FakeDaemon(home)
    result = _admit(home, daemon)
    assert result.phase == "bootstrap" and result.owner is None and result.principal is None


def test_an_ownerless_home_whose_started_daemon_does_not_answer_is_unavailable(tmp_path):
    home = _home(tmp_path, owner=None)
    daemon = FakeDaemon(home, mode=None)
    with pytest.raises(GuiError) as refusal:
        _admit(home, daemon)
    assert refusal.value.code == "GUI_DAEMON_UNAVAILABLE"
    assert daemon.requests.count("start") == 1


def test_an_ownerless_home_without_a_hook_and_without_an_answer_is_unavailable(tmp_path):
    home = _home(tmp_path, owner=None)
    daemon = FakeDaemon(home, mode=None)
    with pytest.raises(GuiError) as refusal:
        _admit(home, daemon, hook=False)
    assert refusal.value.code == "GUI_DAEMON_UNAVAILABLE"
    assert daemon.requests == ["ping"]


def test_an_ownerless_home_without_a_hook_is_admitted_by_a_bootstrap_answer(tmp_path):
    home = _home(tmp_path, owner=None)
    daemon = FakeDaemon(home)
    result = _admit(home, daemon, hook=False)
    assert result.phase == "bootstrap" and result.owner is None and result.principal is None
    assert "start" not in daemon.requests
    assert daemon.requests[0] == "ping"


def test_a_daemon_that_serves_while_the_settings_were_ownerless_is_reread_once(tmp_path):
    home = _home(tmp_path, owner=None)
    daemon = FakeDaemon(home, mode="serving")
    with pytest.raises(GuiError) as refusal:
        _admit(home, daemon)
    assert refusal.value.code == "GUI_DAEMON_IDENTITY"
    assert refusal.value.data["mismatches"] == ["owner"]

    def commit() -> None:
        (home / "settings.json").write_text(json.dumps({"version": 1, "owner": OWNER}), encoding="utf-8")

    daemon.start = commit
    result = admission.admit_source(
        _environ(home), request_once=daemon, ensure_daemon=commit, restore_wait=0.0, sleep=lambda s: None
    )
    assert result.phase == "serving" and result.owner == OWNER


@pytest.mark.parametrize(
    ("mutate", "mismatch"),
    [("node", "node"), ("socket", "socket")],
)
def test_a_bootstrap_daemon_of_another_node_or_socket_is_refused(tmp_path, mutate, mismatch):
    home = _home(tmp_path, owner=None)
    daemon = FakeDaemon(home)
    if mutate == "node":
        daemon.node = "another-node"
    else:
        daemon.socket = tmp_path / "elsewhere" / "daemon.sock"
    with pytest.raises(GuiError) as refusal:
        _admit(home, daemon)
    assert refusal.value.code == "GUI_DAEMON_IDENTITY"
    assert mismatch in refusal.value.data["mismatches"]


def test_a_bootstrap_daemon_of_another_version_is_refused(tmp_path, monkeypatch):
    home = _home(tmp_path, owner=None)
    daemon = FakeDaemon(home)
    monkeypatch.setattr(admission, "__version__", "9.9.9")
    with pytest.raises(GuiError) as refusal:
        _admit(home, daemon)
    assert refusal.value.code == "GUI_DAEMON_IDENTITY"
    assert "version" in refusal.value.data["mismatches"]


def test_a_missing_home_is_refused_before_the_hook(tmp_path):
    home = tmp_path / "absent"
    daemon = FakeDaemon(home)
    with pytest.raises(GuiError) as refusal:
        admission.admit_source(_environ(home), request_once=daemon, ensure_daemon=daemon.start)
    assert refusal.value.code == "GUI_HOME_NOT_INITIALIZED"
    assert daemon.requests == []
