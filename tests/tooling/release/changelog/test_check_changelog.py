"""Release notes must belong to the exact declared version, not a near match."""

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[4]
CHECKER = ROOT / "scripts/ci/check-changelog.mjs"


def check(repo, *args):
    return subprocess.run(
        ["node", str(CHECKER), "--root", str(repo), *args],
        capture_output=True, text=True, timeout=10,
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "VERSION").write_text("0.5.5.1\n")
    return tmp_path


@pytest.mark.parametrize("text,reason", [
    ("## [Unreleased]\n- pending\n", "missing section"),
    ("## [0.5.5.1]\n\n### Fixed\n- \n", "no non-empty bullet"),
    ("## [0.5.5]\n- older\n## [0.5.5.10]\n- newer\n", "missing section"),
    ("## [0.5.5.1]\n### Fixed\n## [0.5.5]\n- older\n", "no non-empty bullet"),
    ("```md\n## [0.5.5.1]\n- example\n```\n", "missing section"),
    ("~~~\n## [0.5.5.1]\n- example\n~~~\n", "missing section"),
    ("<!--\n## [0.5.5.1]\n- hidden\n-->\n", "missing section"),
    ("    ## [0.5.5.1]\n    - example\n", "missing section"),
    ("## [v0.5.5.1]\n- prefixed\n", "missing section"),
    ("## [0.5.5.1]\n```\n- code\n```\n", "no non-empty bullet"),
    ("## [0.5.5.1]\n- <!-- hidden -->\n", "no non-empty bullet"),
    ("## [0.5.5.1]\n* * *\n", "no non-empty bullet"),
    ("## [0.5.5.1]\n- first\n## [0.5.5.1]\n- duplicate\n", "duplicate section"),
])
def test_reject_invalid_section(repo, text, reason):
    (repo / "CHANGELOG.md").write_text(text)
    result = check(repo)
    assert result.returncode == 1
    assert reason in result.stderr


@pytest.mark.parametrize("suffix", ["", " - 2026-10-26"])
@pytest.mark.parametrize("marker", ["-", "*", "+"])
def test_accept_exact_section_and_real_bullet(repo, suffix, marker):
    (repo / "CHANGELOG.md").write_text(
        f"## [0.5.5.1]{suffix}\n\n### Fixed\n{marker} Actual release note.\n"
    )
    assert check(repo, "--tag", "v0.5.5.1").returncode == 0


def test_explicit_version(repo):
    (repo / "CHANGELOG.md").write_text("## [0.5.5]\n- Prior release.\n")
    assert check(repo, "--version", "0.5.5").returncode == 0


def test_tag_version_mismatch(repo):
    (repo / "CHANGELOG.md").write_text("## [0.5.5]\n- Present but wrong.\n")
    result = check(repo, "--tag", "v0.5.5")
    assert result.returncode == 1
    assert "tag/VERSION mismatch" in result.stderr


def test_explicit_version_cannot_override_tag_version_check(repo):
    (repo / "CHANGELOG.md").write_text("## [0.5.5]\n- Wrong version.\n")
    result = check(repo, "--version", "0.5.5", "--tag", "v0.5.5")
    assert result.returncode == 1
    assert "tag/VERSION mismatch" in result.stderr


@pytest.mark.parametrize("tag", ["internal", "nightly", "stable", "V0.5.5.1", "v"])
def test_checker_requires_formal_tag(repo, tag):
    (repo / "CHANGELOG.md").write_text("## [0.5.5.1]\n- Note.\n")
    result = check(repo, "--tag", tag)
    assert result.returncode == 1
    assert "invalid release tag" in result.stderr


def test_missing_file_fails_closed(repo):
    result = check(repo)
    assert result.returncode == 1
    assert "CHANGELOG.md" in result.stderr


def test_rev_reads_both_files_at_target_not_worktree(repo):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / "CHANGELOG.md").write_text("## [0.5.5.1]\n- Frozen note.\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    rev = git("rev-parse", "HEAD")
    (repo / "VERSION").write_text("9.9.9\n")
    (repo / "CHANGELOG.md").write_text("## [9.9.9]\n- Dirty tree.\n")
    assert check(repo, "--rev", rev, "--tag", "v0.5.5.1").returncode == 0
    result = check(repo, "--rev", rev, "--tag", "v9.9.9")
    assert result.returncode == 1
    assert "tag/VERSION mismatch" in result.stderr
