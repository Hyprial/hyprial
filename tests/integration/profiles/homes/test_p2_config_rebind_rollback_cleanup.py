"""Rollback cleanup must be exact: provenance, preserved parents, honest failure.

Reviewer finding B2: rollback removed ``config-revisions`` whenever it was
empty without recording whether this transaction created it, and swallowed
every ``OSError`` -- so a pre-existing directory was deleted and a failing
``rmdir`` was still reported as ``rolled-back``.
"""

from __future__ import annotations

import errno
import json
import stat
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError

from .config_rebind_support import (
    close_application,
    new_application,
    private_tree,
    receipt_phases,
)


def _home(app: DaemonApplication, actor: str) -> Path:
    return Path(app._agent_registry.home_receipt(actor).path)


def _receipt(home: Path, receipt_id: str) -> dict[str, object]:
    return json.loads(
        (home / "state" / "config-rebind" / receipt_id / "receipt.json").read_text(
            encoding="utf-8"
        )
    )


def _rebind_and_roll_back(app: DaemonApplication, tmp_path: Path, name: str) -> None:
    source = private_tree(
        tmp_path / f"{name}-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    rebound = app.handle("agent.config.rebind", {"name": name, "source": str(source)})
    rolled = app.handle(
        "agent.config.rebind",
        {"name": name, "rollbackReceiptId": rebound["receiptId"]},
    )
    assert rolled["state"] == "rolled-back"


def test_rollback_preserves_a_pre_existing_empty_revision_parent(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "preexisting", "preferredHarness": "pi"})
    home = _home(app, "preexisting")
    revisions = home / "config-revisions"
    revisions.mkdir(mode=0o700)
    revisions.chmod(0o700)

    _rebind_and_roll_back(app, tmp_path, "preexisting")

    assert revisions.is_dir()
    assert stat.S_IMODE(revisions.stat().st_mode) == 0o700
    assert list(revisions.iterdir()) == []


def test_rollback_removes_the_revision_parent_it_created(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "created", "preferredHarness": "pi"})
    home = _home(app, "created")
    revisions = home / "config-revisions"
    assert not revisions.exists()

    _rebind_and_roll_back(app, tmp_path, "created")

    assert not revisions.exists()


def test_rollback_keeps_an_empty_revision_parent_after_a_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "preexisting-restart"
    first = new_application(root)
    try:
        first.handle("agent.create", {"name": "kept-parent", "preferredHarness": "pi"})
        home = _home(first, "kept-parent")
        revisions = home / "config-revisions"
        revisions.mkdir(mode=0o700)
        revisions.chmod(0o700)
        source = private_tree(
            tmp_path / "kept-parent-source", {"native/pi/AGENTS.md": b"replacement\n"}
        )
        rebound = first.handle(
            "agent.config.rebind", {"name": "kept-parent", "source": str(source)}
        )
    finally:
        close_application(first)

    second = new_application(root)
    try:
        rolled = second.handle(
            "agent.config.rebind",
            {"name": "kept-parent", "rollbackReceiptId": rebound["receiptId"]},
        )
        assert rolled["state"] == "rolled-back"
        assert revisions.is_dir()
        assert stat.S_IMODE(revisions.stat().st_mode) == 0o700
    finally:
        close_application(second)


