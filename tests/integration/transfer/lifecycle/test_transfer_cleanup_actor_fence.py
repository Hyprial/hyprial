"""Pure plan controls: only private pytest temporary directories are examined."""
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.cleanup import CleanupFenceError, plan_cleanup
from hyprial.daemon.impl.transfer.execution.container import staging_dir


@pytest.fixture
def layout(tmp_path, monkeypatch):
    operator = tmp_path / "operator"
    operator.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator))
    state = tmp_path / "active-state"
    configured_home = tmp_path / "configured-home"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_HOME", str(configured_home))
    home = tmp_path / "source"
    home.mkdir()
    own = staging_dir(state, "alice")
    other = staging_dir(state, "bob")
    own.mkdir(parents=True)
    other.mkdir()
    (other / "keep.txt").write_text("private fixture sentinel")
    return home, state, own, other, configured_home, operator


@pytest.mark.parametrize("kind", ["staging", "credential_volumes", "private_grants"])
@pytest.mark.parametrize("which", ["shared-parent", "other-actor", "inactive-home-state", "inactive-default-state"])
def test_default_plan_rejects_non_actor_roots(layout, kind, which):
    home, state, own, other, configured_home, operator = layout
    target = {
        "shared-parent": own.parent,
        "other-actor": other,
        "inactive-home-state": staging_dir(configured_home / "state", "alice"),
        "inactive-default-state": staging_dir(operator / ".hyprial" / "state", "alice"),
    }[which]
    target.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CleanupFenceError, match="not this actor's"):
        plan_cleanup(home, actor="alice", owner="fixture-owner", **{kind: [target]})
    assert (other / "keep.txt").read_text() == "private fixture sentinel"


@pytest.mark.parametrize("kind", ["staging", "credential_volumes", "private_grants"])
def test_default_plan_accepts_actual_actor_staging(layout, kind):
    home, state, own, other, configured_home, operator = layout
    plan = plan_cleanup(home, actor="alice", owner="fixture-owner", **{kind: [own]})
    assert any(item.path == own for item in plan.items)


def test_explicit_release_notes_foreign_actor_root(layout):
    home, state, own, other, configured_home, operator = layout
    plan = plan_cleanup(home, actor="alice", owner="fixture-owner", staging=[other], allow_external=True)
    assert "--allow-external" in next(item.note for item in plan.items if item.path == other)


@pytest.mark.parametrize("shape", ["dotdot", "symlink-ancestor"])
def test_default_plan_checks_resolved_actor_path(layout, shape):
    home, state, own, other, configured_home, operator = layout
    if shape == "dotdot":
        target = own / ".." / other.name
    else:
        (own / "link").symlink_to(other, target_is_directory=True)
        target = own / "link" / "keep.txt"
    with pytest.raises(CleanupFenceError, match="not this actor's"):
        plan_cleanup(home, actor="alice", owner="fixture-owner", staging=[target])
    assert (other / "keep.txt").read_text() == "private fixture sentinel"


@pytest.mark.parametrize("source", ["explicit-state", "configured-home", "default-home"])
@pytest.mark.parametrize("nested", [False, True])
def test_selected_state_and_actor_descendants_are_accepted(layout, monkeypatch, source, nested):
    home, state, own, other, configured_home, operator = layout
    if source != "explicit-state":
        monkeypatch.delenv("HARNESS_STATE_DIR")
        state = configured_home / "state"
    if source == "default-home":
        monkeypatch.delenv("HYPRIAL_HOME")
        state = operator / ".hyprial" / "state"
    target = staging_dir(state, "alice")
    if nested:
        target = target / "credentials"
    target.mkdir(parents=True, exist_ok=True)
    plan = plan_cleanup(home, actor="alice", owner="fixture-owner", staging=[target])
    assert any(item.path == target for item in plan.items)


@pytest.mark.parametrize("protected", ["agent-home", "inbox", "INBOX", "operator-home"])
def test_explicit_release_keeps_home_and_inbox_protection(layout, protected):
    home, state, own, other, configured_home, operator = layout
    target = {"agent-home": home, "operator-home": operator}.get(protected, own / protected)
    target.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CleanupFenceError):
        plan_cleanup(home, actor="alice", owner="fixture-owner", staging=[target], allow_external=True)


def test_existing_distinct_objects_do_not_match_by_casefold(monkeypatch):
    from hyprial.daemon.impl.transfer import cleanup

    root = Path("/fixture-owned/root")
    other = Path("/fixture-owned/ROOT")
    identities = {root: (1, 10), other: (1, 20)}
    monkeypatch.setattr(cleanup, "_identity", identities.get)
    assert not cleanup._contains(root, other)


def test_actor_root_symlink_does_not_claim_another_actors_descendant(layout):
    home, state, own, other, configured_home, operator = layout
    alias = staging_dir(state, "charlie")
    alias.symlink_to(other, target_is_directory=True)
    with pytest.raises(CleanupFenceError, match="not this actor's"):
        plan_cleanup(home, actor="charlie", owner="fixture-owner", staging=[alias / "keep.txt"])
    assert (other / "keep.txt").read_text() == "private fixture sentinel"


@pytest.mark.parametrize("kind", ["permission", "io-error"])
def test_identity_lookup_errors_are_not_missing_objects(layout, monkeypatch, kind):
    import errno
    import os

    from hyprial.daemon.impl.transfer import cleanup

    home, state, own, other, configured_home, operator = layout
    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == own:
            if kind == "permission":
                raise PermissionError(errno.EACCES, "fixture identity unavailable")
            raise OSError(errno.EIO, "fixture identity unavailable")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(cleanup.os, "stat", failing_stat)
    with pytest.raises(CleanupFenceError, match="verify identity"):
        plan_cleanup(home, actor="alice", owner="fixture-owner", staging=[own])
