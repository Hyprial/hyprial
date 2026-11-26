"""Pi P2 cwd trust: an agent's own 0700 workspace is a trust root.

2026-10-16 production: resident pi agents default their cwd to their own
agent-home workspace (``~/.hyprial/.../agents/<name>/workspace``, 0700, not a
Git repository).  ``resolve_approved_pi_project`` only accepted a Git
worktree, so ``hyprial start`` refused every such agent with "Pi P2 cwd is not
inside an approved Git repository", and the operator's workaround was to pass
``--cwd`` pointing at an unrelated Git checkout.

These tests pin the bounded rule: the trust root may be the agent's OWN
workspace (private to its owner, not a Git worktree), and nothing else.  An
arbitrary directory, another agent's workspace, or a group/other-accessible
workspace are all refused.

Decision (2026-11-26, KP-141): privacy of the trust root is decided by
``hyprial.kernel.private_tree_violation``.  Real mode bits cannot express the
cases that matter, so the cases added for it change the inputs of that check
instead of the filesystem:

* owner and bit cases keep the real check and pin it to its POSIX branch,
  where the verdict is a pure function of the metadata, then forge the
  ``stat`` result of the root.  A foreign owner cannot be produced without
  privileges, and some filesystems drop a requested setgid bit on write;
* descriptor cases keep the real directory and replace the check's verdict,
  which is what a platform without POSIX metadata decides by.

A forged ``stat_result`` keeps the real file-type bits and changes only the
field under test, so the surrounding directory checks still run against the
real filesystem.  Every case records what the product handed to the check and
asserts on it, so a product that stops consulting ``stat`` cannot turn a
forgery into a silent no-op.  No case skips: an unexpected skip fails the
suite on the platform these cases exist for.

An ``OSError`` raised by the check itself is a trust refusal; an ``OSError``
from ``stat`` of the root is not wrapped and still escapes as it did before.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi import loader
from hyprial.daemon.impl.harnesses.pi.loader import (
    PiProjectTrustError,
    PiProjectTrustReceipt,
    resolve_approved_pi_project,
)
from hyprial.kernel import current_uid
from hyprial.kernel.impl.manysys.fs import private_access


def _workspace(root: Path, name: str) -> Path:
    workspace = root / "agents" / name / "workspace"
    workspace.mkdir(parents=True)
    os.chmod(workspace, 0o700)
    return workspace


def test_own_workspace_is_signed_as_a_workspace_trust_root(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")

    receipt = resolve_approved_pi_project(
        cwd=str(workspace), runtime_args=("--approve",), own_workspace=workspace
    )

    assert receipt.trusted is True
    assert receipt.scope == "workspace"
    assert receipt.repository_root == workspace.resolve()
    assert receipt.cwd == workspace.resolve()


def test_a_git_worktree_still_wins_over_the_workspace_root(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    nested = repository / "src"
    nested.mkdir()

    receipt = resolve_approved_pi_project(
        cwd=str(nested), runtime_args=("--approve",), own_workspace=tmp_path / "workspace"
    )

    assert receipt.scope == "repository"
    assert receipt.repository_root == repository.resolve()


def test_an_unrelated_non_git_directory_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()

    with pytest.raises(PiProjectTrustError, match="approved Git repository"):
        resolve_approved_pi_project(
            cwd=str(unrelated), runtime_args=("--approve",), own_workspace=workspace
        )


def test_another_agents_workspace_is_refused(tmp_path: Path) -> None:
    own = _workspace(tmp_path, "be-dev-1")
    intruder = _workspace(tmp_path, "be-dev-2")

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(intruder), runtime_args=("--approve",), own_workspace=own
        )


def test_a_group_readable_workspace_is_not_a_trust_root(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")
    os.chmod(workspace, 0o755)

    with pytest.raises(PiProjectTrustError, match="private to its owner"):
        resolve_approved_pi_project(
            cwd=str(workspace), runtime_args=("--approve",), own_workspace=workspace
        )


def test_workspace_trust_still_requires_explicit_approval(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")

    with pytest.raises(PiProjectTrustError, match="requires explicit --approve"):
        resolve_approved_pi_project(
            cwd=str(workspace), runtime_args=(), own_workspace=workspace
        )


def test_receipt_rejects_a_workspace_root_that_is_not_the_cwd(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "be-dev-1")
    nested = workspace / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="must equal cwd"):
        PiProjectTrustReceipt(nested, workspace, True, "workspace")


def test_a_workspace_symlink_to_another_workspace_is_refused(tmp_path: Path) -> None:
    """A link from this agent's workspace to another agent's must not trust it."""

    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    intruder = _workspace(tmp_path, "be-dev-2")
    own.symlink_to(intruder, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(intruder), runtime_args=("--approve",), own_workspace=own
        )


def test_a_workspace_symlink_to_an_arbitrary_directory_is_refused(
    tmp_path: Path,
) -> None:
    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    arbitrary = tmp_path / "arbitrary"
    arbitrary.mkdir()
    os.chmod(arbitrary, 0o700)
    own.symlink_to(arbitrary, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(arbitrary), runtime_args=("--approve",), own_workspace=own
        )


def test_a_multi_level_workspace_symlink_is_refused(tmp_path: Path) -> None:
    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    target = _workspace(tmp_path, "be-dev-2")
    hop = tmp_path / "hop"
    hop.symlink_to(target, target_is_directory=True)
    own.symlink_to(hop, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(target), runtime_args=("--approve",), own_workspace=own
        )


def test_a_workspace_symlink_is_refused_even_when_cwd_is_the_link(
    tmp_path: Path,
) -> None:
    own = tmp_path / "agents" / "be-dev-1" / "workspace"
    own.parent.mkdir(parents=True)
    target = _workspace(tmp_path, "be-dev-2")
    own.symlink_to(target, target_is_directory=True)

    with pytest.raises(PiProjectTrustError):
        resolve_approved_pi_project(
            cwd=str(own), runtime_args=("--approve",), own_workspace=own
        )


_STAT_FIELDS = (
    "st_mode",
    "st_ino",
    "st_dev",
    "st_nlink",
    "st_uid",
    "st_gid",
    "st_size",
)


def _forge_root_stat(
    monkeypatch: pytest.MonkeyPatch, root: Path, **fields: int
) -> None:
    """Report ``fields`` in place of the real ones for ``root`` only."""

    real_stat = Path.stat
    resolved = root.resolve()

    def forged(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        observed = real_stat(path, follow_symlinks=follow_symlinks)
        if path != resolved:
            return observed
        values = list(observed)
        for index, name in enumerate(_STAT_FIELDS):
            if name in fields:
                values[index] = fields[name]
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", forged)


def _posix_metadata(
    monkeypatch: pytest.MonkeyPatch, root: Path, **fields: int
) -> list[tuple[Path, os.stat_result]]:
    """Judge ``root`` by uid and mode bits alone, with ``fields`` forged.

    The real kernel check is pinned to its POSIX branch, where the verdict is a
    pure function of the metadata, so the result is the same on every platform
    and depends only on the forged fields.  Returns what the product handed to
    the check; a test asserts the forged fields arrived, so a product that
    stops consulting ``stat`` cannot turn the forgery into a silent no-op.
    """

    monkeypatch.setattr(private_access, "posix_metadata_enforced", lambda: True)
    _forge_root_stat(monkeypatch, root, **fields)
    seen: list[tuple[Path, os.stat_result]] = []
    real_violation = loader.private_tree_violation

    def recording(root_argument: Path, metadata: os.stat_result) -> str | None:
        seen.append((root_argument, metadata))
        return real_violation(root_argument, metadata)

    monkeypatch.setattr(loader, "private_tree_violation", recording)
    return seen


def _replace_kernel(
    monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> list[tuple[Path, os.stat_result]]:
    """Make the kernel check return ``answer``; return what the product passed."""

    seen: list[tuple[Path, os.stat_result]] = []

    def violation(root: Path, metadata: os.stat_result) -> str | None:
        seen.append((root, metadata))
        return answer

    monkeypatch.setattr(loader, "private_tree_violation", violation)
    return seen


def _handed_roots(seen: list[tuple[Path, os.stat_result]]) -> list[Path]:
    return [root for root, _metadata in seen]


def _handed_modes(seen: list[tuple[Path, os.stat_result]]) -> list[int]:
    return [int(stat.S_IMODE(metadata.st_mode)) for _root, metadata in seen]


def _trust(workspace: Path) -> PiProjectTrustReceipt:
    return resolve_approved_pi_project(
        cwd=str(workspace), runtime_args=("--approve",), own_workspace=workspace
    )


def test_a_workspace_owned_by_another_user_is_not_a_trust_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 0700 root owned by someone else is refused: privacy includes the owner.

    Before the fix the product looked at the group/other bits alone and signed
    this root.
    """

    workspace = _workspace(tmp_path, "be-dev-1")
    foreign_uid = current_uid() + 1
    seen = _posix_metadata(
        monkeypatch,
        workspace,
        st_uid=foreign_uid,
        st_mode=stat.S_IFDIR | 0o700,
    )

    with pytest.raises(PiProjectTrustError, match="private to its owner"):
        _trust(workspace)

    assert _handed_roots(seen) == [workspace.resolve()]
    assert [metadata.st_uid for _root, metadata in seen] == [foreign_uid]


