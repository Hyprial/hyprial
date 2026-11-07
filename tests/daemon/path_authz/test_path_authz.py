"""path_authz: realpath + component containment, hyprial home denied, one fd.

The five cases hq required (2026-10-06): a symlink out of the allowed root,
a ``../`` escape, a sibling prefix (``/a/ws`` vs ``/a/ws2``), a symlink to
the hyprial home's ``secrets``, and a symlink swapped in after the check.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.path_authz import (
    REASON_CHANGED,
    REASON_EMPTY,
    REASON_HYPRIAL_HOME,
    REASON_NOT_FOUND,
    REASON_NOT_REGULAR,
    REASON_OTHER_AGENT_HOME,
    REASON_OUTSIDE_ALLOWED_ROOTS,
    REASON_SYMLINK,
    REASON_TOO_LARGE,
    REASON_UNVERIFIED,
    PathRefused,
    operator_policy,
    read_authorized_file,
    session_policy,
)
from hyprial.daemon.impl.path_authz import policy as policy_module

_LIMIT = 1024


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    root = Path(os.path.realpath(tmp_path))
    home = root / "hyprial-home"
    workspace = home / "agents" / "worker" / "workspace"
    workspace.mkdir(parents=True)
    (home / "agents" / "worker" / "secrets").mkdir()
    (home / "agents" / "worker" / "secrets" / "token").write_text("agent-secret")
    (home / "agents" / "other" / "workspace").mkdir(parents=True)
    (home / "agents" / "other" / "workspace" / "notes.txt").write_text("theirs")
    (home / "secrets").mkdir()
    (home / "secrets" / "lark.json").write_text("node-secret")
    (home / "settings.json").write_text("{}")
    repo = root / "a" / "ws"
    repo.mkdir(parents=True)
    sibling = root / "a" / "ws2"
    sibling.mkdir(parents=True)
    (sibling / "leak.txt").write_text("sibling")
    outside = root / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside")
    return {
        "root": root,
        "home": home,
        "workspace": workspace,
        "repo": repo,
        "sibling": sibling,
        "outside": outside,
    }


def _session(layout: dict[str, Path]):
    return session_policy(
        layout["home"], workspace=layout["workspace"], cwd=layout["repo"]
    )


def _refused(path: Path | str, policy, reason: str) -> PathRefused:
    with pytest.raises(PathRefused) as failure:
        read_authorized_file(path, policy, max_bytes=_LIMIT)
    assert failure.value.reason == reason
    return failure.value


def test_session_reads_its_workspace_and_its_cwd(layout: dict[str, Path]) -> None:
    (layout["workspace"] / "report.txt").write_text("from workspace")
    (layout["repo"] / "diff.patch").write_text("from cwd")
    policy = _session(layout)

    first = read_authorized_file(
        layout["workspace"] / "report.txt", policy, max_bytes=_LIMIT
    )
    second = read_authorized_file(layout["repo"] / "diff.patch", policy, max_bytes=_LIMIT)

    assert (first.name, first.data) == ("report.txt", b"from workspace")
    assert (second.name, second.data) == ("diff.patch", b"from cwd")


def test_symlink_out_of_the_root_is_refused(layout: dict[str, Path]) -> None:
    link = layout["repo"] / "innocent.txt"
    link.symlink_to(layout["outside"] / "private.txt")

    refusal = _refused(link, _session(layout), REASON_OUTSIDE_ALLOWED_ROOTS)
    assert refusal.path == str(layout["outside"] / "private.txt")


def test_dotdot_escape_is_refused(layout: dict[str, Path]) -> None:
    escape = f"{layout['repo']}/../../outside/private.txt"

    _refused(escape, _session(layout), REASON_OUTSIDE_ALLOWED_ROOTS)


def test_sibling_prefix_is_not_inside_the_root(layout: dict[str, Path]) -> None:
    # A string-prefix check would accept /a/ws2/leak.txt for root /a/ws.
    _refused(layout["sibling"] / "leak.txt", _session(layout), REASON_OUTSIDE_ALLOWED_ROOTS)


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (("secrets", "lark.json"), REASON_HYPRIAL_HOME),
        (("settings.json",), REASON_HYPRIAL_HOME),
        (("agents", "worker", "secrets", "token"), REASON_OTHER_AGENT_HOME),
        (("agents", "other", "workspace", "notes.txt"), REASON_OTHER_AGENT_HOME),
    ],
)
def test_symlink_into_the_hyprial_home_is_refused_without_echoing_it(
    layout: dict[str, Path], target: tuple[str, ...], reason: str
) -> None:
    link = layout["workspace"] / "looks-harmless.txt"
    link.symlink_to(layout["home"].joinpath(*target))

    for policy in (_session(layout), operator_policy(layout["home"])):
        refusal = _refused(link, policy, reason)
        assert refusal.path is None
        assert str(layout["home"]) not in str(refusal)


def test_operator_reads_anywhere_but_the_hyprial_home(layout: dict[str, Path]) -> None:
    policy = operator_policy(layout["home"])

    read = read_authorized_file(
        layout["outside"] / "private.txt", policy, max_bytes=_LIMIT
    )

    assert read.data == b"outside"
    _refused(layout["home"] / "secrets" / "lark.json", policy, REASON_HYPRIAL_HOME)


def test_state_dir_outside_the_home_is_refused_too(layout: dict[str, Path]) -> None:
    # HARNESS_STATE_DIR may sit beside the home, not inside it.
    state = layout["root"] / "state"
    state.mkdir()
    (state / "daemon.sqlite3").write_text("rows")
    link = layout["repo"] / "db.txt"
    link.symlink_to(state / "daemon.sqlite3")

    for policy in (
        session_policy(
            layout["home"], workspace=layout["workspace"], cwd=layout["repo"], state_dir=state
        ),
        operator_policy(layout["home"], state_dir=state),
    ):
        assert _refused(link, policy, REASON_HYPRIAL_HOME).path is None


def test_case_variant_of_the_home_is_refused_before_open(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a case-insensitive volume realpath keeps ".HYPRIAL-HOME"; the
    # decision must not depend on the descriptor re-check to catch it.
    variant = layout["root"] / "HYPRIAL-HOME" / "secrets" / "lark.json"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem")
    monkeypatch.setattr(policy_module, "_descriptor_path", lambda fd: None)

    for policy in (_session(layout), operator_policy(layout["home"])):
        assert _refused(variant, policy, REASON_HYPRIAL_HOME).path is None


@pytest.mark.parametrize(
    "swap",
    ["home", "secrets", "other-agent", "agent-dir"],
)
def test_workspace_replaced_by_a_symlink_is_not_trusted(
    layout: dict[str, Path], swap: str
) -> None:
    # The own workspace is the one home subtree a session may read; if it,
    # or agents/<name>, resolves anywhere else it must not become a root.
    home = layout["home"]
    workspace = layout["workspace"]
    workspace.rmdir()
    if swap == "agent-dir":
        agent_dir = home / "agents" / "worker"
        moved = layout["root"] / "moved-agent"
        agent_dir.rename(moved)
        agent_dir.symlink_to(home)  # agents/worker/workspace -> <home>/workspace
        (home / "workspace").symlink_to(home / "secrets")
    else:
        target = {
            "home": home,
            "secrets": home / "secrets",
            "other-agent": home / "agents" / "other" / "workspace",
        }[swap]
        workspace.symlink_to(target)
    policy = session_policy(home, workspace=workspace, cwd=layout["repo"])

    assert policy.own_workspace is None
    assert policy.allowed_roots == (layout["repo"],)
    leak = {
        "home": workspace / "secrets" / "lark.json",
        "secrets": workspace / "lark.json",
        "other-agent": workspace / "notes.txt",
        "agent-dir": workspace / "lark.json",
    }[swap]
    with pytest.raises(PathRefused) as failure:
        read_authorized_file(leak, policy, max_bytes=_LIMIT)
    assert failure.value.reason in {REASON_HYPRIAL_HOME, REASON_OTHER_AGENT_HOME}


@pytest.mark.skipif(
    not (sys.platform == "darwin" or sys.platform.startswith("linux")),
    reason="the descriptor path is required only where the platform reports it",
)
def test_unreadable_descriptor_path_fails_closed(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (layout["repo"] / "report.txt").write_text("fine")
    if sys.platform == "darwin":
        import fcntl

        def broken(*_args):
            raise OSError("F_GETPATH failed")

        monkeypatch.setattr(fcntl, "fcntl", broken)
    else:
        real_readlink = os.readlink

        def broken(path, *args, **kwargs):
            if str(path).startswith("/proc/self/fd/"):
                raise OSError("no /proc")
            return real_readlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "readlink", broken)

    assert _refused(layout["repo"] / "report.txt", _session(layout), REASON_UNVERIFIED).path is None


def test_too_broad_cwd_is_not_a_root(layout: dict[str, Path]) -> None:
    for cwd in ("/", str(Path.home()), str(layout["root"])):
        policy = session_policy(layout["home"], workspace=layout["workspace"], cwd=cwd)
        assert policy.allowed_roots == (layout["workspace"],)


def test_relative_path_is_refused(layout: dict[str, Path]) -> None:
    _refused("report.txt", _session(layout), REASON_OUTSIDE_ALLOWED_ROOTS)


def test_file_properties_keep_their_own_reasons(layout: dict[str, Path]) -> None:
    policy = _session(layout)
    (layout["repo"] / "empty.txt").touch()
    (layout["repo"] / "big.bin").write_bytes(b"x" * (_LIMIT + 1))
    (layout["repo"] / "dir").mkdir()

    _refused(layout["repo"] / "missing.txt", policy, REASON_NOT_FOUND)
    _refused(layout["repo"] / "empty.txt", policy, REASON_EMPTY)
    assert _refused(layout["repo"] / "big.bin", policy, REASON_TOO_LARGE).size_bytes == _LIMIT + 1
    _refused(layout["repo"] / "dir", policy, REASON_NOT_REGULAR)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_fifo_is_refused_without_blocking(layout: dict[str, Path]) -> None:
    fifo = layout["repo"] / "pipe"
    os.mkfifo(fifo)

    _refused(fifo, _session(layout), REASON_NOT_REGULAR)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_symlink_swapped_in_after_the_check_is_refused(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = layout["repo"] / "report.txt"
    target.write_text("approved content")
    real_decide = policy_module._decide
    swapped: list[bool] = []

    def decide_then_swap(path: Path, policy) -> None:
        real_decide(path, policy)
        if not swapped:
            swapped.append(True)
            target.unlink()
            target.symlink_to(layout["home"] / "secrets" / "lark.json")

    monkeypatch.setattr(policy_module, "_decide", decide_then_swap)

    _refused(target, _session(layout), REASON_SYMLINK)


@pytest.mark.skipif(
    not (sys.platform == "darwin" or sys.platform.startswith("linux")),
    reason="needs the descriptor's own path",
)
def test_parent_directory_swapped_after_the_check_is_judged_where_it_landed(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # O_NOFOLLOW guards only the last component; a parent directory swapped
    # for a symlink must still be caught by re-checking the opened fd.
    nested = layout["repo"] / "out"
    nested.mkdir()
    (nested / "lark.json").write_text("approved content")
    real_decide = policy_module._decide
    swapped: list[bool] = []

    def decide_then_swap(path: Path, policy) -> None:
        real_decide(path, policy)
        if not swapped:
            swapped.append(True)
            (nested / "lark.json").unlink()
            nested.rmdir()
            nested.symlink_to(layout["home"] / "secrets")

    monkeypatch.setattr(policy_module, "_decide", decide_then_swap)

    refusal = _refused(nested / "lark.json", _session(layout), REASON_HYPRIAL_HOME)
    assert refusal.path is None


def test_growth_during_the_read_is_refused(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = layout["repo"] / "log.txt"
    target.write_text("12345")
    real_read = policy_module._read_exact

    def read_then_append(fd: int, size: int) -> bytes:
        data = real_read(fd, size)
        with target.open("a") as handle:
            handle.write("more")
        return data

    monkeypatch.setattr(policy_module, "_read_exact", read_then_append)

    _refused(target, _session(layout), REASON_CHANGED)


@pytest.mark.parametrize("root", ["workspace", "repo"])
def test_session_refuses_a_hardlink_to_a_secret(
    layout: dict[str, Path], root: str
) -> None:
    """A hard link has its own path: realpath, directory-identity containment
    and the descriptor re-judge all see the allowed root.  A session read of
    a multiply-linked regular file is refused, so a link created in the
    workspace (or the registered cwd) cannot exfiltrate ``secrets/``."""

    link = layout[root] / "innocent.json"
    try:
        os.link(layout["home"] / "secrets" / "lark.json", link)
    except OSError as error:  # pragma: no cover - cross-device tmp layouts
        pytest.skip(f"hard links unavailable here: {error}")

    refusal = _refused(link, _session(layout), policy_module.REASON_HARDLINK)
    assert "node-secret" not in str(refusal)
    # Refusals never echo the path: the link may name a secret.
    assert refusal.path is None
    assert str(link) not in str(refusal)


def test_operator_policy_is_not_changed_by_the_hardlink_rule(
    layout: dict[str, Path],
) -> None:
    """The local operator may read anywhere outside the home already; the
    hardlink rule only narrows session policies."""

    target = layout["outside"] / "shared.txt"
    target.write_text("operator data")
    os.link(target, layout["outside"] / "shared-alias.txt")

    data = read_authorized_file(
        layout["outside"] / "shared-alias.txt",
        operator_policy(layout["home"]),
        max_bytes=_LIMIT,
    )
    assert data.data == b"operator data"
