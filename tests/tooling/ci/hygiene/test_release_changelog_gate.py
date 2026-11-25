"""Repros for scripts/ci/check_release_changelog.py — the release/<version> changelog gate.

Every repository here is a fresh temporary repository under pytest's ``tmp_path``; nothing reads
the developer's repository, HOME configuration or the network. Each negative test shows the RED
state; the positive test shows the GREEN state for the same shape of change.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_release_changelog.py"
SPEC = importlib.util.spec_from_file_location("check_release_changelog", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

BASE_CHANGELOG = """# Changelog

## [Unreleased]

### Changed
- something on dev

## [0.5.5.1] - 2026-10-28

### Fixed
- an older fix
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, text=True, capture_output=True,
    ).stdout


def commit(repo: Path, message: str, changelog: str) -> str:
    (repo / "CHANGELOG.md").write_text(changelog)
    git(repo, "add", "CHANGELOG.md")
    git(repo, "-c", "user.email=ci@test.invalid", "-c", "user.name=ci",
        "commit", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "ci@test.invalid")
    git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "README.md").write_text("fixture\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path, commit(tmp_path, "release baseline", BASE_CHANGELOG)


def evaluate(repo: Path, base_sha: str, head_sha: str, base_ref: str, head_ref: str = "",
             allow_unversioned_release: bool = False) -> tuple[bool, str]:
    return gate.evaluate(repo, base_ref=base_ref, base_sha=base_sha, head_sha=head_sha,
                         head_ref=head_ref, allow_unversioned_release=allow_unversioned_release)


def test_version_from_release_branch():
    assert gate.version_of_branch("release/v0.5.5.3") == "0.5.5.3"
    assert gate.version_of_branch("release/0.5.5.3") == "0.5.5.3"
    assert gate.version_of_branch("dev") is None
    assert gate.version_of_branch("release/not-a-version") is None


def test_added_section_is_green(repo):
    root, base = repo
    changed = BASE_CHANGELOG.replace(
        "## [0.5.5.1] - 2026-10-28",
        "## [0.5.5.3] - 2026-11-02\n\n### Fixed\n- the thing this release fixes\n\n## [0.5.5.1] - 2026-10-28",
    )
    head = commit(root, "add 0.5.5.3 section", changed)
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert ok, message
    assert "0.5.5.3" in message


def test_missing_section_is_red(repo):
    root, base = repo
    head = commit(root, "touch unreleased only", BASE_CHANGELOG.replace("- something on dev", "- something else on dev"))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert not ok
    assert "must add a '## [0.5.5.3]' section" in message


def test_dev_into_release_sync_is_exempt(repo):
    """A dev -> release/* fast-forward sync carries already-changelogged work (2026-11-03)."""
    root, base = repo
    head = commit(root, "sync dev into release", BASE_CHANGELOG.replace("- something on dev", "- something else on dev"))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3", head_ref="dev")
    assert ok, message
    assert "sync" in message


def test_feature_into_release_still_requires_the_section(repo):
    root, base = repo
    head = commit(root, "no changelog change", BASE_CHANGELOG.replace("- something on dev", "- something else on dev"))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3", head_ref="feature/b")
    assert not ok
    assert "must add a '## [0.5.5.3]' section" in message


def test_existing_section_untouched_is_red(repo):
    root, _ = repo
    # The section already exists in the base and the PR edits somewhere else entirely.
    base = commit(
        root, "pre-existing 0.5.5.3", BASE_CHANGELOG.replace(
            "## [Unreleased]", "## [0.5.5.3] - 2026-11-02\n\n### Fixed\n- earlier draft\n\n## [Unreleased]"
        ),
    )
    head = commit(root, "edit unrelated section", (
        root / "CHANGELOG.md"
    ).read_text().replace("- something on dev", "- an unrelated line"))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert not ok
    assert "is unchanged by this pull request" in message
    # the reported range must be 1-based and consistent with the diff coordinate system
    assert "lines " in message


def test_changed_existing_section_is_green(repo):
    root, _ = repo
    base = commit(
        root, "pre-existing 0.5.5.3", BASE_CHANGELOG.replace(
            "## [Unreleased]", "## [0.5.5.3] - 2026-11-02\n\n### Fixed\n- earlier draft\n\n## [Unreleased]"
        ),
    )
    head = commit(root, "extend the 0.5.5.3 section", (
        root / "CHANGELOG.md"
    ).read_text().replace("- earlier draft", "- earlier draft\n- the change this PR ships"))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert ok, message


@pytest.mark.parametrize("base_ref", ["dev", "main"])
def test_release_preparation_source_gets_the_same_non_release_target_result(repo, base_ref):
    root, base = repo
    head = commit(root, "no changelog at all", BASE_CHANGELOG)
    ok, message = evaluate(root, base, head, base_ref, head_ref="release/v0.5.5.3")
    assert ok
    assert "promotion" not in message
    assert "not applicable" in message


def test_non_release_branch_is_not_applicable(repo):
    root, base = repo
    head = commit(root, "no changelog change", BASE_CHANGELOG)
    ok, message = evaluate(root, base, head, "feature/whatever")
    assert ok
    assert "not applicable" in message


def test_plain_pull_request_into_dev_is_not_called_a_promotion(repo):
    """An ordinary PR into dev must not be described as a promotion (that wording is a lie)."""
    root, base = repo
    head = commit(root, "no changelog change", BASE_CHANGELOG)
    ok, message = evaluate(root, base, head, "dev", head_ref="feature/x")
    assert ok
    assert "promotion" not in message
    assert "not applicable" in message


def test_missing_changelog_file_is_red(tmp_path, repo):
    root, base = repo
    (root / "CHANGELOG.md").unlink()
    git(root, "add", "-A")
    git(root, "-c", "user.email=ci@test.invalid", "-c", "user.name=ci", "commit", "-q", "-m", "drop changelog")
    head = git(root, "rev-parse", "HEAD").strip()
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert not ok
    assert "is missing" in message


def test_section_spans_and_changed_lines_helpers():
    text = "## [1.0.0]\n- a\n## [0.9.0]\n- b\n"
    spans = gate.section_spans(text)
    assert [span[0] for span in spans] == ["1.0.0", "0.9.0"]
    # 1-based, end exclusive: section 1.0.0 owns lines 1-2, so end == 3
    assert spans[0][1] == 1 and spans[0][2] == 3
    # the reported start must equal the line number a human sees with `grep -n`
    lines = text.splitlines()
    assert lines[spans[0][1] - 1].startswith("## [1.0.0]")
    assert lines[spans[1][1] - 1].startswith("## [0.9.0]")
    assert gate.changed_line_numbers("@@ -1,0 +2,3 @@\n+x\n+y\n+z\n") == {2, 3, 4}


def test_unversioned_release_branch_is_red(repo):
    """A release/<name> branch with no version in the name cannot be gated; silence is not allowed."""
    root, base = repo
    head = commit(root, "no changelog change", BASE_CHANGELOG)
    ok, message = evaluate(root, base, head, "release/bump-0.5.1")
    assert not ok
    assert "carries no vX.Y.Z version" in message


def test_unversioned_release_branch_passes_only_with_the_explicit_flag(repo):
    root, base = repo
    head = commit(root, "no changelog change", BASE_CHANGELOG)
    ok, message = evaluate(root, base, head, "release/bump-0.5.1", allow_unversioned_release=True)
    assert ok
    assert "--allow-unversioned-release" in message


def test_heading_only_section_is_red(repo):
    """Adding '## [0.5.5.3]' and nothing else is not a changelog entry."""
    root, base = repo
    head = commit(root, "heading only", BASE_CHANGELOG.replace(
        "## [Unreleased]", "## [0.5.5.3] - 2026-11-02\n\n## [Unreleased]"))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert not ok
    assert "has no added or rewritten entry line" in message
    assert "only its heading, scaffolding, or deletions changed" in message


def test_heading_plus_one_entry_is_green(repo):
    root, base = repo
    head = commit(root, "heading and entry", BASE_CHANGELOG.replace(
        "## [Unreleased]", "## [0.5.5.3] - 2026-11-02\n\n### Fixed\n- the bug this release fixes\n\n## [Unreleased]"))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert ok, message


def test_deletion_only_section_is_red_with_the_honest_message(repo):
    """Deleting an entry is a change, but not an entry: the message must say deletions, not headings."""
    root, _ = repo
    base = commit(root, "section with two entries", BASE_CHANGELOG.replace(
        "## [Unreleased]",
        "## [0.5.5.3] - 2026-11-02\n\n### Fixed\n- keeps\n- deletes me\n\n## [Unreleased]"))
    head = commit(root, "delete one entry", (root / "CHANGELOG.md").read_text().replace("- deletes me\n", ""))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert not ok
    assert "has no added or rewritten entry line" in message
    assert "only its heading, scaffolding, or deletions changed" in message


def test_deleting_the_only_entry_is_red(repo):
    root, _ = repo
    base = commit(root, "section with one entry", BASE_CHANGELOG.replace(
        "## [Unreleased]",
        "## [0.5.5.3] - 2026-11-02\n\n### Fixed\n- only entry\n\n## [Unreleased]"))
    head = commit(root, "delete the only entry", (root / "CHANGELOG.md").read_text().replace("- only entry\n", ""))
    ok, message = evaluate(root, base, head, "release/v0.5.5.3")
    assert not ok
    assert "has no added or rewritten entry line" in message