@pytest.mark.parametrize("mode", [0o2700, 0o1700, 0o4700])
def test_a_private_workspace_with_a_setgid_sticky_or_setuid_bit_is_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: int
) -> None:
    """A private directory carrying such a bit is still private.

    The kernel check stays real and sees the forged mode.  An exact-mode
    comparison in its place would refuse all three, which is why the product
    must not use one.
    """

    workspace = _workspace(tmp_path, "be-dev-1")
    seen = _posix_metadata(monkeypatch, workspace, st_mode=stat.S_IFDIR | mode)

    receipt = _trust(workspace)

    assert _handed_roots(seen) == [workspace.resolve()]
    assert _handed_modes(seen) == [mode]
    assert receipt.scope == "workspace"
    assert receipt.repository_root == workspace.resolve()


@pytest.mark.parametrize("violation", ["unsafe-acl", "wrong-owner", "unsafe-mode"])
def test_a_workspace_the_kernel_reports_unsafe_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, violation: str
) -> None:
    """The kernel verdict decides even when the mode bits look private."""

    workspace = _workspace(tmp_path, "be-dev-1")
    seen = _replace_kernel(monkeypatch, violation)

    with pytest.raises(PiProjectTrustError, match="private to its owner"):
        _trust(workspace)

    assert _handed_roots(seen) == [workspace.resolve()]


