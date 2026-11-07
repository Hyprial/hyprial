from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent-home-destroy.py"
SPEC = importlib.util.spec_from_file_location("agent_home_destroy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

NOW = datetime(2026, 10, 23, 4, 5, 6, tzinfo=timezone.utc)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agents" / "worker"
    (home / "config").mkdir(parents=True)
    (home / "config/agent.json").write_text("{}", encoding="utf-8")
    (home / "workspace/repo/.git").parent.mkdir(parents=True)
    (home / "workspace/repo/.git").write_text(
        "gitdir: /external/repo.git/worktrees/worker\n", encoding="utf-8"
    )
    (home / "workspace/.venv").mkdir()
    (home / "workspace/.venv/pyvenv.cfg").write_text(
        "home = /python\n", encoding="utf-8"
    )
    (home / "workspace/python").symlink_to(home / "workspace/.venv")
    (home / "persona/skills").mkdir(parents=True)
    (home / "sandbox/home").mkdir(parents=True)
    (home / "state").mkdir()
    (home / "state/home-receipt.json").write_text("{}", encoding="utf-8")
    (home / "state/cache").mkdir()
    (home / "state/cache/session.json").write_text("{}", encoding="utf-8")
    (home / "secrets/provider").mkdir(parents=True)
    (home / "secrets/provider/token").write_text("do-not-copy", encoding="utf-8")
    (home / "secrets/.env").write_text("TOKEN=do-not-copy", encoding="utf-8")
    (home / "secrets/.git").write_text(
        "gitdir: secret-material-is-not-a-worktree\n", encoding="utf-8"
    )
    return home


def _relative_tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_default_is_dry_run_with_named_moves_and_secret_deletions(tmp_path: Path):
    home = _home(tmp_path)
    before = _relative_tree(home)

    result = MODULE.destroy(home, who="user:operator", why="retired", now=NOW)

    assert result["dry_run"] is True
    assert result["target"] == str(
        tmp_path / "agents/destroyed/worker@2026-10-23T04:05:06Z"
    )
    assert result["subdirs_to_move"] == ["config", "persona", "sandbox", "workspace"]
    assert result["state_files_to_move"] == ["state/home-receipt.json"]
    assert result["state_discarded"] == ["state/cache", "state/cache/session.json"]
    assert result["secrets_to_delete"] == [
        "secrets/.env",
        "secrets/.git",
        "secrets/provider",
        "secrets/provider/token",
    ]
    assert result["receipt"]["who"] == "user:operator"
    assert result["receipt"]["when"] == "2026-10-23T04:05:06Z"
    assert result["receipt"]["why"] == "retired"
    assert result["receipt"]["what"]["secrets_deleted"] == result["secrets_to_delete"]
    assert result["receipt"]["external_refs"] == {
        "symlinks": 1,
        "total": 3,
        "venv": 1,
        "worktrees": 1,
    }
    assert _relative_tree(home) == before
    assert not (tmp_path / "agents/destroyed").exists()


def test_apply_moves_non_secrets_deletes_secrets_and_marks_complete(tmp_path: Path):
    home = _home(tmp_path)

    result = MODULE.destroy(home, who="user:operator", why="retired", now=NOW, yes=True)
    target = Path(result["target"])

    assert result["dry_run"] is False
    assert not home.exists()
    assert (target / "config/agent.json").is_file()
    assert (target / "workspace/repo/.git").is_file()
    assert (target / "state/home-receipt.json").is_file()
    assert not (target / "state/cache").exists()
    assert not (target / "secrets").exists()
    receipt = json.loads((target / "DESTROYED.json").read_text(encoding="utf-8"))
    assert receipt == result["receipt"]
    assert receipt["what"]["state_files_moved"] == ["state/home-receipt.json"]
    assert receipt["what"]["state_discarded"] == [
        "state/cache",
        "state/cache/session.json",
    ]
    assert receipt["what"]["secrets_deleted"] == [
        "secrets/.env",
        "secrets/.git",
        "secrets/provider",
        "secrets/provider/token",
    ]
    assert (target / "COMPLETE").read_text(encoding="utf-8") == "2026-10-23T04:05:06Z\n"


def test_apply_order_is_receipt_then_moves_then_secret_delete_then_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = _home(tmp_path)
    target = tmp_path / "agents/destroyed/worker@2026-10-23T04:05:06Z"
    events: list[str] = []
    original_move = MODULE._move_path
    original_delete = MODULE._delete_secrets
    original_marker = MODULE._write_completion_marker

    def move(source: Path, destination: Path) -> None:
        assert (target / "DESTROYED.json").is_file()
        assert (home / "secrets/provider/token").is_file()
        assert not (target / "COMPLETE").exists()
        events.append("move")
        original_move(source, destination)

    def delete(path: Path) -> None:
        assert (target / "config/agent.json").is_file()
        assert (target / "state/home-receipt.json").is_file()
        assert not (target / "COMPLETE").exists()
        events.append("delete-secrets")
        original_delete(path)

    def marker(path: Path, when: str) -> None:
        assert not (home / "secrets").exists()
        events.append("marker")
        original_marker(path, when)

    monkeypatch.setattr(MODULE, "_move_path", move)
    monkeypatch.setattr(MODULE, "_delete_secrets", delete)
    monkeypatch.setattr(MODULE, "_write_completion_marker", marker)

    MODULE.destroy(home, who="user:operator", why="retired", now=NOW, yes=True)

    assert events[-2:] == ["delete-secrets", "marker"]
    assert events[0] == "move"


def test_failed_move_leaves_secrets_and_has_no_completion_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = _home(tmp_path)
    target = tmp_path / "agents/destroyed/worker@2026-10-23T04:05:06Z"

    def fail_move(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected move failure")

    monkeypatch.setattr(MODULE, "_move_path", fail_move)
    with pytest.raises(MODULE.DestroyError, match="injected move failure"):
        MODULE.destroy(home, who="user:operator", why="retired", now=NOW, yes=True)

    assert (home / "secrets/provider/token").is_file()
    assert (target / "DESTROYED.json").is_file()
    assert not (target / "COMPLETE").exists()


def test_existing_target_is_rejected_without_mutation(tmp_path: Path):
    home = _home(tmp_path)
    target = tmp_path / "agents/destroyed/worker@2026-10-23T04:05:06Z"
    target.mkdir(parents=True)
    before = _relative_tree(home)

    with pytest.raises(MODULE.DestroyError, match="target already exists"):
        MODULE.destroy(home, who="user:operator", why="retired", now=NOW, yes=True)

    assert _relative_tree(home) == before


def test_secret_symlink_is_deleted_without_touching_its_target(tmp_path: Path):
    home = _home(tmp_path)
    outside = tmp_path / "outside-secret"
    outside.write_text("keep", encoding="utf-8")
    (home / "secrets/provider/link").symlink_to(outside)

    result = MODULE.destroy(home, who="user:operator", why="retired", now=NOW, yes=True)

    assert outside.read_text(encoding="utf-8") == "keep"
    assert "secrets/provider/link" in result["secrets_to_delete"]


def test_cli_emits_one_json_object_for_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    home = _home(tmp_path)

    assert MODULE.main([str(home), "--who", "user:operator", "--why", "retired"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["receipt"]["why"] == "retired"
