"""The package script with ``--developer-id`` (milestone 2), against fake host tools.

Pinned here: the signed mode runs only with the switch and explicit
identity, notary profile and source sha (malformed or incomplete options exit
2 before any stage); its stage order (preflight before the long build, sign
and verify after the ad-hoc bundle check, both smokes on the app copied out
of the final dmg by ``--verify-only --extract-to``);
fail closed (any failed stage exits 1, no dmg, no checksums, no manifest is
collected, the ad-hoc dmg never is, the signed work directory is removed,
NOT-SIGNED.txt names the stage and the notary ids already spent); the
BUILD-INFO.txt statement of signing and distributability. The fake ``uv``
stands in for sign_bundle.py and the wheel remote check (``script_support``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tooling.release.shell.candidate.script_support import fake_log, make_repo, node_dir, run_script, tools
from tests.tooling.release.shell.candidate.test_package_script_run import _summary

SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"
SIGNED_STAGES = ["source", "host", "node", "toolchain", "sign_preflight", "tauri_cli", "wheel", "stage", "verify_staged", "build_app", "verify_bundle", "sign", "verify_signed", "smoke_sigterm", "smoke_kill9", "collect"]
SIGNED_DMG = "HyprialOS_0.0.0_aarch64-signed.dmg"


def _setup(tmp_path: Path):
    repo, head = make_repo(tmp_path)
    path = [node_dir(tmp_path / "keg", "v24.22.3"), tools(tmp_path)]
    return repo, head, path


def _signed_args(head: str, *extra: str) -> list[str]:
    return ["--developer-id", "--identity", SHA1, "--notary-profile", "test-profile", "--source-sha", head, *extra]


def _steps(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        if line.startswith("build-wheel.sh"):
            found.append("wheel")
        elif "sign_bundle.py" in line:
            found.append("sign_bundle --check-only" if "--check-only" in line else "sign_bundle --verify-only" if "--verify-only" in line else "sign_bundle sign")
        elif line.startswith("cargo tauri build"):
            found.append("tauri build")
        elif "verify_runtime.py" in line and ".app/Contents" in line:
            found.append("verify bundle")
        elif "smoke_bundle.py" in line:
            found.append("smoke " + line.split("--stop ", 1)[1].split()[0])
    return found


def _out(repo: Path) -> Path:
    return repo / "dist" / "shell-macos"


def _names(directory: Path) -> list[str]:
    return sorted(item.name for item in directory.iterdir())


def test_a_signed_run_signs_after_the_ad_hoc_check_and_smokes_the_signed_app(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed_args(head), path)
    assert result.returncode == 0, result.stdout + result.stderr
    log = fake_log(repo)
    assert _steps(log) == ["sign_bundle --check-only", "wheel", "tauri build", "verify bundle", "sign_bundle sign", "sign_bundle --verify-only", "smoke sigterm", "smoke kill9"]
    rows = _summary(result.stdout)
    assert list(rows) == SIGNED_STAGES and set(rows.values()) == {"PASSED"}
    final = repo / "dist" / "shell-macos-work" / "final"
    copied_app = str(final / "app" / "HyprialOS.app")
    smokes = [line for line in log if "smoke_bundle.py" in line]
    assert len(smokes) == 2 and all(f"--app {copied_app} " in line for line in smokes)
    verify = next(line for line in log if "sign_bundle.py" in line and "--verify-only" in line)
    assert verify.endswith(f"--extract-to {final} --mark-unfinished") and str(repo / "dist" / "shell-macos-work" / "signed") in verify
    adhoc = [line for line in log if "verify_runtime.py" in line and ".app/Contents" in line]
    assert adhoc and "/bundle/macos/HyprialOS.app/" in adhoc[0]
    sign = next(line for line in log if "sign_bundle.py" in line and "--check-only" not in line and "--verify-only" not in line)
    assert f"--identity={SHA1}" in sign and "--notary-profile test-profile" in sign and "--notary-timeout-minutes" not in sign
    assert "/bundle/macos/HyprialOS.app " in sign
    assert sign.endswith(" --mark-unfinished")


def test_a_signed_run_collects_only_the_signed_dmg_and_states_signing(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed_args(head, "--notary-timeout-minutes", "90"), path)
    assert result.returncode == 0, result.stdout + result.stderr
    out = _out(repo)
    assert _names(out) == sorted([
        "BUILD-INFO.txt", SIGNED_DMG, "SHA256SUMS", "runtime-manifest.json", "sign-bundle.json", "sign-preflight.json",
        "smoke-kill9.json", "smoke-sigterm.json", "verify-bundle.json", "verify-signed.json", "verify-staged.json",
    ])
    assert (repo / "clients/native/src-tauri/target/release/bundle/dmg/HyprialOS_0.0.0_aarch64.dmg").exists()
    assert (out / "runtime-manifest.json").read_text(encoding="utf-8") == '{"files": {"signed": true}}\n'
    sums = (out / "SHA256SUMS").read_text(encoding="utf-8").split()
    assert sums[1::2] == [SIGNED_DMG, "runtime-manifest.json"]
    info = (out / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert f"commit: {head}" in info and f"requested source sha: {head}" in info and "tree dirty: no" in info
    assert "signing: Developer ID (--developer-id)" in info and "team id: ABCDE12345" in info
    assert "distributable: internal verification only (" in info and "does not check" in info and "KP-100" in info
    assert any("--notary-timeout-minutes 90" in line for line in fake_log(repo) if "sign_bundle.py" in line)


def test_a_wheel_without_the_public_remote_makes_the_artifact_not_distributable(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed_args(head), path, {"FAKE_FAIL": "public_remote"})
    assert result.returncode == 0, result.stdout + result.stderr
    info = (_out(repo) / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert "distributable: no (the wheel does not record the public source remote" in info and "internal verification only" not in info


@pytest.mark.parametrize("failing", ["sign_preflight", "build_app", "verify_bundle", "sign", "verify_signed", "smoke_sigterm", "smoke_kill9"])
def test_any_failure_fails_the_run_and_leaves_nothing_that_looks_like_a_release(tmp_path: Path, failing: str) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed_args(head), path, {"FAKE_FAIL": failing})
    assert result.returncode == 1, result.stdout + result.stderr
    rows = _summary(result.stdout)
    assert rows[failing] == "FAILED" and rows["collect"] == "PASSED" and "result: FAILED" in result.stdout
    out = _out(repo)
    names = _names(out)
    assert not [name for name in names if name.endswith(".dmg")], names
    assert "SHA256SUMS" not in names and "runtime-manifest.json" not in names
    assert not (repo / "dist" / "shell-macos-work" / "signed").exists()
    assert not (repo / "dist" / "shell-macos-work" / "signed.unfinished").exists()
    not_signed = (out / "NOT-SIGNED.txt").read_text(encoding="utf-8")
    assert f"the first stage that did not succeed: {failing} (failure)" in not_signed
    assert "no dmg was collected" in not_signed
    info = (out / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert f"signing: FAILED at {failing} (failure)" in info and "distributable: no" in info and "internal verification only" not in info
    spent = failing in {"sign", "verify_signed", "smoke_sigterm", "smoke_kill9"}
    assert ("11111111-2222-3333-4444-555555555555" in not_signed) is spent
    assert ("66666666-7777-8888-9999-000000000000" in not_signed) is spent


def test_a_failed_preflight_stops_before_the_long_build(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed_args(head), path, {"FAKE_FAIL": "sign_preflight"})
    assert result.returncode == 1
    assert _steps(fake_log(repo)) == ["sign_bundle --check-only"]
    rows = _summary(result.stdout)
    for later in SIGNED_STAGES[5:-1]:
        assert rows[later] == "NOT RUN", later
    assert (_out(repo) / "NOT-BUILT.txt").read_text(encoding="utf-8").startswith("no app or dmg was built; the first stage that did not succeed: sign_preflight (failure)")


def test_the_discover_form_and_the_test_tool_path_variable(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    args = ["--developer-id", "--discover-identity", "--notary-profile", "p", "--source-sha", head]
    result = run_script(repo, args, path, {"HYPRIAL_SIGN_BUNDLE_TOOL_PATH": str(tmp_path / "fakes")})
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [line for line in fake_log(repo) if "sign_bundle.py" in line]
    assert len(calls) == 3 and all("--discover-identity" in line and "--identity=" not in line for line in calls)
    seen = (repo.parent / "fake.log.env").read_text(encoding="utf-8").splitlines()
    assert all("HYPRIAL_SIGN_BUNDLE" not in line for line in seen if line.startswith("uv "))


@pytest.mark.parametrize("args", [
    ["--developer-id", "--notary-profile", "p"],
    ["--developer-id", "--identity", SHA1],
    ["--developer-id", "--identity", SHA1, "--notary-profile", "p", "--no-sha"],
    ["--developer-id", "--identity", SHA1, "--notary-profile", "p", "--skip-smoke", "--sha"],
    ["--developer-id", "--identity", SHA1[:-1], "--notary-profile", "p", "--sha"],
    ["--developer-id", "--identity", "g" * 40, "--notary-profile", "p", "--sha"],
    ["--developer-id", "--identity", SHA1, "--discover-identity", "--notary-profile", "p", "--sha"],
    ["--developer-id", "--identity", SHA1, "--notary-profile", "has space", "--sha"],
    ["--developer-id", "--identity", SHA1, "--notary-profile", "-p", "--sha"],
    ["--developer-id", "--identity", SHA1, "--notary-profile", "p", "--notary-timeout-minutes", "0", "--sha"],
    ["--developer-id", "--identity", SHA1, "--notary-profile", "p", "--notary-timeout-minutes", "1441", "--sha"],
    ["--developer-id", "--identity", SHA1, "--notary-profile", "p", "--notary-timeout-minutes", "9x", "--sha"],
    ["--identity", SHA1],
    ["--discover-identity"],
    ["--notary-profile", "p"],
    ["--notary-timeout-minutes", "30"],
])
def test_incomplete_malformed_or_switchless_signing_options_exit_2_before_any_stage(tmp_path: Path, args: list[str]) -> None:
    repo, head, path = _setup(tmp_path)
    argv = [item for item in args if item not in {"--sha", "--no-sha"}]
    if "--sha" in args:
        argv += ["--source-sha", head]
    result = run_script(repo, argv, path)
    assert result.returncode == 2, (args, result.stdout, result.stderr)
    assert "==> source" not in result.stdout
    assert fake_log(repo) == []
    assert not (repo / "dist").exists()


def test_without_the_switch_signing_lookalike_variables_change_nothing(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    variables = {
        "DEVELOPER_ID": "1",
        "IDENTITY_ARG": "--discover-identity",
        "NOTARY_PROFILE": "p",
        "NOTARY_MINUTES": "90",
        "ALL_STAGES": "source sign collect",
        "SIGNED_STAGES": "source sign collect",
        "BUILD_PATH": "source",
    }
    result = run_script(repo, [], path, variables)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [line for line in fake_log(repo) if "sign_bundle.py" in line or "wheel_source_remote.py" in line]
    rows = _summary(result.stdout)
    assert len(rows) == 13 and "sign" not in rows and "sign_preflight" not in rows
    assert "HyprialOS_0.0.0_aarch64.dmg" in _names(_out(repo))
    assert "Developer ID signing requested" not in result.stdout
    info = (_out(repo) / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert "signing: ad hoc (" in info and "distributable: no (" in info
    assert "Developer ID (" not in info and "internal verification only" not in info
