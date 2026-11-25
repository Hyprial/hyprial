"""Drive scripts/build/import-app-dev-id.sh with scripted answers and fake tools.

The guide reads one answer per line from stdin; ``import`` (run by the guide)
reads the keychain password from the same stdin. ``Wizard`` builds on
``identity_fakes.Host``: the same from-scratch tool PATH and fake keychain,
with ``FAKE_KEY_ID`` fixed so a test can prepare the matching certificate
before the guide makes the key. The private directory is the guide's default,
``$HOME/devid-private``.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from tests.tooling.release.shell.signing.identity_fakes import BASH, SCRIPT, Host, fake_cert
from tests.tooling.release.shell.support import ROOT

WIZARD = ROOT / "scripts" / "build" / "import-app-dev-id.sh"
KEY_ID = "wizardkey0001"
TEAM = "ABCDE12345"
SUBJECT = f"UID={TEAM}/CN=Developer ID Application: Example Developer ({TEAM})/OU={TEAM}/O=Example Developer/C=US"
EMAIL = "dev@example.invalid"
NAME = "Example Developer"
AGAIN = "Run ./scripts/build/import-app-dev-id.sh again to continue."


class Wizard(Host):
    """A fake host plus helpers for the guide."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.env["FAKE_KEY_ID"] = KEY_ID
        self.dir = self.home / "devid-private"
        self.cer = self.dir / "developerID_application.cer"

    def wizard(self, answers: list[str] | str, *flags: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run the guide; a list is joined into lines, a string is fed as is."""

        text = answers if isinstance(answers, str) else "".join(line + "\n" for line in answers)
        env = dict(self.env)
        env.update(extra or {})
        return subprocess.run([BASH, str(WIZARD), *flags], input=text, capture_output=True, text=True, env=env, timeout=120, check=False)

    def certificate(self, name: str = "downloaded.cer", **overrides: object) -> Path:
        """A fake Developer ID Application certificate for the guide's key."""

        values: dict = {"key": KEY_ID, "subject": SUBJECT, "valid": True}
        values.update(overrides)
        return fake_cert(self.tmp / name, **values)

    def make_key(self) -> None:
        """Make the key and CSR in the default directory with the underlying script."""

        result = subprocess.run(
            [BASH, str(SCRIPT), "csr", "--dir", str(self.dir), "--email", EMAIL, "--common-name", NAME],
            capture_output=True, text=True, env=self.env, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def tool_calls(self, tool: str) -> list[list[str]]:
        """The argv of every call of one fake tool."""

        return [call["argv"] for call in self.calls() if call["tool"] == tool]


def pasted(path: Path, *, wrap: int = 76) -> list[str]:
    """The lines of ``base64 -i <file>`` (wrapped), then the empty line that ends a paste."""

    text = base64.b64encode(path.read_bytes()).decode()
    return [text[index:index + wrap] for index in range(0, len(text), wrap)] + [""]


def fresh(host: Wizard, certificate: str, *, notary: list[str] | None = None, delete: str = "") -> list[str]:
    """Answers for a first run: continue, default dir, CSR fields, no CSR print, certificate, password, notary, delete."""

    return ["", "", EMAIL, NAME, "n", certificate, host.password, *(notary or ["n"]), delete]


def combined(result: subprocess.CompletedProcess) -> str:
    """stdout and stderr together, whitespace normalised."""

    return " ".join((result.stdout + result.stderr).split())
