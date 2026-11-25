"""Test-only: a self-signed placeholder code-signing identity in a throwaway keychain.

The release command (packaging/clients/sign_release.py) signs with an
identity the operator already holds; it creates no keychain. Its macOS tests
still need an identity that codesign accepts, so this helper makes one per
test and deletes it afterwards. Decisions (PR #2899 review):

- The keychain lives at ``<work>/signing.keychain-db`` and is reached
  through ``<work>/home``: every ``security`` / ``codesign`` child runs with
  ``HOME=<work>/home``, whose keychains directory holds a symlink to it
  under the login keychain's file name (``home_keychain``). With no
  security preferences under that home, that
  keychain is the child's default keychain and whole search list (measured
  on macOS 26.5.2; ``codesign --keychain <file>`` alone answers "no
  identity found"). The account's own list and default keychain are never
  changed.
- ``set-keychain-settings -l -u -t 600``: the keychain locks on sleep and
  after 600 seconds, the release command's step timeout, so a test killed
  with SIGKILL does not leave an unlocked keychain for an hour.
- The key is imported with ``-T /usr/bin/codesign`` (never ``-A``) and the
  partition list ``apple-tool:,apple:``, so codesign uses it without a
  prompt and nothing else does.
- The password is random per keychain and passed in argv: the keychain
  holds only a one-day self-signed placeholder and is deleted at the end.
- The work directory must be absolute, an existing real directory owned by
  the caller with no access for group or others, empty, and outside the
  account's ``~/Library``.
- Tool failures, timeouts and file errors (``FileNotFoundError`` for a
  missing tool, ``OSError`` from the symlink) all surface as
  ``KeychainError``; cleanup ignores SIGTERM, SIGHUP and SIGINT while it
  runs and restores the previous handlers afterwards.
"""

from __future__ import annotations

import os
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
from pathlib import Path
from typing import Callable

SECURITY = "/usr/bin/security"
OPENSSL = "/usr/bin/openssl"
CODESIGN = "/usr/bin/codesign"
CHILD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
LOCK_SECONDS = 600
PLACEHOLDER_CN = "Hyprial Placeholder Signing"
IDENTITY_LINE = re.compile(r'^\s*\d+\)\s+([0-9A-F]{40})\s+"(.*)"')
PLACEHOLDER_CONFIG = f"""[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = {PLACEHOLDER_CN}
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
"""
CLEANUP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

Runner = Callable[..., subprocess.CompletedProcess]


class KeychainError(Exception):
    """The placeholder keychain could not be created or used."""


def account_home() -> Path:
    """The account's home from the password database, whatever $HOME says."""

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


class PlaceholderKeychain:
    """One throwaway keychain with one self-signed code-signing identity."""

    def __init__(self, work: Path, *, run: Runner = subprocess.run, require_empty: bool = True) -> None:
        self.work = Path(work)
        self.run = run
        self.require_empty = require_empty
        self.sha1 = ""
        self._password = ""
        self._checked = False

    @property
    def keychain(self) -> Path:
        return self.work / "signing.keychain-db"

    @property
    def home(self) -> Path:
        return self.work / "home"

    @property
    def home_keychain(self) -> Path:
        return self.home / "Library" / "Keychains" / "login.keychain-db"

    def _check_work(self) -> None:
        work = self.work
        if not work.is_absolute():
            raise KeychainError("work directory must be absolute")
        if work.is_symlink() or not work.is_dir():
            raise KeychainError("work directory must be an existing directory, not a symlink")
        info = work.stat()
        if info.st_uid != os.getuid():
            raise KeychainError("work directory must be owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise KeychainError("work directory must give group or others no access")
        library = (account_home() / "Library").resolve()
        resolved = work.resolve()
        if resolved == library or library in resolved.parents:
            raise KeychainError("work directory must be outside the account's ~/Library")
        if self.require_empty and any(work.iterdir()):
            raise KeychainError("work directory must be empty")
        self._checked = True

    def _call(self, argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        child = {"HOME": str(self.home), "PATH": CHILD_PATH, "TMPDIR": str(self.work / "tmp"), "LANG": "C"}
        child.update(env or {})
        try:
            result = self.run(argv, capture_output=True, text=True, env=child, timeout=LOCK_SECONDS, check=False)
        except subprocess.TimeoutExpired as error:
            raise KeychainError(f"{Path(argv[0]).name} {argv[1]}: timeout") from error
        except OSError as error:
            raise KeychainError(f"{Path(argv[0]).name} {argv[1]}: {error}") from error
        if result.returncode != 0:
            raise KeychainError(f"{Path(argv[0]).name} {argv[1]}: rc={result.returncode} {(result.stdout + result.stderr).strip()[-300:]}")
        return result

    def create(self) -> str:
        """Create the keychain and the identity; return the identity's SHA-1."""

        self._check_work()
        identity = self.work / "identity"
        try:
            for directory in (self.home_keychain.parent, identity, self.work / "tmp"):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise KeychainError(f"mkdir: {error}") from error
        self._password = secrets.token_hex(32)
        keychain = str(self.keychain)
        self._call([SECURITY, "create-keychain", "-p", self._password, keychain])
        try:
            self.home_keychain.symlink_to(self.keychain)
        except OSError as error:
            raise KeychainError(f"symlink into the isolated home: {error}") from error
        self._call([SECURITY, "set-keychain-settings", "-l", "-u", "-t", str(LOCK_SECONDS), keychain])
        self._call([SECURITY, "unlock-keychain", "-p", self._password, keychain])
        config, key, cert, p12 = identity / "placeholder.cnf", identity / "key.pem", identity / "cert.pem", identity / "identity.p12"
        try:
            config.write_text(PLACEHOLDER_CONFIG, encoding="utf-8")
        except OSError as error:
            raise KeychainError(f"config: {error}") from error
        self._call([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-config", str(config)])
        p12_password = secrets.token_hex(24)
        self._call([OPENSSL, "pkcs12", "-export", "-inkey", str(key), "-in", str(cert), "-out", str(p12), "-passout", "env:HKS_P12_PASSWORD"], env={"HKS_P12_PASSWORD": p12_password})
        try:
            self._call([SECURITY, "import", str(p12), "-k", keychain, "-f", "pkcs12", "-P", p12_password, "-T", CODESIGN])
        finally:
            for path in (key, cert, p12):
                path.unlink(missing_ok=True)
        self._call([SECURITY, "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", self._password, keychain])
        listing = self._call([SECURITY, "find-identity", "-p", "codesigning", keychain]).stdout
        section = listing.split("Valid identities only", 1)[0]
        found = sorted({match.group(1) for match in map(IDENTITY_LINE.match, section.splitlines()) if match})
        if len(found) != 1:
            raise KeychainError(f"expected exactly one identity in the placeholder keychain, found {len(found)}")
        self.sha1 = found[0]
        return self.sha1

    def delete(self) -> dict[str, bool]:
        """Delete the keychain and the work directory with termination signals ignored.

        A work directory that was refused is never touched.
        """

        if not self._checked:
            return {"keychain_deleted": True, "work_dir_removed": False}
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in CLEANUP_SIGNALS}
        try:
            if self.keychain.exists():
                try:
                    self._call([SECURITY, "delete-keychain", str(self.keychain)])
                except KeychainError:
                    self.keychain.unlink(missing_ok=True)
            shutil.rmtree(self.work, ignore_errors=True)
            return {"keychain_deleted": not self.keychain.exists(), "work_dir_removed": not self.work.exists()}
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def __enter__(self) -> "PlaceholderKeychain":
        try:
            self.create()
        except BaseException:
            self.delete()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.delete()
