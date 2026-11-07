from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent-home-inventory.py"
SPEC = importlib.util.spec_from_file_location("agent_home_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_inventory_reports_target_and_legacy_structure_without_writes(tmp_path: Path):
    home = tmp_path / "legacy-agent"
    for relative in ("state/home", "workspace", "persona/skills", "records", "e2e"):
        (home / relative).mkdir(parents=True)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    result = MODULE.inventory(home)

    assert result["agent"] == "legacy-agent"
    assert result["sandbox"] == str(home / "sandbox")
    assert result["notes_dir"] == str(home / "workspace/notes")
    assert result["persona_dir"] == str(home / "persona")
    assert result["skills_dir"] == str(home / "persona/skills")
    assert result["present"]["state"] is True
    assert result["legacy"] == {"records": True, "e2e_at_top_level": True}
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_inventory_counts_git_worktree_venv_and_symlink(tmp_path: Path):
    home = tmp_path / "agent"
    (home / "workspace/repo/.git").parent.mkdir(parents=True)
    (home / "workspace/repo/.git").write_text(
        "gitdir: /outside/worktrees/repo\n", encoding="utf-8"
    )
    (home / "workspace/.venv").mkdir(parents=True)
    (home / "workspace/.venv/pyvenv.cfg").write_text("home = /python\n", encoding="utf-8")
    (home / "workspace/link").symlink_to(home / "workspace/.venv", target_is_directory=True)

    result = MODULE.inventory(home)

    assert result["external_refs"] == {"worktrees": 1, "venv": 1, "symlinks": 1}


def test_script_emits_single_object_and_multi_home_envelope(tmp_path: Path, capsys):
    first = tmp_path / "one"
    second = tmp_path / "two"
    MODULE.main([str(first)])
    single = json.loads(capsys.readouterr().out)
    assert single["agent"] == "one"
    MODULE.main([str(first), str(second)])
    multiple = json.loads(capsys.readouterr().out)
    assert [row["agent"] for row in multiple["homes"]] == ["one", "two"]