def test_rollback_propagates_a_failed_revision_parent_cleanup(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "denied", "preferredHarness": "pi"})
    before = app.agents.require("denied").config
    source = private_tree(
        tmp_path / "denied-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    rebound = app.handle("agent.config.rebind", {"name": "denied", "source": str(source)})
    home = _home(app, "denied")
    revisions = home / "config-revisions"
    assert revisions.is_dir()
    real_rmdir = Path.rmdir
    denied: list[Path] = []

    def refusing_rmdir(self: Path) -> None:
        if self == revisions:
            denied.append(self)
            raise PermissionError(errno.EACCES, "permission denied", str(self))
        real_rmdir(self)

    rollback = {"name": "denied", "rollbackReceiptId": rebound["receiptId"]}
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(Path, "rmdir", refusing_rmdir)
        with pytest.raises(DaemonRequestError) as refused:
            app.handle("agent.config.rebind", rollback)

    assert denied == [revisions]
    assert refused.value.code == "AGENT_CONFIG_REBIND_CLEANUP_FAILED"
    assert revisions.is_dir()
    phases = receipt_phases(app, "denied")
    assert phases[str(rebound["receiptId"])] == "binding-restored"
    assert app.agents.require("denied").config == before
    assert not Path(str(rebound["target"])).exists()

    resumed = app.handle("agent.config.rebind", rollback)
    assert resumed["state"] == "rolled-back"
    assert not revisions.exists()
    assert receipt_phases(app, "denied")[str(rebound["receiptId"])] == "rolled-back"


def test_failed_cleanup_survives_a_restart_and_resumes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "denied-restart"
    source = private_tree(
        tmp_path / "denied-restart-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    first = new_application(root)
    try:
        first.handle("agent.create", {"name": "resumed", "preferredHarness": "pi"})
        before = first.agents.require("resumed").config
        rebound = first.handle(
            "agent.config.rebind", {"name": "resumed", "source": str(source)}
        )
        home = _home(first, "resumed")
        revisions = home / "config-revisions"
        real_rmdir = Path.rmdir

        def refusing_rmdir(self: Path) -> None:
            if self == revisions:
                raise PermissionError(errno.EACCES, "permission denied", str(self))
            real_rmdir(self)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(Path, "rmdir", refusing_rmdir)
            with pytest.raises(DaemonRequestError) as refused:
                first.handle(
                    "agent.config.rebind",
                    {"name": "resumed", "rollbackReceiptId": rebound["receiptId"]},
                )
        assert refused.value.code == "AGENT_CONFIG_REBIND_CLEANUP_FAILED"
        assert _receipt(home, str(rebound["receiptId"]))["phase"] == "binding-restored"
    finally:
        close_application(first)

    second = new_application(root)
    try:
        resumed = second.handle(
            "agent.config.rebind",
            {"name": "resumed", "rollbackReceiptId": rebound["receiptId"]},
        )
        assert resumed["state"] == "rolled-back"
        assert second.agents.require("resumed").config == before
        assert not revisions.exists()
    finally:
        close_application(second)


def test_rollback_ignores_a_non_empty_revision_parent(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "shared-parent", "preferredHarness": "pi"})
    source = private_tree(
        tmp_path / "shared-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    rebound = app.handle(
        "agent.config.rebind", {"name": "shared-parent", "source": str(source)}
    )
    home = _home(app, "shared-parent")
    revisions = home / "config-revisions"
    assert _receipt(home, str(rebound["receiptId"]))["revisionParentCreated"] is True
    # A revision directory this transaction did not create (a stale crash
    # survivor) keeps the parent non-empty; cleanup must not fail on it.
    stranger = revisions / "0123456789abcdef0123456789abcdef"
    stranger.mkdir(mode=0o700)
    stranger.chmod(0o700)

    rolled = app.handle(
        "agent.config.rebind",
        {"name": "shared-parent", "rollbackReceiptId": rebound["receiptId"]},
    )

    assert rolled["state"] == "rolled-back"
    assert revisions.is_dir()
    assert sorted(path.name for path in revisions.iterdir()) == [stranger.name]


def _interrupt_after_parent_creation(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """The review's repro: lose the process right after the parent is created."""

    import hyprial.identity.impl.agents.config_rebind.service as rebind_service

    original = rebind_service._validate_revision_parent
    state = {"interrupted": False}

    def interrupt(parent: Path, *, create: bool) -> bool:
        created = original(parent, create=create)
        if created and not state["interrupted"]:
            state["interrupted"] = True
            raise RuntimeError("simulated loss after the revision parent was created")
        return created

    monkeypatch.setattr(rebind_service, "_validate_revision_parent", interrupt)
    return state


def test_interrupted_revision_parent_creation_keeps_its_ownership(
    application: DaemonApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application
    app.handle("agent.create", {"name": "claimed", "preferredHarness": "pi"})
    home = _home(app, "claimed")
    revisions = home / "config-revisions"
    source = private_tree(
        tmp_path / "claimed-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    state = _interrupt_after_parent_creation(monkeypatch)

    with pytest.raises(
        RuntimeError, match="simulated loss after the revision parent was created"
    ):
        app.handle("agent.config.rebind", {"name": "claimed", "source": str(source)})
    assert state["interrupted"] is True
    assert revisions.is_dir()
    transaction = next((home / "state" / "config-rebind").iterdir())
    recorded = json.loads((transaction / "receipt.json").read_text()).get(
        "revisionParentCreated"
    )

    rolled = app.handle(
        "agent.config.rebind",
        {"name": "claimed", "rollbackReceiptId": transaction.name},
    )
    assert rolled["state"] == "rolled-back"
    assert not revisions.exists()
    assert recorded is True


def test_interrupted_revision_parent_creation_keeps_its_ownership_across_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "claimed-restart"
    source = private_tree(
        tmp_path / "claimed-restart-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )
    first = new_application(root)
    try:
        first.handle("agent.create", {"name": "claimed", "preferredHarness": "pi"})
        home = _home(first, "claimed")
        revisions = home / "config-revisions"
        state = _interrupt_after_parent_creation(monkeypatch)
        with pytest.raises(
            RuntimeError, match="simulated loss after the revision parent was created"
        ):
            first.handle(
                "agent.config.rebind", {"name": "claimed", "source": str(source)}
            )
        assert state["interrupted"] is True
        assert revisions.is_dir()
        transaction = next((home / "state" / "config-rebind").iterdir())
        receipt_id = transaction.name
    finally:
        close_application(first)

    second = new_application(root)
    try:
        rolled = second.handle(
            "agent.config.rebind",
            {"name": "claimed", "rollbackReceiptId": receipt_id},
        )
        assert rolled["state"] == "rolled-back"
        assert not revisions.exists()
    finally:
        close_application(second)
