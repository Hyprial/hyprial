"""Path safety of the native credential writer (review of 95c1b8b45).

B2: every directory the seed creates under the agent home must be 0700, or the
runtime's private-directory check refuses the first start of a fresh agent.
B3: an agent can replace ``secrets`` or ``secrets/native`` with a symlink; the
writer and the remover must never follow it into another agent's home.
M1: a refusal never carries the incoming document.
S3: a failure while writing leaves nothing new behind, and a leftover
``.incoming`` staging file (it may hold plaintext) is not left in place.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.home.native_credential import (
    NativeCredentialError,
    install_native_credential_set,
    native_secrets_root,
    remove_native_credentials,
)
from hyprial.identity.impl.agents.runtime.context import _ensure_private_directory
from hyprial.kernel import ipc_errors

SECRET = b'{"k":"never-echo-sk-ant-oat-0123456789"}'
PI_SET = [("auth.json", b'{"auth": "pi-secret"}'), ("models-store.json", b'{"s": 1}')]


def _home(tmp_path: Path, name: str = "agent-a") -> Path:
    home = tmp_path / name
    home.mkdir(mode=0o700)
    (home / "secrets").mkdir(mode=0o700)
    return home


@pytest.fixture
def loose_umask():  # type: ignore[no-untyped-def]
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_fresh_seed_creates_every_directory_private(
    tmp_path: Path, loose_umask: None
) -> None:
    home = _home(tmp_path)

    install_native_credential_set(home, "claude", [(".credentials.json", SECRET)])

    for directory in (home / "secrets" / "native", native_secrets_root(home, "claude")):
        assert stat.S_IMODE(directory.lstat().st_mode) == 0o700, directory
    _ensure_private_directory(home, native_secrets_root(home, "claude"))


def test_fresh_seed_without_a_secrets_directory_is_private_too(
    tmp_path: Path, loose_umask: None
) -> None:
    home = tmp_path / "bare"
    home.mkdir(mode=0o700)

    install_native_credential_set(home, "codex", [("auth.json", SECRET)])

    _ensure_private_directory(home, native_secrets_root(home, "codex"))


def _link_native_to_other_agent(tmp_path: Path) -> tuple[Path, Path]:
    victim = _home(tmp_path, "agent-b")
    install_native_credential_set(victim, "claude", [(".credentials.json", b"B-OWN")])
    attacker = _home(tmp_path, "agent-a")
    (attacker / "secrets" / "native").symlink_to(victim / "secrets" / "native")
    return attacker, victim


def test_login_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    attacker, victim = _link_native_to_other_agent(tmp_path)
    victim_file = native_secrets_root(victim, "claude") / ".credentials.json"

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential_set(
            attacker, "claude", [(".credentials.json", SECRET)], force=True
        )

    assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert victim_file.read_bytes() == b"B-OWN"
    assert sorted(p.name for p in victim_file.parent.iterdir()) == [".credentials.json"]


def test_logout_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    attacker, victim = _link_native_to_other_agent(tmp_path)
    victim_file = native_secrets_root(victim, "claude") / ".credentials.json"

    with pytest.raises(NativeCredentialError) as refused:
        remove_native_credentials(attacker, "claude")

    assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert victim_file.read_bytes() == b"B-OWN"


def test_symlinked_secrets_directory_is_refused_both_ways(tmp_path: Path) -> None:
    victim = _home(tmp_path, "agent-b")
    install_native_credential_set(victim, "codex", [("auth.json", b"B-OWN")])
    attacker = tmp_path / "agent-a"
    attacker.mkdir(mode=0o700)
    (attacker / "secrets").symlink_to(victim / "secrets")

    for operation in (
        lambda: install_native_credential_set(attacker, "codex", [("auth.json", SECRET)], force=True),
        lambda: remove_native_credentials(attacker, "codex"),
    ):
        with pytest.raises(NativeCredentialError) as refused:
            operation()
        assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert (native_secrets_root(victim, "codex") / "auth.json").read_bytes() == b"B-OWN"


def test_logout_refuses_a_symlinked_harness_directory(tmp_path: Path) -> None:
    victim = _home(tmp_path, "agent-b")
    install_native_credential_set(victim, "claude", [(".credentials.json", b"B-OWN")])
    attacker = _home(tmp_path, "agent-a")
    (attacker / "secrets" / "native").mkdir(mode=0o700)
    native_secrets_root(attacker, "claude").symlink_to(native_secrets_root(victim, "claude"))

    with pytest.raises(NativeCredentialError) as refused:
        remove_native_credentials(attacker, "claude")

    assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert (native_secrets_root(victim, "claude") / ".credentials.json").exists()


def test_a_non_private_existing_directory_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "secrets" / "native").mkdir(mode=0o700)
    os.chmod(home / "secrets" / "native", 0o755)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential_set(home, "claude", [(".credentials.json", SECRET)])

    assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert not native_secrets_root(home, "claude").exists()


def test_refusals_never_carry_the_document(tmp_path: Path) -> None:
    home = _home(tmp_path)
    install_native_credential_set(home, "claude", [(".credentials.json", b"older")])

    refusals = []
    for documents in (
        [(".credentials.json", SECRET)],
        [(".credentials.json", SECRET), ("settings.json", SECRET)],
    ):
        with pytest.raises(NativeCredentialError) as refused:
            install_native_credential_set(home, "claude", documents)
        refusals.append(refused.value)

    fragments = {SECRET[index : index + 8].decode() for index in range(len(SECRET) - 8)}
    for error in refusals:
        for text in (str(error), error.message, repr(error.args)):
            leaked = [fragment for fragment in fragments if fragment in text]
            assert leaked == [], leaked


def test_a_failed_write_leaves_no_new_file_and_no_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    real_rename = os.rename
    calls = {"n": 0}

    def failing_rename(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        real_rename(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", failing_rename)

    with pytest.raises(NativeCredentialError):
        install_native_credential_set(home, "pi", PI_SET)

    root = native_secrets_root(home, "pi")
    assert sorted(p.name for p in root.iterdir()) == []


def test_a_leftover_staging_file_does_not_block_and_is_removed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    root = native_secrets_root(home, "pi")
    root.mkdir(parents=True, mode=0o700)
    os.chmod(home / "secrets" / "native", 0o700)
    (root / "auth.json.incoming").write_bytes(b"stale plaintext")

    install_native_credential_set(home, "pi", PI_SET)

    assert sorted(p.name for p in root.iterdir()) == ["auth.json", "models-store.json"]


def test_logout_also_removes_leftover_staging_files(tmp_path: Path) -> None:
    home = _home(tmp_path)
    install_native_credential_set(home, "pi", PI_SET)
    root = native_secrets_root(home, "pi")
    (root / "auth.json.incoming").write_bytes(b"stale plaintext")

    remove_native_credentials(home, "pi")

    assert list(root.iterdir()) == []


def test_a_staging_write_failure_leaves_no_staging_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    real_write = os.write
    calls = {"n": 0}

    def failing_write(descriptor: int, data: object) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_write(descriptor, data)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "write", failing_write)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential_set(home, "pi", PI_SET)

    assert sorted(p.name for p in native_secrets_root(home, "pi").iterdir()) == []
    assert "No space left on device" in refused.value.message


def test_a_forced_replacement_that_fails_keeps_the_old_set_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    old = [("auth.json", b'{"auth": "old"}'), ("models-store.json", b'{"s": "old"}')]
    install_native_credential_set(home, "pi", old)
    root = native_secrets_root(home, "pi")
    real_rename = os.rename
    calls = {"n": 0}

    def failing_rename(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError(5, "Input/output error")
        real_rename(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", failing_rename)

    with pytest.raises(NativeCredentialError):
        install_native_credential_set(home, "pi", PI_SET, force=True)

    assert sorted(p.name for p in root.iterdir()) == ["auth.json", "models-store.json"]
    assert (root / "auth.json").read_bytes() == b'{"auth": "old"}'
    assert (root / "models-store.json").read_bytes() == b'{"s": "old"}'


def test_a_forced_replacement_leaves_no_previous_copies(tmp_path: Path) -> None:
    home = _home(tmp_path)
    install_native_credential_set(home, "pi", [("auth.json", b"a1"), ("models-store.json", b"s1")])

    install_native_credential_set(home, "pi", PI_SET, force=True)

    root = native_secrets_root(home, "pi")
    assert sorted(p.name for p in root.iterdir()) == ["auth.json", "models-store.json"]
    assert (root / "auth.json").read_bytes() == PI_SET[0][1]


def test_a_planted_staging_symlink_is_never_written_through(tmp_path: Path) -> None:
    victim = _home(tmp_path, "agent-b")
    install_native_credential_set(victim, "claude", [(".credentials.json", b"B-OWN")])
    victim_file = native_secrets_root(victim, "claude") / ".credentials.json"
    attacker = _home(tmp_path, "agent-a")
    install_native_credential_set(attacker, "claude", [(".credentials.json", b"A-OLD")])
    staging = native_secrets_root(attacker, "claude") / ".credentials.json.incoming"
    staging.symlink_to(victim_file)

    install_native_credential_set(
        attacker, "claude", [(".credentials.json", SECRET)], force=True
    )

    assert victim_file.read_bytes() == b"B-OWN"
    attacker_file = native_secrets_root(attacker, "claude") / ".credentials.json"
    assert attacker_file.read_bytes() == SECRET
    assert not staging.is_symlink() and not staging.exists()

    staging.symlink_to(victim_file)
    remove_native_credentials(attacker, "claude")
    assert victim_file.read_bytes() == b"B-OWN"
    assert not staging.is_symlink()


def test_an_agent_home_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    real = _home(tmp_path, "real-home")
    linked = tmp_path / "linked-home"
    linked.symlink_to(real)

    for operation in (
        lambda: install_native_credential_set(linked, "codex", [("auth.json", SECRET)]),
        lambda: remove_native_credentials(linked, "codex"),
    ):
        with pytest.raises(NativeCredentialError) as refused:
            operation()
        assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert not (real / "secrets" / "native").exists()


def test_an_agent_home_that_is_not_private_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    home.chmod(0o755)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential_set(home, "codex", [("auth.json", SECRET)])

    assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert not (home / "secrets" / "native").exists()


def test_a_level_owned_by_another_uid_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    (home / "secrets" / "native").mkdir(mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)

    for operation in (
        lambda: install_native_credential_set(home, "codex", [("auth.json", SECRET)]),
        lambda: remove_native_credentials(home, "codex"),
    ):
        with pytest.raises(NativeCredentialError) as refused:
            operation()
        assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
