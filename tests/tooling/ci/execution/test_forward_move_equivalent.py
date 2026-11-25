"""Two-way control for scripts/ci/forward_move_equivalent.sh.

A judge without a control is idle.  (a) a genuine pure forward-move MUST report
PURE; (b) a commit with the SAME parents but a HAND-EDITED tree MUST report NOT
PURE.  (b) is the discriminating case: the structural layer alone passes it, so
it proves the decisive tree-equality layer is load-bearing.

The scene is built so the pure move MISSES layer 1 (the own-diff fast path): the
new base edits the same file near the reviewed change, shifting the patch
context.  Layer 2 is therefore what decides the PURE verdict, and the test
asserts that -- a green that never touched the decisive layer is not evidence.

LESSON (an earlier mistake): a control built on a NO-CONFLICT merge
(``checkout --ours`` + ``add`` + ``write-tree``) is VACUOUS -- the tree does not
move, so nothing is tested.  A control that cannot fail is not a control.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TOOL = ROOT / "scripts" / "ci" / "forward_move_equivalent.sh"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@forward-move.invalid",
            "-c",
            "user.name=forward-move-test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def rev(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def run_tool(
    repo: Path, old_mb: str, old_head: str, new_base: str, new_head: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(TOOL), old_mb, old_head, new_base, new_head],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def _scene(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    lines = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"
    (repo / "shared.txt").write_text(lines, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "root")

    # The reviewed head edits line 5.
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "shared.txt").write_text(lines.replace("l5\n", "l5-head\n"), encoding="utf-8")
    git(repo, "commit", "-qam", "reviewed change")
    old_head = rev(repo, "HEAD")

    # The new base edits line 3: near enough to shift the patch context, but a
    # different line, so the merge is clean and the move is genuinely pure.
    git(repo, "checkout", "-q", "main")
    (repo / "shared.txt").write_text(lines.replace("l3\n", "l3-base\n"), encoding="utf-8")
    git(repo, "commit", "-qam", "new base")
    new_base = rev(repo, "HEAD")

    old_mb = git(repo, "merge-base", old_head, new_base).stdout.strip()

    # The honest forward move: a two-parent merge of old_head with new_base.
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")
    pure_head = rev(repo, "HEAD")
    return repo, old_mb, old_head, new_base, pure_head


def test_a_genuine_forward_move_is_pure_and_decided_by_layer2(tmp_path):
    repo, old_mb, old_head, new_base, pure_head = _scene(tmp_path)
    completed = run_tool(repo, old_mb, old_head, new_base, pure_head)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "verdict=PURE" in completed.stdout
    # The fast path must have missed, or layer 2 was never exercised.
    assert "own_diffs_identical=no" in completed.stdout
    assert "pure=yes" in completed.stdout
    assert f"tree={rev(repo, pure_head + '^{tree}')}" in completed.stdout


def _chain_scene(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    """A two-hop pure advance, with both hops exercised by the decisive layer.

    #1157's real shape (three hops) with one fewer advance, so the test is fast:
    O reviewed -> merge base B1 -> M -> merge base B2 -> N, where every hop's
    tree equals its own auto-merge tree.  Layer 1 (own-diff byte equality) is
    made to MISS by editing the same file near the reviewed change, so the
    transitive walk in layer 2 is what must decide.
    """

    repo = tmp_path / "chain-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    lines = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"
    (repo / "shared.txt").write_text(lines, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "root")

    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "shared.txt").write_text(lines.replace("l5\n", "l5-head\n"), encoding="utf-8")
    git(repo, "commit", "-qam", "reviewed change")
    old_head = rev(repo, "HEAD")

    git(repo, "checkout", "-q", "main")
    (repo / "shared.txt").write_text(lines.replace("l3\n", "l3-base\n"), encoding="utf-8")
    git(repo, "commit", "-qam", "base advance 1")
    base1 = rev(repo, "HEAD")

    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")  # hop 1
    intermediate = rev(repo, "HEAD")

    git(repo, "checkout", "-q", "main")
    (repo / "shared.txt").write_text(
        lines.replace("l3\n", "l3-base\n").replace("l7\n", "l7-base\n"),
        encoding="utf-8",
    )
    git(repo, "commit", "-qam", "base advance 2")
    base2 = rev(repo, "HEAD")

    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")  # hop 2
    new_head = rev(repo, "HEAD")

    old_mb = git(repo, "merge-base", old_head, base2).stdout.strip()
    assert git(repo, "rev-list", "--parents", "-1", intermediate).stdout.split()[1:] == [
        old_head,
        base1,
    ]
    assert git(repo, "rev-list", "--parents", "-1", new_head).stdout.split()[1:] == [
        intermediate,
        base2,
    ]
    return repo, old_mb, old_head, base2, new_head


def test_a_two_hop_pure_chain_is_pure(tmp_path):
    """The transitive walk: every hop must pass, and the verdict names hops=2."""

    repo, old_mb, old_head, new_base, new_head = _chain_scene(tmp_path)
    completed = run_tool(repo, old_mb, old_head, new_base, new_head)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "verdict=PURE" in completed.stdout
    assert "hops=2" in completed.stdout
    assert completed.stdout.count("pure=yes") == 2
    # Layer 1 must have missed, or the chain walk was never exercised.
    assert "own_diffs_identical=no" in completed.stdout


def test_a_hand_edited_merge_with_the_same_parents_is_not_pure(tmp_path):
    repo, old_mb, old_head, new_base, pure_head = _scene(tmp_path)

    # Same two parents, hand-edited tree: the merge keeps the reviewed change
    # but LOSES the base's edit -- a plausible conflict resolution that must not
    # renew the review.
    worktree = tmp_path / "edit"
    git(repo, "worktree", "add", "--detach", "--force", str(worktree), pure_head)
    lines = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"
    (worktree / "shared.txt").write_text(
        lines.replace("l5\n", "l5-head\n"), encoding="utf-8"
    )
    git(worktree, "add", "shared.txt")
    bad_tree = git(worktree, "write-tree").stdout.strip()
    git(repo, "worktree", "remove", "--force", str(worktree))
    bad_head = git(
        repo, "commit-tree", bad_tree, "-p", old_head, "-p", new_base, "-m", "hand-edited"
    ).stdout.strip()
    parents = git(repo, "rev-list", "--parents", "-1", bad_head).stdout.split()
    assert parents[1:] == [old_head, new_base], "the structural layer must pass it"

    completed = run_tool(repo, old_mb, old_head, new_base, bad_head)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "verdict=NOT_PURE" in completed.stdout
    assert "pure=no" in completed.stdout
    assert "hand-resolution-or-content-change" in completed.stdout
