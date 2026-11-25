"""The troubleshooting doc's checkable claims, checked.

``contract/e2e/README.md`` tells whoever is staring at a red
scenario where each harness writes its own session log.  Most of that is
machine state and cannot be asserted from here -- a CI container has no
``~/.codex``, and a test that demanded one would fail for being right.

One claim is not machine state.  The doc says the controlled gate moves
Claude's config directory to ``~/.claude-ci``, so a CI-run scenario's log is
*not* under ``~/.claude``.  That fact lives in the workflow, in this
repository, and it is the one most likely to quietly stop being true --
someone edits the workflow, the doc keeps saying what it always said, and the
next person searches a directory full of the developer's own sessions.  Not
empty, not relevant, and not obviously either.

So the doc and the workflow are pinned to each other here.  Documentation
that can be wrong out loud is worth more than documentation nobody can check.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
README = ROOT / "contract" / "e2e" / "README.md"
WORKFLOW = ROOT / ".forgejo" / "workflows" / "e2e-macos-credentialed.yml"


def test_the_readme_names_the_config_dir_the_controlled_gate_actually_pins() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^\s*CLAUDE_CONFIG_DIR:\s*(\S+)\s*$", workflow, re.M)
    assert match, (
        "e2e-macos-credentialed.yml no longer sets CLAUDE_CONFIG_DIR at job level; the "
        "README's troubleshooting section is written around it and needs "
        "updating with whatever replaced it"
    )
    # Compare on the directory's name, not the absolute path.  The workflow
    # spells it out under a specific user's home; the README writes it
    # `~/.claude-ci`, which is the right thing for a document read on other
    # machines.  Asserting the full string would have demanded the doc be
    # wrong for its readers -- which is how it failed on this test's first
    # run, before any mutation.
    configured = Path(match.group(1)).name

    readme = README.read_text(encoding="utf-8")
    assert configured in readme, (
        f"the controlled gate pins CLAUDE_CONFIG_DIR to a directory named "
        f"{configured!r}, but the "
        "E2E README's 'read the harness's own session log' section does not "
        "mention it. Someone debugging a CI-run scenario will look under the "
        "default ~/.claude, find the developer's unrelated sessions, and have "
        "no way to tell that is the wrong directory."
    )
