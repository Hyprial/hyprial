"""Static policy of scripts/build/setup-macos-signing-identity.sh.

The script installs a Developer ID signing identity on the packaging Mac by
hand (owner request 2026-11-18). These checks run on every platform and pin
what must never appear in it: changes to the account's keychain search list
or default keychain, an import that opens the key to every application
(``-A``), a password option, shell tracing that would print a secret, or a
workflow that runs it (signing never enters CI, KP-100). They also pin the
SHA-256 of Apple's Developer ID G2 intermediate and check it against the
copy of that certificate kept in ``identity_fakes``.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import ssl
import subprocess

import pytest

from tests.tooling.release.shell.signing.identity_fakes import BASH, G2_PEM, PIN, SCRIPT
from tests.tooling.release.shell.support import ROOT

PIN_LINE = re.compile(r'^DEVELOPER_ID_G2_SHA256="([0-9A-F]{64})"$', re.M)


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _code_lines() -> list[str]:
    return [line for line in _text().splitlines() if not line.lstrip().startswith("#")]


def test_the_script_parses_with_bash_n() -> None:
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed on this machine")
def test_shellcheck_is_clean() -> None:
    result = subprocess.run(["shellcheck", "--shell=bash", "--severity=style", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "forbidden",
    ["list-keychains -s", "list-keychains -d user -s", "default-keychain -s", "default-keychain -d user -s", "set -x", "set -o xtrace", "--password", "<<<", "<<"],
)
def test_forbidden_constructs_are_absent(forbidden: str) -> None:
    for line in _code_lines():
        assert forbidden not in line, line.strip()


def test_no_import_opens_the_key_to_every_application() -> None:
    imports = [line for line in _code_lines() if re.search(r"\bsecurity[ _]import\b", line) and "()" not in line]
    assert any('security import "$@"' in line for line in imports), imports
    for target in ("devid.key", "certificate.der", "g2.der"):
        assert any(target in line for line in imports), target
    for line in imports:
        assert not re.search(r"(^|\s)-A(\s|$)", line), line.strip()
        assert not re.search(r"(^|\s)-P(\s|$)", line), line.strip()
    assert "-T /usr/bin/codesign" in _text()


def test_echo_is_off_before_the_password_prompt_is_printed() -> None:
    lines = _code_lines()
    prompt = next(index for index, line in enumerate(lines) if "Password of keychain" in line)
    read = next(index for index, line in enumerate(lines) if "read -r -s SECRET" in line)
    off = [index for index, line in enumerate(lines) if line.strip() == "stty -echo"]
    restore = [index for index, line in enumerate(lines) if line.strip() == 'stty "${STTY_SAVED}"']
    assert off and off[0] < prompt < read < restore[0]
    assert any('stty "${STTY_SAVED}" 2>/dev/null || true' in line for line in lines)


def test_the_intermediate_pin_is_present_well_formed_and_matches_apples_certificate() -> None:
    pins = PIN_LINE.findall(_text())
    assert pins == [PIN]
    der = ssl.PEM_cert_to_DER_cert(G2_PEM)
    assert hashlib.sha256(der).hexdigest().upper() == PIN
    assert "https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer" in _text()


def test_the_header_states_the_shared_runner_consequence_and_the_search_list_measurement() -> None:
    header = _text().split("\nset -euo pipefail", 1)[0]
    for phrase in ("KP-100", "same macOS account", "any workflow", "--keychain", "search list", "SSH"):
        assert phrase in header, phrase


def test_no_workflow_runs_the_script() -> None:
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        assert "setup-macos-signing-identity" not in path.read_text(encoding="utf-8"), path.name


def test_help_exits_0_everywhere() -> None:
    for args in ([], ["--help"], ["-h"], ["csr", "--help"], ["import", "--help"], ["notary", "--help"], ["check", "--help"]):
        result = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, check=False, timeout=30)
        if args:
            assert result.returncode == 0, (args, result.stdout, result.stderr)
            assert "usage" in result.stdout.lower(), args
        else:
            assert result.returncode == 2, result.stdout


def test_unknown_subcommand_exits_2() -> None:
    result = subprocess.run([BASH, str(SCRIPT), "frobnicate"], capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 2
    assert "unknown subcommand" in result.stderr


def test_top_level_help_names_the_ssh_and_runner_caveats() -> None:
    result = subprocess.run([BASH, str(SCRIPT), "--help"], capture_output=True, text=True, check=False, timeout=30)
    text = " ".join(result.stdout.split())
    assert "locked in an SSH session" in text
    assert "UNKNOWN" in text and "runner" in text


WIZARD = ROOT / "scripts" / "build" / "import-app-dev-id.sh"


def _wizard_code_lines() -> list[str]:
    return [line for line in WIZARD.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]


def test_the_guide_parses_and_is_executable() -> None:
    result = subprocess.run([BASH, "-n", str(WIZARD)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert WIZARD.stat().st_mode & 0o111


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed on this machine")
def test_shellcheck_is_clean_on_the_guide() -> None:
    result = subprocess.run(["shellcheck", "--shell=bash", "--severity=style", str(WIZARD)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "forbidden",
    ["list-keychains", "default-keychain", "set -x", "set -o xtrace", "--password", "<<", "rm -f \"${DIR}\"", "read -s"],
)
def test_the_guide_has_no_forbidden_construct(forbidden: str) -> None:
    for line in _wizard_code_lines():
        assert forbidden not in line, line.strip()


def test_the_guide_never_runs_a_keychain_crypto_or_apple_tool_itself() -> None:
    invocation = re.compile(r"(^|[;&|(]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(security|openssl|codesign|xcrun|curl|base64)\b")
    for line in _wizard_code_lines():
        code = re.sub(r"'[^']*'", "''", re.sub(r'"[^"]*"', '""', line.strip()))
        assert not invocation.search(code), line.strip()


def test_the_guide_removes_recursively_only_its_own_temp_directory() -> None:
    recursive = [line.strip() for line in _wizard_code_lines() if re.search(r"\brm\s+-[a-z]*r", line)]
    assert recursive == ['rm -rf "${WORK}"']


def test_the_guide_drives_the_one_implementation() -> None:
    code = "\n".join(_wizard_code_lines())
    assert 'SETUP="${HERE}/setup-macos-signing-identity.sh"' in code
    for subcommand in ("status", "csr", "intermediate", "import", "notary", "check"):
        assert f"setup {subcommand} " in code, subcommand


def test_the_guide_never_suggests_unwrapped_base64() -> None:
    code = "\n".join(_wizard_code_lines())
    assert "base64 -b 64 -i developerID_application.cer" in code
    assert re.findall(r"\bbase64 -i\s", code) == []


def test_no_workflow_runs_the_guide() -> None:
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        assert "import-app-dev-id" not in path.read_text(encoding="utf-8"), path.name


def test_the_guide_help_exits_0_without_tools() -> None:
    result = subprocess.run([BASH, str(WIZARD), "--help"], capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0 and "usage: ./scripts/build/import-app-dev-id.sh" in result.stdout
