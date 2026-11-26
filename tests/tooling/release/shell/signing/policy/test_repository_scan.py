"""Whole-repository scan for the Apple signing tools (PR #2980 review, item C).

Every tracked file is read, except Markdown (documentation, never executed:
README sections and design notes describe these commands), and in files whose
comments start with ``#`` comment lines are skipped (file-header decision
records name the tools; a ``#!`` line is code, and a file with bytes that are
not UTF-8 is read with replacement characters, review 38872). What counts as a use: ``notarytool``, ``altool``,
``stapler`` or ``spctl`` as a word; a ``security`` subcommand from the list of
``security(1)`` subcommands, in shell or Python argv form; ``codesign`` with
``--sign``/``-s`` and an identity other than ``-`` (shell form) or a Python
argv line naming ``"codesign"`` with ``"--sign"``/``"-s"`` followed by
anything but ``"-"``. A file with a use must be listed in EXEMPT with the
reason it may; every listed file must still have a use (no dead entries). New
entries only through review of this file. Exemptions are by file, never by
directory.

What it does NOT catch, stated plainly: a tool name or the switch assembled
at run time (string concatenation, a variable holding ``codesign``, base64)
in any file. Since 2026-11-20 no workflow packages or signs (owner decision:
packaging is not done in CI; both packaging workflows are deleted) and every
workflow is barred from the package script, the sign tool and the switch by
``test_signing_only_by_switch.py``; but a new script with an assembled name,
called from a workflow, or such a line inside an exempt file or the package
script, is deliberate misuse and is out of reach of a text scan (KP-100: the
control points are outside the repository).

``uses: ./...`` (a local composite action, whose steps no scan above sees in
the workflow file) is refused in every job that runs on the release Mac
(``release-macos-arm64`` and ``macos-host``, two runner processes of the same
macOS account), job-level ``uses:`` included. Workflows are read from
``.github/workflows`` and, when present, ``.github/workflows`` (PR #2980
re-review, item 3). Not covered: a remote action (``uses: owner/action@ref``)
or a remote reusable workflow on a Mac job runs code that is outside this
repository, and a ``pull_request`` job runs the workflow files of the pull
request's own head.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from tests.tooling.release.shell.support import ROOT

SUBCOMMANDS = (
    "add-certificates|add-generic-password|add-internet-password|add-trusted-cert|authorize|authorizationdb|cms|create-keychain|"
    "default-keychain|delete-certificate|delete-generic-password|delete-identity|delete-internet-password|delete-keychain|"
    "dump-keychain|dump-trust-settings|export|find-certificate|find-generic-password|find-identity|find-internet-password|import|"
    "leaks|list-keychains|lock-keychain|login-keychain|remove-trusted-cert|set-generic-password-partition-list|"
    "set-identity-preference|set-internet-password-partition-list|set-key-partition-list|set-keychain-password|"
    "set-keychain-settings|show-keychain-info|smartcard|trust-settings-export|trust-settings-import|unlock-keychain|"
    "user-trust-settings-enable|verify-cert"
)
APPLE_TOOL = re.compile(r"\b(notarytool|altool|stapler|spctl)\b")
SECURITY = re.compile(r"(?<![\w.-])security\b[\"',\s]+(?:-[a-z]\b[\"',\s]+)*(" + SUBCOMMANDS + r")\b")
CODESIGN_SHELL = re.compile(r"codesign\b[^\n]*?(?:--sign|\s-s)(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)")
CODESIGN_ARGV = re.compile(r"[\"']codesign[\"'][^\n]*[\"'](?:--sign|-s)[\"']\s*,\s*+(?![\"']-[\"'])")
HASH_COMMENTS = {".sh", ".bash", ".py", ".yml", ".yaml", ".toml", ".ps1", ""}
MAC_LABELS = {"release-macos-arm64", "macos-host"}

EXEMPT = {
    "packaging/clients/sign_bundle.py": "the Developer ID signing tool: the one place that signs, notarizes, staples and assesses",
    "packaging/clients/signing/modes.py": "sign_bundle's --check-only / --verify-only (test signature, notary history, final checks)",
    "packaging/clients/signing/notary.py": "sign_bundle's notarization (submit, history poll for the id, wait, log)",
    "scripts/build/setup-macos-signing-identity.sh": "owner-run identity setup, never in a workflow (test_identity_setup_policy.py)",
    "scripts/build/import-app-dev-id.sh": "owner-run guide driving the identity setup, never in a workflow (test_identity_setup_policy.py)",
    "tests/tooling/release/shell/candidate/test_package_script.py": "test: lists the tool names the ad-hoc script must not contain",
    "tests/tooling/release/shell/candidate/test_package_script_run.py": "test: asserts no notary call in the ad-hoc run",
    "tests/tooling/release/shell/signing/bundle/bundle_fakes.py": "test: fake codesign/xcrun/spctl for sign_bundle",
    "tests/tooling/release/shell/signing/bundle/test_sign_bundle_audit.py": "test of sign_bundle against the fakes",
    "tests/tooling/release/shell/signing/bundle/test_sign_bundle_flow.py": "test of sign_bundle against the fakes",
    "tests/tooling/release/shell/signing/bundle/test_sign_bundle_modes.py": "test of sign_bundle's modes against the fakes",
    "tests/tooling/release/shell/signing/bundle/test_sign_bundle_refusals.py": "test of sign_bundle's refusals against the fakes",
    "tests/tooling/release/shell/signing/conftest.py": "test fixture: removes the throw-away keychains of the Darwin identity tests",
    "tests/tooling/release/shell/signing/identity_fakes.py": "test: fakes for the identity setup scripts",
    "tests/tooling/release/shell/signing/test_identity_setup_behaviour.py": "test of the identity setup scripts against fakes",
    "tests/tooling/release/shell/signing/test_identity_setup_macos.py": "Darwin test of the identity setup with throw-away keychains",
    "tests/tooling/release/shell/signing/test_identity_setup_policy.py": "policy test: lists the keychain commands the setup may use",
    "tests/tooling/release/shell/signing/test_signing_only_by_switch.py": "policy test: lists the forbidden commands",
    "tests/tooling/release/shell/signing/wizard/test_wizard_flow.py": "test of the identity guide against fakes",
    "tests/tooling/release/shell/signing/wizard/test_wizard_macos.py": "Darwin test of the identity guide with throw-away keychains",
    "tests/tooling/release/shell/signing/policy/test_repository_scan.py": "this test: the patterns and their controls",
    "tests/tooling/release/shell/signing/hardening/test_defence_in_depth.py": "test of sign_bundle's individual checks against the fakes",
    "tests/tooling/release/shell/signing/hardening/test_notary_evidence.py": "test of sign_bundle's notary evidence against the fakes",
    "tests/tooling/release/shell/signing/hardening/test_final_copy.py": "test of --verify-only --extract-to against the fakes",
}


def _tracked() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True)
    return listed.stdout.splitlines()


def uses_in(text: str, suffix: str) -> list[str]:
    """The signing-tool uses in ``text`` (comment lines skipped for ``#``-comment suffixes)."""

    found = []
    for line in text.splitlines():
        if suffix in HASH_COMMENTS and line.lstrip().startswith("#") and not line.startswith("#!"):
            continue
        found += [match.group(0) for match in APPLE_TOOL.finditer(line)]
        found += [match.group(0) for match in SECURITY.finditer(line)]
        found += [match.group(0) for match in CODESIGN_SHELL.finditer(line) if match.group(1).strip("\"'") != "-"]
        found += [match.group(0) for match in CODESIGN_ARGV.finditer(line)]
    return found


def file_uses(path: Path) -> list[str]:
    """The signing-tool uses in one repository file (Markdown, symlinks and non-files have none)."""

    if path.suffix == ".md" or path.is_symlink() or not path.is_file():
        return []
    return uses_in(path.read_text(encoding="utf-8", errors="replace"), path.suffix)


def _users() -> dict[str, list[str]]:
    users = {}
    for name in _tracked():
        found = file_uses(ROOT / name)
        if found:
            users[name] = found
    return users


def test_a_shebang_line_and_a_file_with_a_stray_byte_are_still_scanned(tmp_path: Path) -> None:
    shebang = tmp_path / "wrap.sh"
    shebang.write_text("#!/usr/bin/xcrun notarytool\n", encoding="utf-8")
    assert file_uses(shebang) == ["notarytool"]
    stray = tmp_path / "stray.sh"
    stray.write_bytes(b"echo \xff\nxcrun notarytool submit x\n")
    assert file_uses(stray) == ["notarytool"]


def test_only_the_listed_files_use_the_signing_tools_and_every_listed_file_still_does() -> None:
    users = _users()
    unlisted = {name: found[:3] for name, found in users.items() if name not in EXEMPT}
    assert unlisted == {}, unlisted
    stale = sorted(name for name in EXEMPT if name not in users)
    assert stale == [], stale


def test_the_patterns_catch_each_form_and_spare_the_harmless_ones() -> None:
    caught = [
        'xcrun notarytool submit x --keychain-profile p',
        'subprocess.run(["xcrun", "stapler", "staple", app])',
        "spctl --assess --type execute app",
        "security unlock-keychain ~/Library/Keychains/login.keychain-db",
        'tools.must("security", "find-identity", "-v")',
        "security -v import key.p12",
        'codesign --force --sign "Developer ID Application: X" app',
        "codesign -s ABCDEF app",
        'run("codesign", "--force", "--sign", identity, path)',
    ]
    for line in caught:
        assert uses_in(line, ".sh"), line
    spared = [
        "from fastapi.security import HTTPBearer",
        "codesign --force --sign - app",
        'run("codesign", "--force", "--sign", "-", path)',
        "codesign --verify --deep --strict app",
        "the security consequence is accepted",
        '["git", "status", "--short"]',
        "# xcrun notarytool submit in a comment",
    ]
    for line in spared:
        assert not uses_in(line, ".sh"), line
    assert uses_in("# xcrun notarytool", ".json")


def _mac_jobs() -> list[tuple[str, str, dict]]:
    found = []
    directories = (ROOT / ".github" / "workflows", ROOT / ".github" / "workflows")
    for path in sorted(path for directory in directories for pattern in ("*.yml", "*.yaml") for path in directory.glob(pattern)):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, job in (document.get("jobs") or {}).items():
            labels = job.get("runs-on", [])
            labels = {labels} if isinstance(labels, str) else set(labels)
            if labels & MAC_LABELS:
                found.append((path.name, name, job))
    return found


def test_no_job_on_the_release_mac_uses_a_local_action() -> None:
    """Every job on the release Mac, job-level ``uses:`` included.

    No workflow in the tree runs on the release Mac as of PR #78 (2026-11-26),
    so the loop below is empty until one does; the scan stays fail-closed
    because any Mac job with a local ``uses:`` is reported.
    """

    for workflow, name, job in _mac_jobs():
        uses = [str(job.get("uses", ""))] + [str(step.get("uses", "")) for step in job.get("steps") or []]
        local = [item for item in uses if item.startswith(".")]
        assert local == [], f"{workflow}::{name}: {local}"
