"""AT11 leave-half: destroy clears a gone home, refuses an unreadable one.

Split verbatim out of ``test_transfer_daemon.py`` so the merged tree stays
inside the structural line budget (#1446's destroy tests met #1469's TUI
tests in the same module); the ``make_app`` fixture and ``_seed_worker``
helper are imported from the sibling module.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from tests.integration.transfer.lifecycle.test_transfer_daemon import (  # noqa: F401 - fixtures/helpers
    SOURCE,
    _seed_worker,
    make_app,
)


# ---------------------------------------------------------------------------
# AT11 leave-half: the source row a completed transfer-out leaves behind
# ---------------------------------------------------------------------------


def test_destroy_finishes_a_transfer_out_whose_home_is_already_gone(make_app) -> None:  # noqa: F811
    """A completed transfer-out deletes the home; destroy must clear the row.

    ``transfer-bundle-complete`` removes the source home by identity fence and
    deliberately leaves the registration to the identity side.  Reading the
    workspace summary first made that handover impossible: the home was gone,
    the fence raised, and the row -- with its routes and undelivered messages --
    stayed behind, still advertised as a delivery target.
    """

    app = make_app(SOURCE)
    _seed_worker(app)

    home = app.hyprial_home / "agents" / "wk"
    assert home.is_dir()
    shutil.rmtree(home)

    result = app.handle("agent.destroy", {"name": "wk"})

    assert result["ok"] is True
    assert result["destroyed"] is True
    assert result["workspace"]["exists"] is False
    assert result["workspace"]["absent"] is True
    assert app.agents.get("wk") is None


def test_home_is_absent_only_trusts_an_explicit_enoent(tmp_path, monkeypatch) -> None:
    """Absence needs a named ENOENT; any other lstat failure still refuses.

    ``safe_directory`` funnels every ``lstat`` failure into the ``io``
    category, so this confirmation is what separates "the home is gone" from
    "the home cannot be read": ENOENT answers True, while ENOTDIR and EACCES
    answer False so the caller re-raises the fence failure instead of
    clearing the row.
    """

    from hyprial.daemon.impl.application.actors.agents_admin.destroys import (
        _AgentDestroyMixin,
    )
    from hyprial.identity import AgentHomeError

    error = AgentHomeError("io", "wk", "validate-agent-root")

    # A path that really is not there answers True.
    missing = tmp_path / "agents" / "wk"
    assert _AgentDestroyMixin._home_is_absent(error, missing) is True

    # A path whose parent is a regular file answers False: lstat fails ENOTDIR.
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("not a directory")
    assert _AgentDestroyMixin._home_is_absent(error, not_a_dir / "wk") is False

    # A read that fails EACCES answers False.  The patch lands on the exact
    # name this helper calls -- ``os.lstat`` -- and the hit count proves the
    # arm reached it.  (The 3.12 CI red came from an arm that patched a name
    # the *fence* never calls: ``Path.lstat`` keeps its own accessor, so a
    # patch on ``os.lstat`` alone cannot make the fence fail.)
    home = tmp_path / "agents" / "wk"
    real_lstat = os.lstat
    hits = 0

    def denied(path, *args, **kwargs):
        nonlocal hits
        if str(path) == str(home):
            hits += 1
            raise PermissionError(13, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    assert _AgentDestroyMixin._home_is_absent(error, home) is False
    assert hits == 1


def test_destroy_still_refuses_a_home_it_cannot_read(make_app, monkeypatch) -> None:  # noqa: F811
    """EACCES is not absence: destroy must refuse and keep the row.

    The two layers read the path through different names -- the fence calls
    ``Path.lstat`` while the absence confirmation (and the old
    ``os.path.lexists``) calls ``os.lstat`` -- so the denial is installed on
    both and each hit is counted: an arm that patches a name the code never
    reaches would go green on a broken tree.  This is also why a real
    ``chmod 000`` arm is not used here: under a root test runner it silently
    stops denying anything.
    """

    app = make_app(SOURCE)
    _seed_worker(app)

    home = app.hyprial_home / "agents" / "wk"
    real_path_lstat = Path.lstat
    real_lstat = os.lstat
    hits = {"path": 0, "os": 0}

    def denied_path_lstat(self, *args, **kwargs):
        if str(self) == str(home):
            hits["path"] += 1
            raise PermissionError(13, "Permission denied")
        return real_path_lstat(self, *args, **kwargs)

    def denied_lstat(path, *args, **kwargs):
        if str(path) == str(home):
            hits["os"] += 1
            raise PermissionError(13, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied_path_lstat)
    monkeypatch.setattr(os, "lstat", denied_lstat)

    with pytest.raises(DaemonRequestError):
        app.handle("agent.destroy", {"name": "wk"})

    assert hits["path"] >= 1
    assert app.agents.get("wk") is not None


def test_destroy_still_refuses_a_home_that_is_not_absent(make_app) -> None:  # noqa: F811
    """Tolerating a missing home must not tolerate a tampered one."""

    app = make_app(SOURCE)
    _seed_worker(app)

    home = app.hyprial_home / "agents" / "wk"
    shutil.rmtree(home)
    elsewhere = home.parent / "wk-elsewhere"
    elsewhere.mkdir(mode=0o700)
    home.symlink_to(elsewhere)

    try:
        with pytest.raises(DaemonRequestError):
            app.handle("agent.destroy", {"name": "wk"})
        assert app.agents.get("wk") is not None
    finally:
        home.unlink()
