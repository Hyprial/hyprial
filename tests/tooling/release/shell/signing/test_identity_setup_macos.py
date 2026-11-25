"""setup-macos-signing-identity.sh end to end with the real macOS tools (Darwin only).

Skipped off macOS, the way the repository's other Darwin-only tests are.
Nothing here touches the account's keychains: every ``security`` and
``codesign`` call, the script's included, runs with a from-nothing environment
whose HOME is a directory under the test's temp dir. With no security
preferences under such a HOME its search list is that HOME's
``Library/Keychains/login.keychain-db`` alone, and ``security create-keychain``
of a keychain elsewhere does not add it (measured 2026-11-18, macOS 26.5.2).
The "account" keychain is created as ``<home>/Library/Keychains/login.keychain-db``
so the script's default path and the search-list branch of ``check`` are the
ones exercised; a second keychain lies outside that HOME, so it is not on its
search list and ``check`` has to sign through its isolated-HOME branch. The certificate is a test-time self-signed
code-signing certificate made from the CSR's key with ``/usr/bin/openssl``,
so ``import`` runs with ``--allow-other-type``; Apple's G2 intermediate is
given offline (``--intermediate``) from the copy in ``identity_fakes``. The
account's keychain search list and default keychain are read before and
after (read-only) and must be identical.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.identity_fakes import BASH, G2_PEM, SCRIPT, isolated_env

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real keychain and codesign are macOS only")

SECURITY = "/usr/bin/security"
OPENSSL = "/usr/bin/openssl"
EXTENSIONS = "basicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=critical,codeSigning\n"
IDENTITY = re.compile(r"^\s*\d+\)\s+([0-9A-F]{40})\s+\"")


def _security(home: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([SECURITY, *args], input=stdin, capture_output=True, text=True, env=isolated_env(home), check=False, timeout=60)


def _create_keychain(home: Path, path: Path, password: str) -> None:
    quoted = password.replace("\\", "\\\\").replace('"', '\\"')
    result = _security(home, "-i", stdin=f'create-keychain -p "{quoted}" "{path}"\n')
    assert result.returncode == 0, result.stdout + result.stderr


def _identities(home: Path, keychain: Path, sha1: str) -> int:
    listing = _security(home, "find-identity", "-p", "codesigning", str(keychain)).stdout
    section = listing.split("Valid identities only", 1)[0]
    return sum(1 for line in section.splitlines() if (match := IDENTITY.match(line)) and match.group(1) == sha1)


def _script(home: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = isolated_env(home)
    env["TMPDIR"] = str(home.parent / "tmp")
    return subprocess.run([BASH, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=env, check=False, timeout=120)


def test_csr_import_twice_and_check_with_a_placeholder_identity(work: tuple[Path, list[tuple[Path, Path]]]) -> None:
    root, created = work
    account = root / "account"
    other = root / "other"
    for home in (account, other):
        (home / "Library" / "Keychains").mkdir(parents=True)
    (root / "tmp").mkdir(mode=0o700)
    login = account / "Library" / "Keychains" / "login.keychain-db"
    separate = root / "separate.keychain-db"
    login_password = "t " + secrets.token_hex(16) + ' "q" \\ $x'
    separate_password = secrets.token_hex(16)
    _create_keychain(account, login, login_password)
    created.append((account, login))
    _create_keychain(other, separate, separate_password)
    created.append((other, separate))

    private = root / "private"
    made = _script(account, "csr", "--dir", str(private), "--email", "dev@example.invalid", "--common-name", "Hyprial Placeholder Test")
    assert made.returncode == 0, made.stdout + made.stderr
    assert oct((private / "devid.key").stat().st_mode & 0o777) == "0o600"

    extensions = root / "ext.cnf"
    extensions.write_text(EXTENSIONS, encoding="utf-8")
    placeholder = root / "placeholder.cer"
    subprocess.run(
        [OPENSSL, "x509", "-req", "-in", str(private / "devid.csr"), "-signkey", str(private / "devid.key"), "-days", "1", "-extfile", str(extensions), "-outform", "DER", "-out", str(placeholder)],
        capture_output=True, env=isolated_env(account), check=True, timeout=60,
    )
    fingerprint = subprocess.run([OPENSSL, "x509", "-inform", "DER", "-in", str(placeholder), "-noout", "-fingerprint", "-sha1"], capture_output=True, text=True, check=True, timeout=30)
    sha1 = fingerprint.stdout.strip().split("=", 1)[1].replace(":", "")
    intermediate = root / "g2.pem"
    intermediate.write_text(G2_PEM, encoding="utf-8")

    common = ("--dir", str(private), "--cer", str(placeholder), "--intermediate", str(intermediate), "--allow-other-type")
    first = _script(account, "import", *common, "--keychain", str(login), stdin=login_password + "\n")
    assert first.returncode == 0, first.stdout + first.stderr
    assert f"identity {sha1}" in " ".join(first.stdout.split())
    assert login_password not in first.stdout + first.stderr
    assert _identities(account, login, sha1) == 1

    second = _script(account, "import", *common, "--keychain", str(login), stdin=login_password + "\n")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already installed" in second.stdout
    assert _identities(account, login, sha1) == 1

    checked = _script(account, "check", "--identity", sha1)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "non-interactive signing: PASS" in checked.stdout
    assert "intermediate: present" in checked.stdout
    assert "not on the search list" not in checked.stdout

    into_separate = _script(account, "import", *common, "--keychain", str(separate), stdin=separate_password + "\n")
    assert into_separate.returncode == 0, into_separate.stdout + into_separate.stderr
    assert _identities(other, separate, sha1) == 1
    checked_separate = _script(account, "check", "--keychain", str(separate), "--identity", sha1)
    assert checked_separate.returncode == 0, checked_separate.stdout + checked_separate.stderr
    assert "not on the search list" in checked_separate.stdout
    assert "non-interactive signing: PASS" in checked_separate.stdout
    assert list((root / "tmp").iterdir()) == []
