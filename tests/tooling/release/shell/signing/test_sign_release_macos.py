"""sign_release.py and the placeholder keychain against the real macOS tools.

Every `security` and `codesign` child here runs with HOME pointing at a
throwaway home: either the placeholder keychain's own home, whose
`Library/Keychains` holds only the per-test keychain, or an empty one. No
call reaches the account's login keychain; the account's keychain search
list and default keychain are read (with the account's real home from the
password database, since the session fixture points HOME elsewhere) before
and after and must not change. The bundle is a tiny synthetic app made of
copies of /usr/bin/true, so verify_runtime and the launch smoke are replaced
by a manifest check; the real candidate is exercised by hand (PR body).
These need macOS; elsewhere they are marked like the repository's other
Darwin-only tests (tests/kernel/processes/test_owned_process.py), and the
recorded-tool tests in test_sign_release_flow.py and
test_placeholder_keychain.py run instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.placeholder_keychain import PlaceholderKeychain
from tests.tooling.release.shell.support import load

sign_release = load("sign_release")
stage_runtime = load("stage_runtime")
verify_runtime = load("verify_runtime")

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real macOS keychain, codesign and hdiutil are Darwin-only")
COMMIT = "d" * 40
DMG = "HyprialOS_0.0.0_aarch64.dmg"
RUNTIME_MACHOS = {"bin/python3.12", "lib/python3.12/site-packages/pkg/_ext.cpython-312-darwin.so"}


def _account_lists() -> tuple[str, str]:
    env = {"HOME": pwd.getpwuid(os.getuid()).pw_dir, "PATH": "/usr/bin:/bin", "LANG": "C"}
    search = subprocess.run(["/usr/bin/security", "list-keychains", "-d", "user"], capture_output=True, text=True, env=env, check=True, timeout=60).stdout
    default = subprocess.run(["/usr/bin/security", "default-keychain", "-d", "user"], capture_output=True, text=True, env=env, check=True, timeout=60).stdout
    return search, default


def _mounts_under(root: Path) -> list[str]:
    info = subprocess.run(["/usr/bin/hdiutil", "info"], capture_output=True, text=True, check=True, timeout=60).stdout
    return [line for line in info.splitlines() if str(root) in line or str(root.resolve()) in line]


def _copy_true(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile("/usr/bin/true", path)
    path.chmod(0o755)


def _make_candidate(root: Path) -> Path:
    source = root / "source"
    app = source / "HyprialOS.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "hyprial-desktop", "CFBundleIdentifier": "com.hyprial.signing-test", "CFBundlePackageType": "APPL"}))
    _copy_true(contents / "MacOS" / "hyprial-desktop")
    runtime = contents / "Resources" / "runtime"
    _copy_true(runtime / "bin" / "python3.12")
    _copy_true(runtime / "lib" / "python3.12" / "site-packages" / "pkg" / "_ext.cpython-312-darwin.so")
    (runtime / "lib" / "python3.12" / "site-packages" / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (runtime / "lib" / "python3.12" / "site-packages" / "pkg" / "__init__.py").chmod(0o644)
    lock = {"python": "3.12.12", "pbsTag": "20261110", "targets": {"aarch64-apple-darwin": {"sha256": "e" * 64}}}
    facts = {"file": "hyprial-0.0.0-cp312-abi3-macosx_13_0_arm64.whl", "version": "0.0.0", "sha256": "f" * 64, "sourceCommit": COMMIT}
    manifest = stage_runtime.build_manifest(runtime, lock, "aarch64-apple-darwin", facts)
    (runtime / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], capture_output=True, check=True, timeout=120)
    candidate = root / "candidate"
    candidate.mkdir()
    subprocess.run(["/usr/bin/hdiutil", "create", "-volname", "HyprialOS", "-srcfolder", str(source), "-fs", "HFS+", "-format", "UDZO", str(candidate / DMG)], capture_output=True, check=True, timeout=300)
    shutil.copyfile(runtime / "runtime-manifest.json", candidate / "runtime-manifest.json")
    sums = "".join(f"{hashlib.sha256((candidate / name).read_bytes()).hexdigest()}  {name}\n" for name in (DMG, "runtime-manifest.json"))
    (candidate / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return candidate


def _manifest_only_verify(runtime: Path, root_parent: Path):
    problems, _ = verify_runtime.check_manifest(runtime)
    line = "PASS" if not problems else f"FAIL manifest {problems[:3]}"
    return (0 if not problems else 1), line, {"steps": [{"name": "manifest", "status": "PASS" if not problems else "FAIL", "detail": line}]}


def _no_smoke(app: Path, root_parent: Path):
    return 0, "PASS", {"steps": [{"name": "launch", "status": "PASS", "detail": "synthetic bundle: not launched"}]}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "r"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def work(root: Path) -> Path:
    path = root / "kc"
    path.mkdir(mode=0o700)
    return path


def test_identity_lookup_follows_home_not_the_keychain_flag(root: Path, work: Path) -> None:
    """The measured reason the release command passes the operator's HOME through."""

    before = _account_lists()
    target = root / "throwaway-true"
    _copy_true(target)
    empty_home = root / "empty-home"
    (empty_home / "Library" / "Keychains").mkdir(parents=True)
    with PlaceholderKeychain(work) as keychain:
        base = {"PATH": "/usr/bin:/bin", "LANG": "C"}
        flagged = subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", keychain.sha1, "--keychain", str(keychain.keychain), "--timestamp=none", str(target)],
            capture_output=True, text=True, env={**base, "HOME": str(empty_home)}, check=False, timeout=120,
        )
        assert flagged.returncode != 0
        assert "no identity found" in flagged.stderr
        through_home = subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", keychain.sha1, "--timestamp=none", str(target)],
            capture_output=True, text=True, env={**base, "HOME": str(keychain.home)}, check=False, timeout=120,
        )
        assert through_home.returncode == 0, through_home.stderr
    assert not work.exists()
    assert list((empty_home / "Library" / "Keychains").iterdir()) == []
    assert _account_lists() == before


