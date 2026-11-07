"""write_authorized_file: the write-side twin of read_authorized_file (#1169)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hyprial.daemon.impl.path_authz import (
    PathRefused,
    operator_policy,
    session_policy,
)
from hyprial.daemon.impl.path_authz import write as write_module
from hyprial.daemon.impl.path_authz.write import write_authorized_file


@pytest.fixture
def roots(tmp_path: Path):
    root = Path(os.path.realpath(tmp_path))
    home = root / "hyprial-home"
    workspace = home / "agents" / "worker" / "workspace"
    workspace.mkdir(parents=True)
    (home / "secrets").mkdir()
    state = root / "state"
    state.mkdir()
    cwd = root / "repo"
    cwd.mkdir()
    outside = root / "outside"
    outside.mkdir()
    policy = session_policy(home, workspace=workspace, cwd=cwd, state_dir=state)
    return {
        "home": home,
        "workspace": workspace,
        "state": state,
        "cwd": cwd,
        "outside": outside,
        "session": policy,
        "operator": operator_policy(home, state_dir=state),
    }


def _refused(raw: Path | str, policy, **kwargs) -> PathRefused:
    with pytest.raises(PathRefused) as failure:
        write_authorized_file(raw, b"data", policy, overwrite=kwargs.get("overwrite", False))
    return failure.value


def test_session_writes_into_its_workspace_and_cwd(roots) -> None:
    for root in (roots["workspace"], roots["cwd"]):
        written = write_authorized_file(root / "out.bin", b"bytes", roots["session"], overwrite=False)
        assert written == root / "out.bin"
        assert written.read_bytes() == b"bytes"
        assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_session_cannot_write_outside_its_roots(roots) -> None:
    error = _refused(roots["outside"] / "x.bin", roots["session"])
    assert error.reason == "outside-allowed-roots"
    assert not (roots["outside"] / "x.bin").exists()


def test_nobody_writes_into_the_hyprial_home_or_state_dir(roots) -> None:
    for policy in (roots["session"], roots["operator"]):
        home_error = _refused(roots["home"] / "secrets" / "planted.json", policy)
        assert home_error.reason == "hyprial-home"
        assert home_error.path is None
        state_error = _refused(roots["state"] / "daemon.sock", policy)
        assert state_error.reason == "hyprial-home"
    assert not (roots["home"] / "secrets" / "planted.json").exists()


def test_relative_path_is_refused(roots) -> None:
    assert _refused("out.bin", roots["session"]).reason == "outside-allowed-roots"


def test_symlinked_parent_into_the_home_is_judged_where_it_lands(roots) -> None:
    link = roots["cwd"] / "secrets-link"
    link.symlink_to(roots["home"] / "secrets", target_is_directory=True)

    error = _refused(link / "planted.json", roots["session"])

    assert error.reason == "hyprial-home"
    assert not (roots["home"] / "secrets" / "planted.json").exists()


def test_final_symlink_component_is_never_followed(roots) -> None:
    victim = roots["cwd"] / "victim.txt"
    victim.write_text("original")
    link = roots["cwd"] / "link.txt"
    link.symlink_to(victim)

    for overwrite in (False, True):
        error = _refused(link, roots["session"], overwrite=overwrite)
        assert error.reason in {"symlink", "exists"}
    assert victim.read_text() == "original"


def test_existing_file_is_replaced_only_with_overwrite(roots) -> None:
    target = roots["cwd"] / "report.txt"
    target.write_text("keep")

    assert _refused(target, roots["session"]).reason == "exists"
    assert target.read_text() == "keep"

    write_authorized_file(target, b"new", roots["session"], overwrite=True)
    assert target.read_bytes() == b"new"


def test_parent_swap_after_judgment_cannot_redirect_the_open(
    roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: opening the judged pathname again must not follow a swapped parent."""

    parent = roots["cwd"] / "export"
    parent.mkdir()
    displaced = roots["cwd"] / "export-before-swap"
    denied = roots["home"] / "secrets"
    target = parent / "report.txt"
    original_decide = write_module._decide
    swapped = False

    def decide_then_swap(path, policy) -> None:
        nonlocal swapped
        original_decide(path, policy)
        if not swapped and path == target:
            swapped = True
            parent.rename(displaced)
            parent.symlink_to(denied, target_is_directory=True)

    monkeypatch.setattr(write_module, "_decide", decide_then_swap)

    with pytest.raises(PathRefused):
        write_authorized_file(target, b"must not escape", roots["session"], overwrite=False)

    assert swapped is True
    assert not (denied / target.name).exists()
    assert not (displaced / target.name).exists()


