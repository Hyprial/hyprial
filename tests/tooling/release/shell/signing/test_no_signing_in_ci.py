"""Owner direction on PR #2899 (2026-11-17): signing never enters CI.

The Developer ID certificate stays on the packaging Mac and the release is
signed and notarized by hand with ``packaging/clients/sign_release.py``.
These checks keep a workflow from quietly growing a signing step again: no
workflow names a signing credential (Tauri signs and notarizes by itself
when the ``APPLE_*`` variables are present), runs the release command,
``notarytool``, ``stapler`` or a non-ad-hoc ``codesign --sign``, and no job
on the release Mac reads a secret other than its checkout token. They are a
regression guard for this repository's files, not a security boundary: a
writer can push a branch with any workflow (KP-100).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from tests.tooling.release.shell.support import PACKAGING, ROOT

WORKFLOW_DIR = ROOT / ".forgejo" / "workflows"
TAURI_CONF = ROOT / "clients" / "native" / "src-tauri" / "tauri.conf.json"
SIGNING_HELPERS = (PACKAGING / "sign_release.py", Path(__file__).resolve().parent / "placeholder_keychain.py")
SECRET_REFERENCE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
FORBIDDEN_IN_WORKFLOWS = (
    "APPLE_",
    "notarytool",
    "stapler",
    "altool",
    "keychain-profile",
    "sign_release",
    "sign_keychain",
    "import-certificate",
    "security import",
    "create-keychain",
)
CODESIGN_SIGN = re.compile(r"codesign\b[^\n]*?(?:--sign|\s-s)(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)")
FORBIDDEN_KEYCHAIN_COMMANDS = (
    "default-keychain -s",
    "default-keychain -d user -s",
    "list-keychains -s",
    "list-keychains -d user -s",
)


def _workflows() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def _code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]


def test_the_signed_workflow_is_gone() -> None:
    assert not (WORKFLOW_DIR / "shell-macos-signed.yml").exists()
    assert not (PACKAGING / "sign_keychain.py").exists()


def test_no_workflow_names_a_signing_credential_or_runs_a_signing_tool() -> None:
    for path in _workflows():
        for line in _code_lines(path):
            for forbidden in FORBIDDEN_IN_WORKFLOWS:
                assert forbidden not in line, f"{path.name}: {line.strip()}"


def test_codesign_in_a_workflow_signs_only_ad_hoc() -> None:
    for path in _workflows():
        for line in _code_lines(path):
            for identity in CODESIGN_SIGN.findall(line):
                assert identity.strip("\"'") == "-", f"{path.name}: {line.strip()}"


def test_the_codesign_pattern_catches_a_real_identity() -> None:
    assert CODESIGN_SIGN.findall('codesign --force --sign "Developer ID Application: X" app') == ['"Developer ID Application: X"']
    assert CODESIGN_SIGN.findall("codesign -s ABCDEF0123 app") == ["ABCDEF0123"]
    assert CODESIGN_SIGN.findall("codesign --sign=- app") == ["-"]
    assert CODESIGN_SIGN.findall("codesign --verify --deep --strict app") == []


def test_jobs_on_the_release_mac_read_only_the_job_token() -> None:
    for path in _workflows():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in (document.get("jobs") or {}).items():
            if "release-macos-arm64" in str(job.get("runs-on", "")):
                names = set(SECRET_REFERENCE.findall(yaml.safe_dump(job)))
                assert names <= {"GITHUB_TOKEN"}, f"{path.name}::{name}"


def test_the_tauri_build_signs_ad_hoc_only() -> None:
    macos = json.loads(TAURI_CONF.read_text(encoding="utf-8"))["bundle"]["macOS"]
    assert macos["signingIdentity"] == "-"


def test_no_workflow_or_signing_code_changes_the_account_keychains() -> None:
    for path in [*_workflows(), *SIGNING_HELPERS]:
        for line in _code_lines(path):
            for forbidden in FORBIDDEN_KEYCHAIN_COMMANDS:
                assert forbidden not in line, f"{path.name}: {line.strip()}"


def test_only_the_test_keychain_helper_names_a_login_keychain_and_only_in_its_isolated_home() -> None:
    for path in [*_workflows(), PACKAGING / "sign_release.py"]:
        assert not any("login.keychain" in line or "Library/Keychains" in line for line in _code_lines(path)), path.name
    helper = [line.strip() for line in _code_lines(SIGNING_HELPERS[1]) if "login.keychain" in line]
    assert helper == ['return self.home / "Library" / "Keychains" / "login.keychain-db"']
