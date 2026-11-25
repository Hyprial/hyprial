"""The package script's signed collection, kill and cleanup paths, against fake host tools (PR #2980 review).

Pinned here:
- F: collect copies the signed dmg and manifest, regenerates SHA256SUMS and
  requires all three to have the SHA-256 that ``--verify-only`` recorded
  (and the dmg the one it attached for the smokes); any difference collects
  nothing signed, fails the run and names collect in NOT-SIGNED.txt.
- E: a signed run writes NOT-SIGNED.txt ("did not finish") as soon as its
  output exists, so a run killed by SIGKILL (the runner's cancel and
  timeout) leaves a truthful, text-only output; a successful run removes it.
- Fail-closed upload: whatever stage fails, the output holds only text and
  evidence files.
- Mounts under the signed work directories are detached before they are
  removed; one that cannot be detached leaves the directory in place and
  NOT-SIGNED.txt says so.
- Re-review item 5 and review 38872: ``sign`` and ``verify_signed`` pass
  ``--mark-unfinished``, so the signed work directory and the final copy
  directory hold UNFINISHED.txt from the moment they exist (the tool writes it
  before its rename / copy), removed only by a collect that passed; a SIGINT to the
  process group that ends the script's shell during a smoke (the fake smoke
  is a shell ``sleep``) leaves it and NOT-SIGNED.txt in place. A real smoke
  exits 130 instead and the stage fails, so collect removes the signed work
  (measured on a Mac, PR #2980). Collect removes NOT-SIGNED.txt last, after BUILD-INFO.txt has the
  signed record, so a collect that fails on the way keeps it. A collect
  that cannot write the signed record after copying (review 38872) removes
  the dmg, manifest and SHA256SUMS it had copied and fails like any other
  collect failure (signed work removed, NOT-SIGNED.txt naming collect), so
  "no dmg was collected" stays true.
- Re-review item 6: collect re-checks that HEAD is still --source-sha and the
  tree is still clean; a change during the run collects nothing signed.
- The identity SHA-1 given with --identity is never printed.
- The one deliberate change of the no-switch path: verify_bundle runs
  ``packaging/clients/signing/archives.py`` on the built app, and its failure
  fails that stage.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.tooling.release.shell.candidate.script_support import BASH, SCRIPT, fake_log, make_repo, node_dir, run_script, tools
from tests.tooling.release.shell.candidate.test_package_script_run import _summary

SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"
TEXT_SUFFIXES = (".txt", ".json")


def _setup(tmp_path: Path):
    repo, head = make_repo(tmp_path)
    return repo, head, [node_dir(tmp_path / "keg", "v24.22.3"), tools(tmp_path)]


def _signed(head: str) -> list[str]:
    return ["--developer-id", "--identity", SHA1, "--notary-profile", "test-profile", "--source-sha", head]


def _out(repo: Path) -> Path:
    return repo / "dist" / "shell-macos"


def test_a_collected_dmg_that_is_not_the_verified_one_collects_nothing_signed(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed(head), path, {"FAKE_FAIL": "verified_mismatch"})
    assert result.returncode == 1, result.stdout + result.stderr
    rows = _summary(result.stdout)
    assert rows["collect"] == "FAILED" and all(value == "PASSED" for key, value in rows.items() if key != "collect")
    names = sorted(item.name for item in _out(repo).iterdir())
    assert all(name.endswith(TEXT_SUFFIXES) for name in names), names
    assert "is not the file verify_signed checked (finalCopy dmgSha256" in result.stdout
    not_signed = (_out(repo) / "NOT-SIGNED.txt").read_text(encoding="utf-8")
    assert "the first stage that did not succeed: collect (no signed dmg identical to the verified one could be collected)" in not_signed
    assert "11111111-2222-3333-4444-555555555555" in not_signed and "the signed work directory was removed" in not_signed
    info = (_out(repo) / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert "signing: FAILED at collect" in info and "internal verification only" not in info


def test_a_successful_signed_run_collects_exactly_the_verified_files(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed(head), path)
    assert result.returncode == 0, result.stdout + result.stderr
    out = _out(repo)
    signed = repo / "dist" / "shell-macos-work" / "signed"
    assert (out / "SHA256SUMS").read_bytes() == (signed / "SHA256SUMS").read_bytes()
    assert not (out / "NOT-SIGNED.txt").exists() and not (signed / "UNFINISHED.txt").exists()
    assert not (repo / "dist" / "shell-macos-work" / "final" / "UNFINISHED.txt").exists()
    marked = [line for line in fake_log(repo) if "sign_bundle.py" in line and "--check-only" not in line]
    assert len(marked) == 2 and all("--mark-unfinished" in line for line in marked), marked
    assert "both smokes ran on the app copied out of the final dmg with a quarantine attribute" in (out / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert SHA1 not in result.stdout and "identity <identity> (given, not printed)" in result.stdout


@pytest.mark.parametrize("failing", ["sign_preflight", "wheel", "build_app", "verify_bundle", "sign", "verify_signed", "smoke_sigterm", "smoke_kill9", "verified_mismatch"])
def test_a_failed_signed_run_leaves_only_text_and_evidence(tmp_path: Path, failing: str) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed(head), path, {"FAKE_FAIL": failing})
    assert result.returncode == 1
    names = sorted(item.name for item in _out(repo).iterdir())
    assert "NOT-SIGNED.txt" in names and "BUILD-INFO.txt" in names
    assert all(name.endswith(TEXT_SUFFIXES) for name in names), names


def test_a_run_killed_with_sigkill_leaves_a_truthful_text_only_output(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    marker = tmp_path / "building"
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = {"PATH": os.pathsep.join(str(item) for item in path), "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"), "FAKE_LOG": str(tmp_path / "fake.log"), "GIT_CONFIG_GLOBAL": os.devnull, "FAKE_HANG_BUILD": str(marker)}
    assert BASH is not None
    process = subprocess.Popen([BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", str(repo / "scripts" / "build" / SCRIPT.name), *_signed(head)], cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    names = sorted(item.name for item in _out(repo).iterdir())
    assert all(name.endswith(TEXT_SUFFIXES) for name in names), names
    not_signed = (_out(repo) / "NOT-SIGNED.txt").read_text(encoding="utf-8")
    assert not_signed.startswith("the Developer ID signed run FAILED; the first stage that did not succeed: unknown: the run did not finish")
    assert "no dmg was collected" in not_signed


def test_an_interrupt_during_a_smoke_leaves_the_signed_work_marked_unfinished(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    marker = tmp_path / "smoking"
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = {"PATH": os.pathsep.join(str(item) for item in path), "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"), "FAKE_LOG": str(tmp_path / "fake.log"), "GIT_CONFIG_GLOBAL": os.devnull, "FAKE_HANG_SMOKE": str(marker)}
    assert BASH is not None
    process = subprocess.Popen([BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", str(repo / "scripts" / "build" / SCRIPT.name), *_signed(head)], cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        os.killpg(process.pid, signal.SIGINT)
        assert process.wait(timeout=30) != 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    signed = repo / "dist" / "shell-macos-work" / "signed"
    assert (signed / "HyprialOS_0.0.0_aarch64-signed.dmg").is_file()
    assert "NOT a release" in (signed / "UNFINISHED.txt").read_text(encoding="utf-8")
    assert "NOT a release" in (repo / "dist" / "shell-macos-work" / "final" / "UNFINISHED.txt").read_text(encoding="utf-8")
    assert (_out(repo) / "NOT-SIGNED.txt").read_text(encoding="utf-8").startswith("the Developer ID signed run FAILED; the first stage that did not succeed: unknown: the run did not finish")
    assert not [name for name in os.listdir(_out(repo)) if name.endswith(".dmg")]


def test_a_collect_that_fails_after_copying_keeps_not_signed_and_removes_the_copies(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed(head), path, {"FAKE_FAIL": "signed_build_info"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert _summary(result.stdout)["collect"] == "FAILED"
    assert (_out(repo) / "NOT-SIGNED.txt").is_file()
    left = sorted(item.name for item in _out(repo).iterdir())
    assert all(name.endswith(TEXT_SUFFIXES) for name in left), left
    not_signed = (_out(repo) / "NOT-SIGNED.txt").read_text(encoding="utf-8")
    assert "no dmg was collected" in not_signed
    assert "the first stage that did not succeed: collect (the signed record for BUILD-INFO.txt could not be written)" in not_signed
    assert not (repo / "dist" / "shell-macos-work" / "signed").exists()
    assert "signing: FAILED at collect" in (_out(repo) / "BUILD-INFO.txt").read_text(encoding="utf-8")


def test_a_failing_append_of_the_signed_record_removes_the_copies(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed(head), path, {"FAKE_FAIL": "build_info_readonly"})
    assert result.returncode != 0, result.stdout + result.stderr
    left = sorted(item.name for item in _out(repo).iterdir())
    assert all(name.endswith(TEXT_SUFFIXES) for name in left), left
    assert "no dmg was collected" in (_out(repo) / "NOT-SIGNED.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("change", ["dirty", "head"])
def test_a_source_change_during_the_run_collects_nothing_signed(tmp_path: Path, change: str) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, _signed(head), path, {"FAKE_SOURCE_CHANGE": change})
    assert result.returncode == 1, result.stdout + result.stderr
    rows = _summary(result.stdout)
    assert rows["collect"] == "FAILED" and all(value == "PASSED" for key, value in rows.items() if key != "collect")
    names = sorted(item.name for item in _out(repo).iterdir())
    assert all(name.endswith(TEXT_SUFFIXES) for name in names), names
    not_signed = (_out(repo) / "NOT-SIGNED.txt").read_text(encoding="utf-8")
    assert "the first stage that did not succeed: collect (HEAD or the work tree changed during the run)" in not_signed
    assert not (repo / "dist" / "shell-macos-work" / "signed").exists()


def _mount_line(path: Path) -> str:
    return f"/dev/disk9s1\\tApple_HFS\\t{path}\\n"


def test_a_mount_left_under_the_signed_work_directory_is_detached_before_it_is_removed(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    work = repo / "dist" / "shell-macos-work"
    mount = work / "signed.unfinished" / "work" / "mnt"
    mount.mkdir(parents=True)
    other = work / "final" / "mnt"
    other.mkdir(parents=True)
    lines = "/dev/disk7\\t\\t\\n/dev/disk8\\tGUID_partition_scheme\\t\\n" + _mount_line(mount) + f"/dev/disk9s2\\t\\t{other}\\n" + _mount_line(tmp_path / "elsewhere")
    result = run_script(repo, _signed(head), path, {"FAKE_HDIUTIL_INFO": lines})
    assert result.returncode == 0, result.stdout + result.stderr
    detaches = [line for line in fake_log(repo) if line.startswith("hdiutil detach")]
    assert set(detaches) == {f"hdiutil detach {mount}", f"hdiutil detach {other}"}
    log = fake_log(repo)
    assert log.index(f"hdiutil detach {mount}") < next(index for index, line in enumerate(log) if "sign_bundle.py" in line and "--check-only" not in line)


def test_a_mount_that_cannot_be_detached_is_left_in_place_and_reported(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    work = repo / "dist" / "shell-macos-work"
    mount = work / "signed.unfinished" / "work" / "mnt"
    mount.mkdir(parents=True)
    result = run_script(repo, _signed(head), path, {"FAKE_HDIUTIL_INFO": _mount_line(mount), "FAKE_FAIL": "hdiutil_detach"})
    assert result.returncode == 1
    assert _summary(result.stdout)["sign"] == "FAILED"
    assert mount.is_dir()
    detaches = [line for line in fake_log(repo) if line.startswith("hdiutil detach")]
    assert f"hdiutil detach {mount}" in detaches and f"hdiutil detach -force {mount}" in detaches
    not_signed = (_out(repo) / "NOT-SIGNED.txt").read_text(encoding="utf-8")
    assert "could NOT be removed (a dmg stays attached)" in not_signed


def test_without_the_switch_verify_bundle_runs_the_archive_scan_and_fails_with_it(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path)
    assert result.returncode == 0, result.stdout + result.stderr
    scans = [line for line in fake_log(repo) if "signing/archives.py" in line]
    assert scans == [f"uv run --no-project --python 3.12 python packaging/clients/signing/archives.py --app {repo}/clients/native/src-tauri/target/release/bundle/macos/HyprialOS.app"]
    failed = run_script(repo, [], path, {"FAKE_FAIL": "archives"})
    assert failed.returncode == 1
    assert _summary(failed.stdout)["verify_bundle"] == "FAILED"