def test_unreportable_parent_descriptor_fails_closed_before_file_creation(
    roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = roots["cwd"] / "unverified.txt"
    monkeypatch.setattr(write_module, "_descriptor_path", lambda _fd: None)

    failure = _refused(target, roots["session"])

    assert failure.reason == "unverifiable"
    assert failure.path is None
    assert not target.exists()


def test_overwrite_refuses_a_hardlink_without_changing_its_secret_inode(roots) -> None:
    """M2: truncation must not clobber another name for the same inode."""

    secret = roots["home"] / "secrets" / "credential.json"
    original = b'{"token":"node-secret"}'
    secret.write_bytes(original)
    target = roots["cwd"] / "innocent.json"
    os.link(secret, target)

    with pytest.raises(PathRefused) as failure:
        write_authorized_file(target, b"replacement", roots["session"], overwrite=True)

    assert failure.value.reason == "hardlinked"
    assert failure.value.path is None
    assert str(target) not in str(failure.value)
    assert b"node-secret" not in str(failure.value).encode()
    assert secret.read_bytes() == original
    assert target.read_bytes() == original


def test_a_fifo_is_refused_without_blocking(roots) -> None:
    fifo = roots["cwd"] / "pipe"
    os.mkfifo(fifo)

    error = _refused(fifo, roots["session"], overwrite=True)

    assert error.reason in {"unreadable", "not-regular"}


def test_workspace_that_is_a_symlink_to_the_home_grants_nothing(tmp_path: Path) -> None:
    root = Path(os.path.realpath(tmp_path))
    home = root / "hyprial-home"
    (home / "secrets").mkdir(parents=True)
    agent = home / "agents" / "worker"
    agent.mkdir(parents=True)
    (agent / "workspace").symlink_to(home, target_is_directory=True)
    policy = session_policy(home, workspace=agent / "workspace", cwd=None)

    error = _refused(agent / "workspace" / "secrets" / "planted.json", policy)

    assert error.reason in {"hyprial-home", "other-agent-home"}
    assert not (home / "secrets" / "planted.json").exists()


def test_middle_component_swap_after_judgment_cannot_redirect_the_open(
    roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 1012: a swap above the immediate parent is refused too."""

    middle = roots["cwd"] / "reports"
    parent = middle / "2026" / "export"
    parent.mkdir(parents=True)
    displaced = roots["cwd"] / "reports-before-swap"
    denied = roots["home"] / "secrets"
    (denied / "2026" / "export").mkdir(parents=True)
    target = parent / "report.txt"
    original_decide = write_module._decide
    swapped = False

    def decide_then_swap(path, policy) -> None:
        nonlocal swapped
        original_decide(path, policy)
        if not swapped and path == target:
            swapped = True
            middle.rename(displaced)
            middle.symlink_to(denied, target_is_directory=True)

    monkeypatch.setattr(write_module, "_decide", decide_then_swap)

    with pytest.raises(PathRefused):
        write_authorized_file(target, b"must not escape", roots["session"], overwrite=False)

    assert swapped is True
    assert not (denied / "2026" / "export" / target.name).exists()
    assert not (displaced / "2026" / "export" / target.name).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_a_search_only_directory_on_the_path_is_not_refused(roots) -> None:
    """Review 1012: traversing a --x component must not need read access."""

    locked = roots["cwd"] / "search-only"
    inner = locked / "inner"
    inner.mkdir(parents=True)
    locked.chmod(0o111)
    try:
        written = write_authorized_file(
            inner / "out.bin", b"bytes", roots["session"], overwrite=False
        )
        assert written.read_bytes() == b"bytes"
    finally:
        locked.chmod(0o755)
