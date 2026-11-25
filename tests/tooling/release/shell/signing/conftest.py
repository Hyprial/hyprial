"""Shared fixture of the Darwin end-to-end tests of the signing-identity scripts.

``work`` is a private temp directory plus a list the test fills with
``(home, keychain)`` pairs it created; at teardown every listed keychain is
deleted with ``security delete-keychain`` under its own isolated HOME, the
directory is removed, and the account's keychain search list and default
keychain (read with the real home from the password database, read-only)
must be what they were before the test.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.identity_fakes import isolated_env

SECURITY = "/usr/bin/security"


def _account_keychains() -> str:
    env = {"HOME": pwd.getpwuid(os.getuid()).pw_dir, "PATH": "/usr/bin:/bin", "LANG": "C"}
    listed = subprocess.run([SECURITY, "list-keychains", "-d", "user"], capture_output=True, text=True, env=env, check=True, timeout=30)
    default = subprocess.run([SECURITY, "default-keychain", "-d", "user"], capture_output=True, text=True, env=env, check=True, timeout=30)
    return listed.stdout + "---\n" + default.stdout



@pytest.fixture
def work(tmp_path: Path):
    before = _account_keychains()
    root = tmp_path / "w"
    root.mkdir(mode=0o700)
    created: list[tuple[Path, Path]] = []
    yield root, created
    for home, keychain in created:
        if keychain.exists():
            subprocess.run([SECURITY, "delete-keychain", str(keychain)], capture_output=True, env=isolated_env(home), check=False, timeout=60)
    shutil.rmtree(root, ignore_errors=True)
    assert not root.exists()
    assert _account_keychains() == before
