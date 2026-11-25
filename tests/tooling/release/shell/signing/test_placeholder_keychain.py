"""The test-only placeholder keychain, with recorded tools, on every platform.

placeholder_keychain.py holds a self-signed identity for the macOS tests of
sign_release.py. These pin the review fixes of PR #2899 on that path: the
keychain locks itself after the step timeout, the key is usable by codesign
only (`-T /usr/bin/codesign`, never `-A`), an unsafe work directory is
refused, tool and file errors come back as one error type, and cleanup runs
with termination signals ignored.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing import placeholder_keychain
from tests.tooling.release.shell.support import load

sign_release = load("sign_release")
SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"


class Tools:
    def __init__(self, *, fail_on: str | None = None, missing: str | None = None, on_call=None) -> None:
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.missing = missing
        self.on_call = on_call

    @staticmethod
    def name(argv: list[str]) -> str:
        return argv[1] if Path(argv[0]).name == "security" else f"openssl {argv[1]}"

    def __call__(self, argv, *, env=None, timeout=None, **_kwargs) -> subprocess.CompletedProcess:
        argv = [str(arg) for arg in argv]
        name = self.name(argv)
        self.calls.append({"name": name, "argv": argv, "env": dict(env or {}), "timeout": timeout})
        if self.on_call:
            self.on_call(name)
        if name == self.missing:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        if name == self.fail_on:
            return subprocess.CompletedProcess(argv, 1, "", f"{name} failed")
        stdout = ""
        if name == "create-keychain":
            Path(argv[-1]).write_bytes(b"keychain")
        elif name == "delete-keychain":
            Path(argv[-1]).unlink()
        elif name == "openssl req":
            for flag in ("-keyout", "-out"):
                Path(argv[argv.index(flag) + 1]).write_text("pem", encoding="utf-8")
        elif name == "openssl pkcs12":
            Path(argv[argv.index("-out") + 1]).write_bytes(b"p12")
        elif name == "find-identity":
            stdout = f'  1) {SHA1} "{placeholder_keychain.PLACEHOLDER_CN}" (CSSMERR_TP_NOT_TRUSTED)\n     1 identities found\n\n  Valid identities only\n     0 valid identities found\n'
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def of(self, name: str) -> list[str]:
        return next(call["argv"] for call in self.calls if call["name"] == name)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir(mode=0o700)
    return path


def test_the_lock_timeout_is_the_release_step_timeout_and_auto_lock_stays_on(work: Path) -> None:
    tools = Tools()
    with placeholder_keychain.PlaceholderKeychain(work, run=tools) as keychain:
        assert keychain.sha1 == SHA1
    settings = tools.of("set-keychain-settings")
    assert settings == [placeholder_keychain.SECURITY, "set-keychain-settings", "-l", "-u", "-t", "600", str(work / "signing.keychain-db")]
    assert placeholder_keychain.LOCK_SECONDS == sign_release.STEP_TIMEOUT == 600


def test_only_codesign_may_use_the_key(work: Path) -> None:
    tools = Tools()
    with placeholder_keychain.PlaceholderKeychain(work, run=tools):
        pass
    imported = tools.of("import")
    assert imported[imported.index("-T") + 1] == "/usr/bin/codesign"
    assert "-A" not in imported
    partition = tools.of("set-key-partition-list")
    assert partition[partition.index("-S") + 1] == "apple-tool:,apple:"


def test_every_security_call_runs_in_the_isolated_home_and_all_is_deleted(work: Path) -> None:
    tools = Tools()
    with placeholder_keychain.PlaceholderKeychain(work, run=tools) as keychain:
        assert keychain.home_keychain.is_symlink()
        assert keychain.home_keychain.readlink() == work / "signing.keychain-db"
    for call in tools.calls:
        assert call["env"]["HOME"] == str(work / "home"), call["name"]
        assert call["timeout"] == placeholder_keychain.LOCK_SECONDS
    assert tools.calls[-1]["name"] == "delete-keychain"
    assert not work.exists()


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770, 0o777])
def test_a_work_directory_open_to_group_or_others_is_refused(work: Path, mode: int) -> None:
    work.chmod(mode)
    tools = Tools()
    with pytest.raises(placeholder_keychain.KeychainError, match="group or others"):
        placeholder_keychain.PlaceholderKeychain(work, run=tools).create()
    assert tools.calls == []


def test_unsafe_work_directories_are_refused(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir(mode=0o700)
    (nonempty / "x").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(nonempty)
    for path, message in ((Path("relative"), "absolute"), (tmp_path / "missing", "existing directory"), (link, "existing directory"), (nonempty, "empty")):
        with pytest.raises(placeholder_keychain.KeychainError, match=message):
            placeholder_keychain.PlaceholderKeychain(path, run=Tools()).create()


@pytest.mark.parametrize("failure", [{"missing": "create-keychain"}, {"fail_on": "import"}, {"missing": "openssl req"}])
def test_tool_and_file_errors_come_back_as_one_error_and_everything_is_deleted(work: Path, failure: dict) -> None:
    tools = Tools(**failure)
    with pytest.raises(placeholder_keychain.KeychainError):
        with placeholder_keychain.PlaceholderKeychain(work, run=tools):
            pass
    assert not work.exists()


def test_a_symlink_that_cannot_be_made_is_a_keychain_error(work: Path) -> None:
    (work / "home" / "Library" / "Keychains").mkdir(parents=True, mode=0o700)
    (work / "home" / "Library" / "Keychains" / "login.keychain-db").write_text("in the way", encoding="utf-8")
    keychain = placeholder_keychain.PlaceholderKeychain(work, run=Tools(), require_empty=False)
    with pytest.raises(placeholder_keychain.KeychainError, match="symlink"):
        keychain.create()
    keychain.delete()
    assert not work.exists()


def test_cleanup_ignores_termination_signals_and_restores_them(work: Path) -> None:
    seen: list[object] = []

    def watch(name: str) -> None:
        if name == "delete-keychain":
            seen.extend(signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT))

    previous = signal.getsignal(signal.SIGTERM)
    with placeholder_keychain.PlaceholderKeychain(work, run=Tools(on_call=watch)):
        pass
    assert seen == [signal.SIG_IGN] * 3
    assert signal.getsignal(signal.SIGTERM) == previous