def test_a_workspace_with_synthesised_mode_bits_is_judged_by_the_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``0o777`` mode bits are not a refusal when the kernel reports privacy.

    Platforms without POSIX metadata report the same bits for every directory,
    so the security descriptor and not the bits is the authority there.  Before
    the fix the product refused this root on the bits alone.
    """

    workspace = _workspace(tmp_path, "be-dev-1")
    seen = _replace_kernel(monkeypatch, None)
    _forge_root_stat(monkeypatch, workspace, st_mode=stat.S_IFDIR | 0o777)

    receipt = _trust(workspace)

    assert _handed_roots(seen) == [workspace.resolve()]
    assert _handed_modes(seen) == [0o777]
    assert receipt.scope == "workspace"
    assert receipt.cwd == workspace.resolve()


def test_a_workspace_the_kernel_cannot_verify_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A platform that cannot verify privacy is a refusal, never a silent trust."""

    workspace = _workspace(tmp_path, "be-dev-1")
    _replace_kernel(monkeypatch, "unsupported-private-acl")

    with pytest.raises(PiProjectTrustError, match="private to its owner"):
        _trust(workspace)


def test_a_workspace_whose_privacy_cannot_be_inspected_is_a_trust_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ``OSError`` from the kernel check is a permanent trust refusal.

    The descriptor check raises ``OSError`` when it cannot read the security
    descriptor or the current account.  Escaping bare, it would carry no
    permanent-failure flag and the streaming pump would retry it until the
    startup deadline.
    """

    workspace = _workspace(tmp_path, "be-dev-1")
    seen: list[tuple[Path, os.stat_result]] = []

    def uninspectable(root: Path, metadata: os.stat_result) -> str | None:
        seen.append((root, metadata))
        raise OSError("cannot inspect the Windows security descriptor for workspace")

    monkeypatch.setattr(loader, "private_tree_violation", uninspectable)

    with pytest.raises(PiProjectTrustError, match="privacy cannot be inspected") as raised:
        _trust(workspace)

    assert raised.value.permanent_start_failure is True
    assert _handed_roots(seen) == [workspace.resolve()]


def test_an_unreadable_workspace_root_still_raises_its_own_os_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the kernel check is wrapped; ``stat`` of the root keeps its error.

    ``Path.stat`` of the resolved root fails while the directory checks before
    it still see the real filesystem through ``os.path.isdir``.  The kernel is
    never reached.
    """

    workspace = _workspace(tmp_path, "be-dev-1")
    resolved = workspace.resolve()
    real_stat = Path.stat
    failure = OSError("stat of the workspace root failed")

    def failing(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if follow_symlinks and path == resolved:
            raise failure
        return real_stat(path, follow_symlinks=follow_symlinks)

    def real_is_dir(path: Path, *, follow_symlinks: bool = True) -> bool:
        return os.path.isdir(path)

    monkeypatch.setattr(Path, "stat", failing)
    monkeypatch.setattr(Path, "is_dir", real_is_dir)
    seen = _replace_kernel(monkeypatch, None)

    with pytest.raises(OSError) as raised:
        _trust(workspace)

    assert raised.value is failure
    assert seen == []
