"""``--verify-only --extract-to``: the app the smokes launch comes out of the final dmg (PR #2980 review D, F).

Pinned: after every other check, the final dmg is attached read-only, the
app copied out with ``ditto``, the image detached, the copy given a
``com.apple.quarantine`` value of the browser-download form and checked
(``codesign --verify --deep --strict``, ``stapler validate``, ``spctl
--assess --type execute``); the image is detached on a failure too; the
evidence records the SHA-256 of the dmg that was attached (equal before and
after) and of the dmg, manifest and SHA256SUMS that were verified; the option
is refused without ``--verify-only``, for an existing directory or one inside
``--out``. The ``UNFINISHED.txt`` that the package script puts into the
signed output after ``sign`` (PR #2980 re-review, item 5) is not among the
checked files and does not fail them.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import SHA1, VERSION, Bench
from tests.tooling.release.shell.support import load

sign_bundle = load("sign_bundle")
DMG = f"HyprialOS_{VERSION}_aarch64-signed.dmg"
QUARANTINE = re.compile(r"^0081;[0-9a-f]{8};Safari;[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    bench = Bench(tmp_path).install(monkeypatch, sign_bundle)
    assert sign_bundle.main(bench.argv()) == 0
    for name in ("calls.jsonl", "notary-count"):
        (bench.state / name).unlink(missing_ok=True)
    bench.evidence.unlink()
    return bench


def _verify(bench: Bench, target: Path, **knobs: str) -> int:
    bench.knob(**knobs)
    return sign_bundle.main(["--verify-only", "--identity", SHA1, "--out", str(bench.out), "--evidence", str(bench.evidence), "--root-parent", str(bench.tmp), "--extract-to", str(target)])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_app_is_copied_out_of_the_attached_dmg_quarantined_and_checked(bench: Bench) -> None:
    target = bench.tmp / "final"
    assert _verify(bench, target) == 0, bench.evidence_json()["problems"]
    copy = target / "app" / "HyprialOS.app"
    mount = target / "mnt"
    names = bench.names()
    tail = names[names.index("hdiutil-attach"):]
    assert tail == ["hdiutil-attach", "ditto", "hdiutil-detach", "xattr", "codesign-verify", "stapler-validate", "spctl"]
    attach = bench.of("hdiutil-attach")[0]["argv"]
    assert attach == ["attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", str(mount), str(bench.out / DMG)]
    assert bench.of("ditto")[-1]["argv"] == [str(mount / "HyprialOS.app"), str(copy)]
    assert bench.of("hdiutil-detach")[-1]["argv"] == ["detach", str(mount)]
    xattr = bench.of("xattr")[0]["argv"]
    assert xattr[:4] == ["-w", "-r", "com.apple.quarantine", xattr[3]] and xattr[-1] == str(copy)
    assert QUARANTINE.match(xattr[3]), xattr[3]
    assert bench.of("codesign-verify")[-1]["argv"][-1] == str(copy) and "--deep" in bench.of("codesign-verify")[-1]["argv"]
    assert bench.of("stapler-validate")[-1]["argv"] == ["stapler", "validate", str(copy)]
    assert bench.of("spctl")[-1]["argv"][:3] == ["--assess", "--type", "execute"] and bench.of("spctl")[-1]["argv"][-1] == str(copy)
    assert copy.is_dir() and not (mount / "HyprialOS.app").exists()
    evidence = bench.evidence_json()
    final = evidence["finalCopy"]
    assert final["dmgSha256"] == _sha(bench.out / DMG) and final["quarantine"] == xattr[3] and final["dmg"] == DMG
    assert evidence["verifiedSha256"] == {DMG: _sha(bench.out / DMG), "runtime-manifest.json": _sha(bench.out / "runtime-manifest.json"), "SHA256SUMS": _sha(bench.out / "SHA256SUMS")}
    assert [entry["step"] for entry in evidence["steps"]][-1] == "final-copy"


@pytest.mark.parametrize("failing", ["ditto", "xattr", "spctl@final", "stapler-validate@final", "codesign-verify@final"])
def test_a_failing_copy_or_check_fails_and_the_image_is_detached(bench: Bench, failing: str) -> None:
    target = bench.tmp / "final"
    assert _verify(bench, target, FAKE_FAIL=failing) == 1
    assert len(bench.of("hdiutil-attach")) == len(bench.of("hdiutil-detach")) == 1
    assert not (target / "mnt" / "HyprialOS.app").exists()
    evidence = bench.evidence_json()
    assert [entry["step"] for entry in evidence["steps"] if entry["status"] == "FAIL"] == ["final-copy"]
    assert "finalCopy" not in evidence


@pytest.mark.parametrize("case", ["no-verify-only", "exists", "inside-out"])
def test_extract_to_is_refused_without_verify_only_or_for_an_existing_or_inner_directory(bench: Bench, case: str) -> None:
    target = bench.tmp / "final"
    argv = ["--verify-only", "--identity", SHA1, "--out", str(bench.out), "--evidence", str(bench.evidence), "--extract-to", str(target)]
    if case == "no-verify-only":
        argv = ["--check-only", "--identity", SHA1, "--notary-profile", "p", "--evidence", str(bench.evidence), "--extract-to", str(target)]
    elif case == "exists":
        target.mkdir()
    else:
        argv[argv.index("--extract-to") + 1] = str(bench.out / "final")
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(argv)
    assert raised.value.code == 2
    assert bench.calls() == []


def test_the_unfinished_marker_of_the_package_script_does_not_disturb_the_final_checks(bench: Bench) -> None:
    (bench.out / "UNFINISHED.txt").write_text("NOT a release\n", encoding="utf-8")
    target = bench.tmp / "final"
    assert _verify(bench, target) == 0, bench.evidence_json()["problems"]
    assert "UNFINISHED.txt" not in bench.evidence_json()["verifiedSha256"]
