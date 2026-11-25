"""import-app-dev-id.sh end to end with the real macOS tools (Darwin only).

Skipped off macOS, like the repository's other Darwin-only tests. The same
isolation as test_identity_setup_macos.py: every call runs with a
from-nothing environment whose HOME is under the test's temp dir, the
"login" keychain is a temp keychain at ``<home>/Library/Keychains/login.keychain-db``
and is also passed with ``--keychain``. Run 1 makes the key and CSR and is
stopped at the certificate prompt; the test then makes a self-signed
code-signing placeholder from that CSR's key with /usr/bin/openssl and puts
Apple's G2 intermediate (the copy in identity_fakes) where the guide keeps it,
so no network is used. Run 2 resumes: it skips the key, takes the
certificate path, imports (``--allow-other-type``), declines notarization,
checks signing and keeps the private directory. Run 3 finds the
certificate in place and the identity installed, skips the import; the
test locked the keychain before it (as a new SSH session would find it), so
the check fails, the guide runs the import again to unlock it, the check
passes and the private directory is deleted. The account's keychain search list and
default keychain must be unchanged.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.identity_fakes import BASH, G2_PEM, isolated_env
from tests.tooling.release.shell.signing.test_identity_setup_macos import EXTENSIONS, OPENSSL, _create_keychain, _identities
from tests.tooling.release.shell.signing.wizard.wizard_support import AGAIN, EMAIL, WIZARD

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real keychain and codesign are macOS only")


def _wizard(home: Path, answers: list[str], *flags: str) -> subprocess.CompletedProcess:
    env = isolated_env(home)
    env["TMPDIR"] = str(home.parent / "tmp")
    text = "".join(line + "\n" for line in answers)
    return subprocess.run([BASH, str(WIZARD), *flags], input=text, capture_output=True, text=True, env=env, check=False, timeout=180)


def test_the_guide_with_a_placeholder_identity_stops_resumes_and_finishes(work: tuple[Path, list[tuple[Path, Path]]]) -> None:
    root, created = work
    account = root / "account"
    (account / "Library" / "Keychains").mkdir(parents=True)
    (root / "tmp").mkdir(mode=0o700)
    login = account / "Library" / "Keychains" / "login.keychain-db"
    password = "w " + secrets.token_hex(16) + ' "q" \\ $x'
    _create_keychain(account, login, password)
    created.append((account, login))
    flags = ("--keychain", str(login), "--allow-other-type")
    private = account / "devid-private"

    first = _wizard(account, ["", "", EMAIL, "Hyprial Placeholder Test", "y", "q"], *flags)
    assert first.returncode == 1, first.stdout + first.stderr
    assert AGAIN in first.stderr and "-----BEGIN CERTIFICATE REQUEST-----" in first.stdout
    assert oct((private / "devid.key").stat().st_mode & 0o777) == "0o600"

    extensions = root / "ext.cnf"
    extensions.write_text(EXTENSIONS, encoding="utf-8")
    placeholder = root / "placeholder.cer"
    subprocess.run(
        [OPENSSL, "x509", "-req", "-in", str(private / "devid.csr"), "-signkey", str(private / "devid.key"), "-days", "1", "-extfile", str(extensions), "-outform", "DER", "-out", str(placeholder)],
        capture_output=True, env=isolated_env(account), check=True, timeout=60,
    )
    sha1 = subprocess.run([OPENSSL, "x509", "-inform", "DER", "-in", str(placeholder), "-noout", "-fingerprint", "-sha1"], capture_output=True, text=True, check=True, timeout=30).stdout.strip().split("=", 1)[1].replace(":", "")
    (private / "DeveloperIDG2CA.cer").write_text(G2_PEM, encoding="utf-8")

    second = _wizard(account, ["", "", "n", str(placeholder), password, "n", ""], *flags)
    assert second.returncode == 0, second.stdout + second.stderr
    text = " ".join(second.stdout.split())
    assert "skipping key and CSR generation" in text
    assert "Using the Apple intermediate already saved" in text
    assert "non-interactive signing: PASS" in text and "check: PASS" in text
    assert f"identity: {sha1}" in text and text.endswith("Done.")
    assert password not in second.stdout + second.stderr
    assert (private / "devid.key").exists()
    assert _identities(account, login, sha1) == 1

    locked = subprocess.run(["/usr/bin/security", "lock-keychain", str(login)], capture_output=True, env=isolated_env(account), check=False, timeout=30)
    assert locked.returncode == 0, locked.stderr
    third = _wizard(account, ["", "", "n", "r", password, "yes"], *flags)
    assert third.returncode == 0, third.stdout + third.stderr
    resumed = " ".join(third.stdout.split())
    assert "it matches the key; using it" in resumed
    assert "already in the keychain: skipping the import" in resumed
    assert "non-interactive signing: FAIL" in resumed and "usually the locked keychain" in resumed
    assert "non-interactive signing: PASS" in resumed
    assert "check: PASS" in resumed and resumed.endswith("Done.")
    assert not private.exists()
    assert _identities(account, login, sha1) == 1
    assert list((root / "tmp").iterdir()) == []
