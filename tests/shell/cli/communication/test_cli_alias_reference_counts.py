"""Static Phase 0 impact counts for command spellings that remain on dev."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
COUNTER = ROOT / "scripts" / "archived" / "count_cli_alias_references.py"


def test_counter_classifies_tracked_skill_prompt_and_docs_references(
    tmp_path: Path,
) -> None:
    scan_root = tmp_path / "scan"
    (scan_root / "docs").mkdir(parents=True)
    (scan_root / "prompts").mkdir()
    (scan_root / "skills" / "demo").mkdir(parents=True)
    (scan_root / "docs" / "commands.md").write_text(
        "Use `hyprial service`. Legacy auth: `lark-auth check|complete`. "
        "Already absent from dev: `hyprial channel list`.\n",
        encoding="utf-8",
    )
    (scan_root / "prompts" / "agent-prompt.md").write_text(
        "Diagnose with hyprial trajectory <message-id>.\n", encoding="utf-8"
    )
    (scan_root / "skills" / "demo" / "SKILL.md").write_text(
        "Run `python -m hyprial.daemon.impl.mcp.entry agent-channel` for the resident worker.\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", scan_root], check=True)
    subprocess.run(["git", "-C", scan_root, "add", "."], check=True)

    result = subprocess.run(
        [sys.executable, str(COUNTER), "--root", str(scan_root), "--json"],
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    rows = {row["command"]: row for row in payload["aliases"]}
    assert "service" not in rows
    # R9 retired the top-level lark-auth command (now `adapter user-auth`), so
    # the counter reports it as absent instead of counting references -- same
    # shape as the `channel list` precedent above.
    assert "lark-auth check" not in rows
    assert "lark-auth complete" not in rows
    assert "lark-auth check" in payload["notRegistered"]
    assert "lark-auth complete" in payload["notRegistered"]
    assert "trajectory" not in rows
    # R2 moved the message observation surface under `message`, so the counter
    # reports the retired top-level spellings as absent instead of counting them.
    assert "trajectory" in payload["notRegistered"]
    assert "outbox list" in payload["notRegistered"]
    assert "outbox prune" in payload["notRegistered"]
    # The no-alias ruling retired the old mcp spelling outright.
    assert "mcp agent-channel" not in rows
    assert "mcp agent-channel" in payload["notRegistered"]
    assert "channel list" not in rows
    assert "channel list" in payload["notRegistered"]