def test_a_dry_run_signs_a_real_bundle_inside_out_with_the_placeholder(root: Path, work: Path) -> None:
    before = _account_lists()
    candidate = _make_candidate(root)
    out = root / "release" / "HyprialOS-0.0.0"
    out.parent.mkdir()
    temp = root / "t"
    temp.mkdir()
    with PlaceholderKeychain(work) as keychain:
        options = sign_release.Options(candidate=candidate, source_commit=COMMIT, out=out, identity=keychain.sha1, notary_profile=None, dry_run=True, home=keychain.home)
        code, evidence = sign_release.run_release(options, verify=_manifest_only_verify, smoke=_no_smoke, temp_parent=temp)
    assert code == 3, json.dumps(evidence, indent=1)
    assert evidence["verdict"] == "UNKNOWN"
    statuses = {step["step"]: step["status"] for step in evidence["steps"]}
    assert statuses["verify app"] == "PASS" and statuses["leaf app"] == "PASS" and statuses["sign dmg"] == "PASS"
    assert statuses["notarize app"] == "UNKNOWN" and statuses["gatekeeper dmg"] == "UNKNOWN"
    assert evidence["machos"] == len(RUNTIME_MACHOS) + 1
    assert evidence["manifest"]["changed_files"] == len(RUNTIME_MACHOS)
    assert evidence["identity"]["leaf_matches"] is True
    assert keychain.sha1 not in json.dumps(evidence) and keychain.sha1.lower() not in json.dumps(evidence)
    assert not out.exists() and not out.with_name(out.name + ".unfinished").exists()
    assert _mounts_under(root) == []
    assert not work.exists()
    assert _account_lists() == before


def test_a_forced_failure_on_a_real_bundle_leaves_nothing_that_looks_finished(root: Path, work: Path) -> None:
    before = _account_lists()
    candidate = _make_candidate(root)
    out = root / "release" / "HyprialOS-0.0.0"
    out.parent.mkdir()
    temp = root / "t"
    temp.mkdir()
    with PlaceholderKeychain(work) as keychain:
        options = sign_release.Options(candidate=candidate, source_commit=COMMIT, out=out, identity="0" * 40, notary_profile=None, dry_run=True, home=keychain.home)
        code, evidence = sign_release.run_release(options, verify=_manifest_only_verify, smoke=_no_smoke, temp_parent=temp)
    assert code == 1
    assert evidence["verdict"] == "FAIL"
    assert any("codesign" in problem for problem in evidence["problems"])
    assert not out.exists() and not out.with_name(out.name + ".unfinished").exists()
    assert list(temp.iterdir()) == []
    assert _mounts_under(root) == []
    assert _account_lists() == before
