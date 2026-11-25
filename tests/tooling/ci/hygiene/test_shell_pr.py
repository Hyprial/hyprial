"""Isolated repros for scripts/ci/check_shell_pr.py (the OI-2 shell-PR detector).

Every repository here is a fresh temporary repository under pytest's ``tmp_path``:
nothing reads the developer's repository, HOME configuration or the network.  The
cases pin the two things that were written wrong the first time (§6.11):

* ``git cherry`` polarity -- empty = ancestor, all ``-`` = patch-equivalent
  (squash/rebase landing), any ``+`` = not landed;
* the base is **the PR's own ``base.ref``** -- a lane branch works, and a base
  that cannot be resolved is an ``inconclusive`` fail-closed, never a guess.

The detector must never close anything: the tests assert that only a label and a
comment are ever attempted, and that a dry run touches nothing.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_shell_pr.py"
SPEC = importlib.util.spec_from_file_location("check_shell_pr", SCRIPT)
shell_pr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shell_pr)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=check, capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(repo),
            "GIT_CONFIG_GLOBAL": str(repo / ".gitconfig-none"),
            "GIT_CONFIG_SYSTEM": str(repo / ".gitconfig-none"),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def new_repo(path: Path, base_branch: str = "dev") -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", base_branch)
    (path / "f.txt").write_text("base\n")
    git(path, "add", "f.txt")
    git(path, "commit", "-q", "-m", "base")
    return path


def add_change(repo: Path, branch: str, filename: str, content: str = "change\n") -> str:
    """Create ``branch`` with one commit on top of the current HEAD; returns its sha."""
    git(repo, "checkout", "-q", "-b", branch)
    (repo / filename).write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-q", "-m", f"add {filename}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


# --- kernel: the three cherry polarities -------------------------------------

def test_ff_landed_head_is_an_ancestor_of_the_base(tmp_path):
    """The head is already contained in the base: cherry is empty, verdict ancestor."""
    repo = new_repo(tmp_path / "r")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "dev")
    (repo / "later.txt").write_text("later\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "advance the base")

    verdict, facts = shell_pr.classify("dev", git(repo, "rev-parse", "dev").stdout.strip(), head, repo)
    assert verdict == shell_pr.V_ANCESTOR
    assert facts["head_is_ancestor"] is True
    assert facts["cherry_classification"] == "empty"
    body = shell_pr.comment_body(facts, verdict)
    assert "already contained in the base" in body
    assert facts["base_sha"][:12] in body and head[:12] in body


def test_squash_landed_head_is_patch_equivalent(tmp_path):
    """The base holds the same change made by another commit: all '-' => shell."""
    repo = new_repo(tmp_path / "r")
    head = add_change(repo, "feat", "g.txt")
    git(repo, "checkout", "-q", "dev")
    git(repo, "merge", "--squash", "-q", "feat")
    git(repo, "commit", "-q", "-m", "squash feat")
    base_sha = git(repo, "rev-parse", "dev").stdout.strip()

    verdict, facts = shell_pr.classify("dev", base_sha, head, repo)
    assert verdict == shell_pr.V_PATCH_EQUIVALENT
    assert facts["head_is_ancestor"] is False
    assert facts["cherry_classification"] == "all-minus"
    assert len(facts["cherry_lines"]) == 1
    assert facts["cherry_lines"][0].startswith("-")


def test_head_with_unlanded_work_is_not_a_shell(tmp_path):
    """A '+' line means the work is NOT in the base: the tool must do nothing."""
    repo = new_repo(tmp_path / "r")
    head = add_change(repo, "feat", "g.txt")

    verdict, facts = shell_pr.classify("dev", git(repo, "rev-parse", "dev").stdout.strip(), head, repo)
    assert verdict == shell_pr.V_NOT_LANDED
    assert facts["cherry_classification"] == "has-plus"
    assert any(line.startswith("+") for line in facts["cherry_lines"])


# --- the base is the PR's own base.ref, never a hardcoded branch -------------

def test_a_lane_base_ref_is_honoured(tmp_path):
    """A lane branch as the base works exactly like ``dev`` (§6.11: no hardcoding)."""
    repo = new_repo(tmp_path / "r", base_branch="intg/lane-x")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "later.txt").write_text("later\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "advance the lane")

    verdict, facts = shell_pr.classify("intg/lane-x",
                                       git(repo, "rev-parse", "intg/lane-x").stdout.strip(), head, repo)
    assert verdict == shell_pr.V_ANCESTOR
    assert facts["base_ref"] == "intg/lane-x"


def test_unresolvable_base_ref_is_inconclusive_and_writes_nothing(tmp_path, capsys, monkeypatch):
    """Fail-closed: an unknown base must not be replaced by a guessed branch."""
    repo = new_repo(tmp_path / "r")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(shell_pr, "apply_label_and_comment",
                        lambda *a, **k: pytest.fail("must not write on an inconclusive verdict"))
    rc = shell_pr.main(["--base", "intg/batch-1008", "--head", head, "--apply",
                        "--pr", "1", "--repo", "o/r", "--cwd", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "inconclusive" in out and "cannot be resolved" in out


# --- write discipline: dry run only, and never a close -----------------------

def test_dry_run_prints_the_comment_and_writes_nothing(tmp_path, capsys, monkeypatch):
    repo = new_repo(tmp_path / "r")
    head = add_change(repo, "feat", "g.txt")
    git(repo, "checkout", "-q", "dev")
    git(repo, "merge", "--squash", "-q", "feat")
    git(repo, "commit", "-q", "-m", "squash feat")
    monkeypatch.setattr(shell_pr, "apply_label_and_comment",
                        lambda *a, **k: pytest.fail("dry run must not write"))
    monkeypatch.setenv("FORGEJO_TOKEN", "irrelevant-in-dry-run")

    rc = shell_pr.main(["--base", "dev", "--head", head, "--pr", "7", "--repo", "o/r",
                        "--cwd", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "action=dry-run" in out
    assert f"`{shell_pr.LABEL}`" in out
    assert "does **not** close anything" in out


def test_apply_without_a_token_is_fail_closed(tmp_path, capsys, monkeypatch):
    repo = new_repo(tmp_path / "r")
    head = add_change(repo, "feat", "g.txt")
    git(repo, "checkout", "-q", "dev")
    git(repo, "merge", "--squash", "-q", "feat")
    git(repo, "commit", "-q", "-m", "squash feat")
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    monkeypatch.setattr(shell_pr, "apply_label_and_comment",
                        lambda *a, **k: pytest.fail("no token must mean no write"))
    rc = shell_pr.main(["--base", "dev", "--head", head, "--apply", "--pr", "7",
                        "--repo", "o/r", "--cwd", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fail-closed" in out
    assert "verdict=patch-equivalent" in out  # the detection still stands on its own


def test_apply_writes_exactly_one_label_and_one_comment(tmp_path, monkeypatch):
    """The only writes the detector may make: the label and the comment."""
    repo = new_repo(tmp_path / "r")
    head = add_change(repo, "feat", "g.txt")
    git(repo, "checkout", "-q", "dev")
    git(repo, "merge", "--squash", "-q", "feat")
    git(repo, "commit", "-q", "-m", "squash feat")
    calls: list[tuple] = []

    def fake_apply(server, repo_name, pr, token, body):
        calls.append((server, repo_name, pr, token, body))
        return ["label stub: ok", "comment stub: ok"]

    monkeypatch.setenv("FORGEJO_TOKEN", "t")
    monkeypatch.setattr(shell_pr, "apply_label_and_comment", fake_apply)
    rc = shell_pr.main(["--base", "dev", "--head", head, "--apply", "--pr", "9",
                        "--repo", "o/r", "--cwd", str(repo)])
    assert rc == 0
    assert len(calls) == 1
    _, repo_name, pr, token, body = calls[0]
    assert (repo_name, pr, token) == ("o/r", 9, "t")
    assert f"`{shell_pr.LABEL}`" in body and "verdict: `patch-equivalent`" in body
