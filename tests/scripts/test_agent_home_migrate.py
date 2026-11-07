from __future__ import annotations

import errno
import os
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent-home-migrate.py"
SPEC = importlib.util.spec_from_file_location("agent_home_migrate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _legacy_home(tmp_path: Path) -> Path:
    home = tmp_path / "throwaway-agent"
    (home / "state/home").mkdir(parents=True)
    (home / "state/home/state.txt").write_text("state", encoding="utf-8")
    (home / "records").mkdir()
    (home / "records/note.md").write_text("note", encoding="utf-8")
    (home / "e2e").mkdir()
    (home / "e2e/run.json").write_text("{}", encoding="utf-8")
    return home


def test_default_is_dry_run_and_does_not_mutate(tmp_path: Path):
    home = _legacy_home(tmp_path)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    result = MODULE.migrate(home)

    assert result["dry_run"] is True
    assert result["changed"] is True
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_apply_moves_all_paths_and_second_run_is_unchanged(tmp_path: Path):
    home = _legacy_home(tmp_path)

    first = MODULE.migrate(home, yes=True)
    second = MODULE.migrate(home, yes=True)

    assert first["changed"] is True
    assert second["changed"] is False
    # The harness HOME stays put: it moves once, per organelle, with the cell mechanism.
    assert (home / "state/home/state.txt").read_text(encoding="utf-8") == "state"
    assert not (home / "sandbox").exists()
    assert (home / "workspace/notes/note.md").read_text(encoding="utf-8") == "note"
    assert (home / "workspace/e2e/run.json").read_text(encoding="utf-8") == "{}"
    assert (home / "persona").is_dir()
    assert (home / "persona/skills").is_dir()
    assert not (home / "records").exists()
    assert not (home / "e2e").exists()


def test_conflict_is_rejected_without_partial_mutation(tmp_path: Path):
    home = _legacy_home(tmp_path)
    (home / "workspace/notes").mkdir(parents=True)
    (home / "workspace/notes/existing.md").write_text("keep", encoding="utf-8")
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    with pytest.raises(MODULE.MigrationError):
        MODULE.migrate(home, yes=True)

    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_apply_rolls_back_when_a_later_operation_fails(tmp_path: Path, monkeypatch):
    home = _legacy_home(tmp_path)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
    original_mkdir_recorded = MODULE._mkdir_recorded
    calls = 0

    def fail_on_second_parent(path: Path, created: list[Path]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected destination failure")
        original_mkdir_recorded(path, created)

    monkeypatch.setattr(MODULE, "_mkdir_recorded", fail_on_second_parent)
    with pytest.raises(MODULE.MigrationError, match="rolled back"):
        MODULE.migrate(home, yes=True)

    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_cli_emits_json_for_dry_run(tmp_path: Path, capsys):
    home = _legacy_home(tmp_path)

    assert MODULE.main([str(home)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["changed"] is True


def _shape_a_home(tmp_path: Path) -> Path:
    """Shape A: the legacy records directory is top-level ``records``."""

    home = tmp_path / "shape-a"
    (home / "records").mkdir(parents=True)
    (home / "records/note.md").write_text("note-a", encoding="utf-8")
    return home


def _shape_b_home(tmp_path: Path) -> Path:
    """Shape B: the legacy records directory is ``workspace/records``."""

    home = tmp_path / "shape-b"
    (home / "workspace/records").mkdir(parents=True)
    (home / "workspace/records/note.md").write_text("note-b", encoding="utf-8")
    return home


def test_shape_a_records_moves_to_workspace_notes(tmp_path: Path):
    home = _shape_a_home(tmp_path)

    MODULE.migrate(home, yes=True)

    assert MODULE._kind(home / "records") == "absent"
    assert (home / "workspace/notes/note.md").read_text(encoding="utf-8") == "note-a"


def test_shape_b_workspace_records_moves_to_workspace_notes(tmp_path: Path):
    home = _shape_b_home(tmp_path)

    MODULE.migrate(home, yes=True)

    assert MODULE._kind(home / "workspace/records") == "absent"
    assert (home / "workspace/notes/note.md").read_text(encoding="utf-8") == "note-b"


def test_shape_b_moves_a_plain_git_directory_intact(tmp_path: Path):
    home = _shape_b_home(tmp_path)
    (home / "workspace/records/.git").mkdir()
    (home / "workspace/records/.git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    MODULE.migrate(home, yes=True)

    assert MODULE._kind(home / "workspace/records") == "absent"
    assert (home / "workspace/notes/.git/HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert (home / "workspace/notes/note.md").read_text(encoding="utf-8") == "note-b"


def test_both_shapes_present_is_ambiguous_and_names_both_paths(tmp_path: Path):
    home = _shape_a_home(tmp_path)
    (home / "workspace/records").mkdir(parents=True)
    (home / "workspace/records/note.md").write_text("note-b", encoding="utf-8")
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    with pytest.raises(MODULE.MigrationError) as caught:
        MODULE.migrate(home, yes=True)

    message = str(caught.value)
    assert "ambiguous" in message
    assert str(home / "records") in message
    assert str(home / "workspace/records") in message
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_registered_worktree_pointer_is_refused_and_named(tmp_path: Path):
    home = _shape_b_home(tmp_path)
    (home / "workspace/records/.git").write_text(
        "gitdir: /Users/someone/Workspace/repo/.git/modules/x/worktrees/records\n",
        encoding="utf-8",
    )
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    with pytest.raises(MODULE.MigrationError) as caught:
        MODULE.migrate(home, yes=True)

    message = str(caught.value)
    assert "registered worktree" in message
    assert str(home / "workspace/records") in message
    # Only the source path/category is public; pointer bytes may be secrets.
    assert "/worktrees/records" not in message
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before
    assert MODULE._kind(home / "workspace/notes") == "absent"


def test_result_reports_same_device_for_a_same_filesystem_home(tmp_path: Path):
    home = _shape_b_home(tmp_path)

    result = MODULE.migrate(home, yes=False)

    assert result["same_device"] is True


def test_cross_device_source_is_refused_and_names_both_sides(tmp_path: Path, monkeypatch):
    home = _shape_b_home(tmp_path)
    home_device = MODULE._device(home)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    real_device = MODULE._device

    def fake_device(path: Path) -> int:
        # Pretend the staging parent lives on another filesystem while the home
        # does not, which is what a home that is itself a mount point looks like.
        if Path(path) == home.absolute().parent:
            return home_device + 1
        return real_device(path)

    monkeypatch.setattr(MODULE, "_device", fake_device)
    with pytest.raises(MODULE.MigrationError) as caught:
        MODULE.migrate(home, yes=True)

    message = str(caught.value)
    assert "cross-device" in message
    assert str(home / "workspace/records") in message
    assert "os.rename cannot copy" in message
    # Prevention, not reaction: the refusal comes from the device check, so the
    # destination must never be created. Asserting only "it raised" would pass
    # equally for a refusal that happens after the first write.
    assert MODULE._kind(home / "workspace/notes") == "absent"
    assert MODULE._kind(home / "sandbox/home") == "absent"
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_rename_exdev_fails_by_name_and_rolls_back(tmp_path: Path, monkeypatch):
    """The guard can be fooled by a bind mount; the failure must still be clean."""

    home = _shape_b_home(tmp_path)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
    real_rename = MODULE.os.rename

    def exdev_rename(source, destination):
        raise OSError(errno.EXDEV, "Cross-device link", str(source))

    monkeypatch.setattr(MODULE.os, "rename", exdev_rename)
    with pytest.raises(MODULE.MigrationError) as caught:
        MODULE.migrate(home, yes=True)

    assert "rolled back" in str(caught.value)
    # strerror wording differs: macOS "Cross-device link", Linux "Invalid cross-device link".
    assert "cross-device" in str(caught.value).lower()
    monkeypatch.setattr(MODULE.os, "rename", real_rename)
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_cross_device_refuses_when_destination_parent_is_absent(tmp_path: Path, monkeypatch):
    """The device check must not depend on the destination existing.

    Shape A has no ``workspace/`` directory, so a check that stat'ed the
    destination parent would raise or be swallowed and the refusal would vanish.
    "unknown" has to land on the refusing side.
    """

    home = _shape_a_home(tmp_path)
    assert MODULE._kind(home / "workspace") == "absent"
    home_device = MODULE._device(home)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
    real_device = MODULE._device

    def fake_device(path: Path) -> int:
        if Path(path) == home.absolute().parent:
            return home_device + 1
        return real_device(path)

    monkeypatch.setattr(MODULE, "_device", fake_device)
    with pytest.raises(MODULE.MigrationError, match="cross-device"):
        MODULE.migrate(home, yes=True)

    assert MODULE._kind(home / "workspace") == "absent"
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_device_lookup_uses_nearest_existing_ancestor(tmp_path: Path):
    home = _shape_a_home(tmp_path)

    missing = home / "does" / "not" / "exist"

    assert MODULE._device(missing) == MODULE._device(home)


def test_unreadable_device_refuses_instead_of_skipping(tmp_path: Path, monkeypatch):
    """An undetermined device is not evidence that both sides match."""

    home = _shape_b_home(tmp_path)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
    real_stat = MODULE.os.stat

    def unreadable(target, *args, **kwargs):
        # Only real stat() calls (follow_symlinks not False); pathlib.lstat() must
        # keep working, otherwise _kind() would refuse before the device check.
        if Path(target) == home / "workspace/records" and kwargs.get("follow_symlinks", True) is not False:
            raise PermissionError(13, "Permission denied", str(target))
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(MODULE.os, "stat", unreadable)
    with pytest.raises(MODULE.MigrationError) as caught:
        MODULE.migrate(home, yes=True)

    assert "cannot determine the device" in str(caught.value)
    assert str(home / "workspace/records") in str(caught.value)
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_same_device_is_none_when_undetermined_and_never_folded_to_true(tmp_path: Path, monkeypatch):
    home = _shape_b_home(tmp_path)
    monkeypatch.setattr(MODULE, "_device", lambda path: None)

    # Without a device the plan must refuse before the result is even built, so
    # the None branch is exercised through migrate() and through _device itself.
    assert MODULE._device(home) is None
    with pytest.raises(MODULE.MigrationError, match="cannot determine the device"):
        MODULE.migrate(home, yes=False)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
)
def test_mode_000_parent_makes_the_source_unreadable_and_refuses(tmp_path: Path):
    """A real EACCES, not an injected one: refusal, never a traceback."""

    home = _shape_b_home(tmp_path)
    workspace = home / "workspace"
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
    workspace.chmod(0o000)
    try:
        # The device lookup must answer None (unknown), never an ancestor's device.
        assert MODULE._device(workspace / "records") is None
        with pytest.raises(MODULE.MigrationError) as caught:
            MODULE.migrate(home, yes=True)
    finally:
        workspace.chmod(0o755)

    message = str(caught.value)
    assert "cannot inspect" in message or "cannot determine the device" in message
    # The first unreadable path encountered is named; under a 0o000 parent that is
    # whichever MOVES entry is inspected first, so pin the shape not the leaf.
    assert str(home) in message
    assert "workspace" in message
    after = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
    assert after == before
