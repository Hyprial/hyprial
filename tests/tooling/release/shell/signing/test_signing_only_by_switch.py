"""Developer ID signing happens only through one explicit switch, never in CI (KP-100).

History: owner direction on PR #2899 (2026-11-17) kept signing out of CI and
this file was ``test_no_signing_in_ci.py``. Owner decision 2026-11-19
(milestone 2) reversed that for one manual workflow, ``shell-macos-signed.yml``.
Owner decision 2026-11-20 reversed it again: packaging leaves CI entirely.
Both packaging workflows (``shell-macos-candidate.yml``, ad hoc, and
``shell-macos-signed.yml``) are deleted; the macOS package, ad hoc or signed,
is built only by running ``scripts/build/package-macos-shell.sh`` by hand
(``packaging/clients/README.md`` "Package by hand").

Signing has exactly one door: ``package-macos-shell.sh --developer-id`` with
an explicit identity and notary profile, which calls
``packaging/clients/sign_bundle.py``; it and its ``signing/modes.py`` and
``signing/notary.py`` are the only product files that run ``codesign
--sign``, ``notarytool``, ``stapler`` or ``spctl`` (the owner-run identity
setup scripts are the others, ``policy/test_repository_scan.py``).

Two halves:

- The script switch: ``DEVELOPER_ID`` is set only by ``--developer-id``, the
  three signed stages run only through ``signed_stage``, there is no ad-hoc
  fallback, the package script runs no Apple tool and reads no secret, and
  the repository's Tauri configuration stays ad hoc without hardened runtime.
  Behaviour of the switch (no signing without it, fail closed with it) is
  pinned with fake tools in ``candidate/test_package_script_signed.py`` and
  ``signing/bundle/``.
- No workflow packages, signs or notarizes: no workflow file (``*.yml`` or
  ``*.yaml``) invokes ``package-macos-shell.sh`` or ``sign_bundle.py``, passes
  ``--developer-id``, names a signing credential, runs ``notarytool``,
  ``stapler``, ``altool``, a keychain import or a non-ad-hoc ``codesign
  --sign``, sets the test-only tool path or changes the account's keychains;
  no job on the release Mac reads a secret but its checkout token, and no
  repository file names an Apple-related secret or holds key material.

Who names the entry points (PR #2980 re-review, item 2): a wrapper script
that calls ``package-macos-shell.sh --developer-id`` and is itself called
from a workflow passes every workflow check above. So every repository file
(Markdown excepted, ``#`` comment lines skipped but never a ``#!`` line, a
file with bytes that are not UTF-8 read with replacement characters, review
38872) whose code names ``package-macos-shell``, ``sign_bundle.py`` or
``--developer-id`` must be in ENTRY_POINT_NAMERS with its reason; every
listed file must still name one (no dead entries). New entries only through
review of this file: a new wrapper anywhere fails until it is listed here.
The repository-wide scan for ``codesign --sign`` with a real identity and
for ``notarytool``, ``stapler``, ``spctl`` and keychain commands is
``policy/test_repository_scan.py`` (every tracked file, its own list); the
workflow checks below repeat the workflow part.

Workflow directories (item 3): ``.github/workflows`` and, when present,
``.github/workflows`` (the repository moves to GitHub). Not covered: code
that a Mac job runs through a remote action (``uses: owner/action@ref``) or a
remote reusable workflow lives outside this repository and no scan here sees
it. A ``pull_request`` job runs the workflow files of the pull request's own
head, so a pull request can change the workflow and these tests together.
The job-token check covers ``release-macos-arm64`` only: extending it to
``macos-host`` fails today, because two end-to-end jobs there
(``e2e-macos-credentialed.yml``, ``e2e-provider-calibration.yml``) read
test-service credentials; that is reported on PR #2980, not changed here.

These tests are a regression check for this repository's files, NOT a
security boundary. They guard against packaging or signing being switched
on in CI by ACCIDENT. They cannot stop deliberate misuse: Forgejo runs the
workflow file of whatever branch is pushed or dispatched, and whoever can
push a branch can add a workflow and change these tests in the same push.
The signing identity still sits in the login keychain of the account the
runner process also uses, so "CI does not sign" rests on the absence of a
workflow, not on isolation. The real control points are outside the
repository: who can push branches and trigger workflows on Forgejo, and
which programs the keychain allows to use the private key.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from tests.tooling.release.shell.support import PACKAGING, ROOT

WORKFLOW_DIRS = (ROOT / ".github" / "workflows", ROOT / ".github" / "workflows")
PACKAGE_SCRIPT = ROOT / "scripts" / "build" / "package-macos-shell.sh"
SIGNED_COLLECT = ROOT / "scripts" / "build" / "macos-package" / "signed-collect.sh"
SIGN_BUNDLE = PACKAGING / "sign_bundle.py"
SIGN_MODES = PACKAGING / "signing" / "modes.py"
TAURI_CONF = ROOT / "clients" / "native" / "src-tauri" / "tauri.conf.json"
SWITCH = "--developer-id"
PACKAGING_ENTRY_POINTS = ("package-macos-shell", "sign_bundle", SWITCH)
DELETED_WORKFLOWS = ("shell-macos-candidate", "shell-macos-signed")
SECRET_REFERENCE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
APPLE_SECRET = re.compile(r"secrets\.[A-Za-z0-9_]*(APPLE|P12|PFX|PASSWORD|CERTIFICATE|NOTAR|KEYCHAIN|SIGNING|IDENTITY|DEVELOPER)", re.I)
SCAN_ROOTS = (".github", ".github", ".agents", ".claude", "scripts", "packaging", "clients")
CODE_SUFFIXES = {".sh", ".py", ".yml", ".yaml", ".mjs", ".js", ".ps1", ".json", ".toml"}
KEY_MATERIAL = re.compile(r"\.(p12|pfx|cer|der|p8|key|mobileprovision|provisionprofile|keychain|keychain-db)$", re.I)
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
SCRUBBING_LINES = (
    "for name in $(compgen -v APPLE_) $(compgen -v TAURI_SIGNING_PRIVATE_KEY); do",
    "The script pins Python 3.12 itself (UV_PYTHON) and unsets every APPLE_* and",
)
FORBIDDEN_KEYCHAIN_COMMANDS = (
    "default-keychain -s",
    "default-keychain -d user -s",
    "list-keychains -s",
    "list-keychains -d user -s",
    "unlock-keychain",
    "lock-keychain",
    "set-key-partition-list",
    "add-generic-password",
    "add-internet-password",
    "store-credentials",
    "delete-keychain",
    "delete-identity",
    "delete-certificate",
)
ENTRY_POINT = re.compile(r"package-macos-shell|sign_bundle\.py|--developer-id")
_SHELL_TESTS = "tests/tooling/release/shell/"
ENTRY_POINT_NAMERS = {
    "scripts/build/package-macos-shell.sh": "the package script itself (its usage text and the sign stages)",
    "scripts/build/macos-package/signed-collect.sh": "the package script's signed collection, sourced by it (BUILD-INFO.txt names the switch)",
    "scripts/ci/wheel_source_remote.py": "docstring: the signed run uses it for BUILD-INFO.txt; it calls nothing",
    "packaging/clients/stage_runtime.py": "docstring: sign_bundle.py reuses its manifest function",
    "packaging/clients/signing/archives.py": "failure message naming sign_bundle.py",
    "tests/tooling/ci/tailcat_wheel/test_wheel_builds_record_public_remote.py": "test: the script counts as a wheel build step",
    "tests/tooling/ci/uv_resilience/test_workflow_uv_resilience.py": "test: the script's uv pin",
    _SHELL_TESTS + "candidate/script_support.py": "test harness: runs the script with fake tools",
    _SHELL_TESTS + "candidate/signed/__init__.py": "test package docstring",
    _SHELL_TESTS + "candidate/signed/test_signed_collect.py": "test of the script's signed collection",
    _SHELL_TESTS + "candidate/test_candidate_steps.py": "test of single script stages",
    _SHELL_TESTS + "candidate/test_package_script.py": "test of the script",
    _SHELL_TESTS + "candidate/test_package_script_signed.py": "test of the script's --developer-id mode",
    _SHELL_TESTS + "signing/bundle/bundle_fakes.py": "test fakes for sign_bundle.py",
    _SHELL_TESTS + "signing/bundle/test_sign_bundle_audit.py": "test of sign_bundle.py",
    _SHELL_TESTS + "signing/bundle/test_sign_bundle_flow.py": "test of sign_bundle.py",
    _SHELL_TESTS + "signing/bundle/test_sign_bundle_modes.py": "test of sign_bundle.py",
    _SHELL_TESTS + "signing/bundle/test_sign_bundle_refusals.py": "test of sign_bundle.py",
    _SHELL_TESTS + "signing/bundle/test_sign_bundle_scan.py": "test of sign_bundle.py",
    _SHELL_TESTS + "signing/hardening/__init__.py": "test package docstring",
    _SHELL_TESTS + "signing/hardening/test_defence_in_depth.py": "test of sign_bundle.py",
    _SHELL_TESTS + "signing/hardening/test_notary_evidence.py": "test of sign_bundle.py",
    _SHELL_TESTS + "signing/hardening/test_unfinished_marker.py": "test of sign_bundle.py --mark-unfinished",
    _SHELL_TESTS + "signing/policy/test_repository_scan.py": "policy test",
    _SHELL_TESTS + "signing/test_signing_only_by_switch.py": "this test",
    _SHELL_TESTS + "test_shell_bundle_config.py": "test of the bundle configuration the script passes",
}
SIGNED_STAGE_FUNCTIONS = ("stage_sign_preflight", "stage_sign", "stage_verify_signed")


def _workflows() -> list[Path]:
    found = sorted(path for directory in WORKFLOW_DIRS for pattern in ("*.yml", "*.yaml") for path in directory.glob(pattern))
    assert any(path.is_relative_to(WORKFLOW_DIRS[0]) for path in found), WORKFLOW_DIRS[0]
    return found


def _scanned() -> list[Path]:
    return [*_workflows(), PACKAGE_SCRIPT, SIGNED_COLLECT]


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#") and not line.startswith("#!")


def _code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if not _is_comment(line)]


def _repository_files() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *SCAN_ROOTS], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / name for name in listed.stdout.splitlines() if (ROOT / name).is_file()]


def _script() -> str:
    return PACKAGE_SCRIPT.read_text(encoding="utf-8") + SIGNED_COLLECT.read_text(encoding="utf-8")


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", _script(), re.S | re.M)
    assert match is not None, name
    return match.group(1)


def _packaging_lines_of_text(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if any(entry in line for entry in PACKAGING_ENTRY_POINTS)]


def _packaging_lines(path: Path) -> list[str]:
    return _packaging_lines_of_text("\n".join(_code_lines(path)))


def test_no_workflow_invokes_the_package_script_or_the_sign_tool() -> None:
    assert _packaging_lines_of_text("bash scripts/build/package-macos-shell.sh --source-sha x") == ["bash scripts/build/package-macos-shell.sh --source-sha x"]
    assert _packaging_lines_of_text("uv run python packaging/clients/sign_bundle.py --check-only") != []
    assert _packaging_lines_of_text("run: ./build.sh --developer-id") != []
    offenders = {path.name: _packaging_lines(path) for path in _workflows() if _packaging_lines(path)}
    assert offenders == {}, offenders


def test_the_packaging_workflows_stay_deleted() -> None:
    present = sorted(path.name for path in _workflows() if path.stem in DELETED_WORKFLOWS)
    assert present == []


def test_only_the_package_script_calls_the_sign_tool() -> None:
    callers = {path.relative_to(ROOT).as_posix() for path in _repository_files() if not path.is_relative_to(PACKAGING) and path.suffix in CODE_SUFFIXES and any("sign_bundle.py" in line for line in _code_lines(path))}
    assert callers == {"scripts/build/package-macos-shell.sh"}


def _names_an_entry_point(path: Path) -> bool:
    if path.suffix == ".md" or path.is_symlink() or not path.is_file():
        return False
    return any(ENTRY_POINT.search(line) for line in _code_lines(path))


def test_a_shebang_line_and_a_file_with_a_stray_byte_still_name_an_entry_point(tmp_path: Path) -> None:
    shebang = tmp_path / "wrap"
    shebang.write_text("#!/usr/bin/env -S bash scripts/build/package-macos-shell.sh --developer-id\n", encoding="utf-8")
    assert _names_an_entry_point(shebang)
    stray = tmp_path / "wrap.sh"
    stray.write_bytes(b"echo \xff\nexec bash scripts/build/package-macos-shell.sh\n")
    assert _names_an_entry_point(stray)
    comment = tmp_path / "note.sh"
    comment.write_text("# package-macos-shell.sh is run by hand\n", encoding="utf-8")
    assert not _names_an_entry_point(comment)


def test_only_the_listed_files_name_the_package_script_the_sign_tool_or_the_switch() -> None:
    assert ENTRY_POINT.search("exec bash scripts/build/package-macos-shell.sh --source-sha x")
    assert ENTRY_POINT.search("./release.sh --developer-id") and ENTRY_POINT.search("python packaging/clients/sign_bundle.py")
    assert not ENTRY_POINT.search("def sign_bundle(path):")
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True)
    namers = {name for name in listed.stdout.splitlines() if _names_an_entry_point(ROOT / name)}
    unlisted = sorted(namers - set(ENTRY_POINT_NAMERS))
    assert unlisted == [], unlisted
    stale = sorted(set(ENTRY_POINT_NAMERS) - namers)
    assert stale == [], stale


def test_the_package_script_signs_only_behind_the_switch() -> None:
    code = "\n".join(_code_lines(PACKAGE_SCRIPT))
    assert re.findall(r"^DEVELOPER_ID=(\S+)$", code, re.M) == ["0"]
    assert re.findall(r"DEVELOPER_ID=1", code) == ["DEVELOPER_ID=1"]
    assert "--developer-id) DEVELOPER_ID=1; shift ;;" in _function("parse_args")
    bodies = [_function(name) for name in SIGNED_STAGE_FUNCTIONS]
    for line in _code_lines(PACKAGE_SCRIPT):
        if "sign_bundle.py" in line:
            assert any(line in body for body in bodies) or line.strip().startswith(("--developer-id", "sign_bundle.py")), line
    main = _function("main")
    for name in SIGNED_STAGE_FUNCTIONS:
        stage = name.removeprefix("stage_")
        assert re.findall(rf"^\s+signed_stage {stage}$", main, re.M) == [f"    signed_stage {stage}"]
        assert not re.search(rf"^\s+(build_stage|smoke_stage|run_stage) {stage}\b", main, re.M), stage
    assert _function("signed_stage").splitlines()[0].strip() == 'if [ "${DEVELOPER_ID}" -eq 1 ]; then'


def test_signed_mode_has_no_ad_hoc_fallback() -> None:
    collect = _function("collect_signed")
    assert "${BUNDLE}" not in collect and "bundle/dmg" not in collect
    assert "find \"${SIGNED}\" -maxdepth 1 -name '*_aarch64-signed.dmg' -print" in collect
    assert 'if [ "${FAILED}" -eq 0 ]; then' in collect
    assert "remove_signed_work" in collect
    assert 'for dir in "${SIGNED}" "${SIGNED}.unfinished" "${FINAL}"; do' in _function("remove_signed_work")
    assert "collect_signed\n        exit 0" in _function("stage_collect").split("for dmg in", 1)[0]
    text = SIGN_BUNDLE.read_text(encoding="utf-8")
    signs = re.findall(r'"--sign", ([a-z_.]+)', text + SIGN_MODES.read_text(encoding="utf-8"))
    assert sorted(signs) == ["identity", "run.identity"]
    assert "SHA1_PATTERN.match(self.args.identity" in text and "self.identity not in known" in text


def test_no_workflow_names_a_signing_credential_or_runs_a_signing_tool() -> None:
    for path in _scanned():
        for line in _code_lines(path):
            if line.strip() in SCRUBBING_LINES and path == PACKAGE_SCRIPT:
                continue
            for forbidden in FORBIDDEN_IN_WORKFLOWS:
                assert forbidden not in line, f"{path.name}: {line.strip()}"


def test_codesign_in_a_workflow_or_the_package_script_signs_only_ad_hoc() -> None:
    for path in _scanned():
        for line in _code_lines(path):
            for identity in CODESIGN_SIGN.findall(line):
                assert identity.strip("\"'") == "-", f"{path.name}: {line.strip()}"


def test_the_package_script_runs_no_apple_tool_and_reads_no_secret() -> None:
    code = "\n".join(_code_lines(PACKAGE_SCRIPT) + _code_lines(SIGNED_COLLECT))
    assert "codesign --verify" in code
    assert CODESIGN_SIGN.findall(code) == []
    assert not SECRET_REFERENCE.findall(code)
    assert [line.strip() for line in _code_lines(PACKAGE_SCRIPT) if "APPLE_" in line] == list(SCRUBBING_LINES)
    for name in ("GITHUB_TOKEN", "CERTIFICATE", "KEYCHAIN", "PASSWORD", "spctl", "xcrun", "security "):
        assert name not in code, name


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


def test_no_repository_file_names_an_apple_secret_or_holds_key_material() -> None:
    assert APPLE_SECRET.search("${{ secrets.APPLE_CERTIFICATE_P12 }}") and APPLE_SECRET.search("secrets.NOTARY_PASSWORD")
    assert not APPLE_SECRET.search("${{ secrets.GITHUB_TOKEN }}")
    files = _repository_files()
    assert PACKAGE_SCRIPT in files and set(_workflows()) <= set(files)
    for path in files:
        assert not APPLE_SECRET.search(path.read_text(encoding="utf-8", errors="ignore")), path
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    assert [name for name in tracked if KEY_MATERIAL.search(name)] == []


def test_no_workflow_sets_the_test_only_tool_path_and_the_signing_path_never_traces() -> None:
    for path in _scanned():
        for line in _code_lines(path):
            assert "HYPRIAL_SIGN_BUNDLE_TOOL_PATH" not in line or path == PACKAGE_SCRIPT, f"{path.name}: {line.strip()}"
    for line in _code_lines(PACKAGE_SCRIPT):
        for tracing in ("set -x", "set -o xtrace", "bash -x"):
            assert tracing not in line, f"{PACKAGE_SCRIPT.name}: {line.strip()}"
    assert [line.strip() for line in _code_lines(PACKAGE_SCRIPT) if "HYPRIAL_SIGN_BUNDLE_TOOL_PATH" in line] == ["unset HYPRIAL_SIGN_BUNDLE_TOOL_PATH"]


def test_the_tauri_build_signs_ad_hoc_only_without_hardened_runtime() -> None:
    macos = json.loads(TAURI_CONF.read_text(encoding="utf-8"))["bundle"]["macOS"]
    assert macos["signingIdentity"] == "-"
    assert macos["hardenedRuntime"] is False


def _signing_code() -> list[Path]:
    return [SIGN_BUNDLE, *sorted((PACKAGING / "signing").glob("*.py"))]


def test_no_workflow_or_signing_code_changes_the_account_keychains() -> None:
    signing = _signing_code()
    assert SIGN_MODES in signing and PACKAGING / "signing" / "notary.py" in signing
    for path in [*_scanned(), *signing]:
        for line in _code_lines(path):
            for forbidden in FORBIDDEN_KEYCHAIN_COMMANDS:
                assert forbidden not in line, f"{path.name}: {line.strip()}"
    text = "".join(path.read_text(encoding="utf-8") for path in signing)
    assert re.findall(r'"security", "([a-z-]+)"', text) == ["find-identity"]
    assert set(re.findall(r'"notarytool", "([a-z-]+)"', text)) == {"history", "submit", "wait", "log"}


def test_the_retired_signing_helpers_stay_gone() -> None:
    assert not (PACKAGING / "sign_keychain.py").exists()
    assert not (PACKAGING / "sign_release.py").exists()
