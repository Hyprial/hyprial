"""sign_bundle.py's checks each catch the fault they exist for, and only that one.

Every case makes exactly one check see a bad signature, ticket, verdict or
file (the knobs of ``bundle_fakes`` change only what ``codesign -dvv``,
``--extract-certificates``, ``--verify``, ``spctl``, ``notarytool`` or the
dmg's copy report), then pins the step that must stop the run and the words
of its failure, so deleting that check turns the case red instead of letting
a later check (or nothing) catch it. Covered: an inner item of another team,
another leaf certificate, a foreign root or chain, no Developer ID authority,
no hardened runtime, the app changed after its staple (a sealed resource:
verify; a runtime file's mode: the manifest), a dmg that does not verify, an
Accepted notary reply with a failing exit, an accepted spctl verdict with a
failing exit, nothing signed under the runtime, and the app inside the
mounted final dmg (verify, ticket, manifest, Gatekeeper, detach).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import OTHER_LEAF, VERSION, Bench
from tests.tooling.release.shell.support import load

sign_bundle = load("sign_bundle")
DMG = f"HyprialOS_{VERSION}_aarch64-signed.dmg"


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    return Bench(tmp_path).install(monkeypatch, sign_bundle)


def _fails_at(bench: Bench, step: str, needle: str, **knobs: str) -> dict:
    bench.knob(**knobs)
    assert sign_bundle.main(bench.argv()) == 1
    evidence = bench.evidence_json()
    failing = [entry["step"] for entry in evidence["steps"] if entry["status"] == "FAIL"]
    assert failing == [step], (failing, evidence["problems"])
    assert any(needle in problem for problem in evidence["problems"]), evidence["problems"]
    assert not bench.out.exists() and not bench.unfinished.exists()
    return evidence


def _submissions(bench: Bench) -> int:
    return len(bench.of("notarytool-submit"))


def test_an_inner_item_signed_for_another_team_fails_the_audit(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "deeper/_x.so: unexpected Team ID", FAKE_TEAM_FOR=json.dumps({"_x.so": "ZZZZZ99999"}))
    assert _submissions(bench) == 0


def test_a_leaf_certificate_other_than_the_identity_fails(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "leaf certificate", FAKE_LEAF=OTHER_LEAF)
    assert _submissions(bench) == 0


def test_a_chain_that_does_not_end_in_the_pinned_apple_root_fails(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "root certificate", FAKE_ROOT="some other root")


def test_a_chain_through_another_intermediate_fails_the_audit(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "unexpected certificate chain", FAKE_CHAIN=json.dumps(["Apple Worldwide Developer Relations Certification Authority", "Apple Root CA"]))


def test_a_signature_without_a_developer_id_authority_fails_the_audit(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "no Developer ID Application authority", FAKE_AUTHORITY="Apple Development: Example Developer (ZZZZZ99999)")


def test_an_inner_item_without_hardened_runtime_fails_the_audit(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "deeper/_x.so: no hardened runtime", FAKE_NO_RUNTIME_FOR="_x.so")
    assert _submissions(bench) == 0


def test_an_app_changed_after_its_staple_fails_the_final_verify(bench: Bench) -> None:
    _fails_at(bench, "gatekeeper", "codesign --verify failed", FAKE_TAMPER="app-content")


def test_a_runtime_file_whose_mode_changed_after_the_staple_fails_the_final_manifest_check(bench: Bench) -> None:
    _fails_at(bench, "gatekeeper", "runtime manifest of the stapled app", FAKE_TAMPER="app-mode")


def test_a_dmg_that_does_not_verify_fails_before_its_submission(bench: Bench) -> None:
    _fails_at(bench, "sign-dmg", "codesign --verify failed", FAKE_FAIL="codesign-verify@dmg")
    assert _submissions(bench) == 1


def test_an_accepted_notary_reply_with_a_failing_exit_fails(bench: Bench) -> None:
    evidence = _fails_at(bench, "notarize-app", "notarization of the app", FAKE_NOTARY_EXIT="1")
    assert evidence["notary"][0]["status"] == "Accepted" and evidence["notary"][0]["exit"] == 1


def test_an_accepted_gatekeeper_verdict_with_a_failing_exit_fails(bench: Bench) -> None:
    _fails_at(bench, "gatekeeper", "exit 1", FAKE_SPCTL_EXIT="1")


def test_nothing_signed_under_the_runtime_fails_before_the_app_signature(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sign_bundle, "signing_order", lambda app: [])
    _fails_at(bench, "manifest", "nothing under the runtime was signed")
    assert bench.signs() == [] and len(bench.probes()) == 1


def test_the_app_inside_the_final_dmg_is_mounted_read_only_and_checked(bench: Bench) -> None:
    assert sign_bundle.main(bench.argv()) == 0
    attach = bench.of("hdiutil-attach")[0]["argv"]
    mount = Path(attach[attach.index("-mountpoint") + 1])
    assert attach[:4] == ["attach", "-readonly", "-nobrowse", "-noautoopen"] and Path(attach[-1]).name == DMG
    app = str(mount / "HyprialOS.app")
    assert any(call["argv"][-1] == app and "--deep" in call["argv"] and "--strict" in call["argv"] for call in bench.of("codesign-verify"))
    assert any(call["argv"] == ["stapler", "validate", app] for call in bench.of("stapler-validate"))
    assert any(call["argv"][:3] == ["--assess", "--type", "execute"] and call["argv"][-1] == app for call in bench.of("spctl"))
    assert [call["argv"] for call in bench.of("hdiutil-detach")] == [["detach", str(mount)]]
    steps = [entry["step"] for entry in bench.evidence_json()["steps"]]
    assert steps[-2:] == ["verify-dmg-app", "collect"]
    assert bench.evidence_json()["dmgApp"]["gatekeeper"] == ["HyprialOS.app: accepted", "source=Notarized Developer ID"]


@pytest.mark.parametrize(("tamper", "needle"), [("FAKE_TAMPER=image-content", "codesign --verify failed"), ("FAKE_TAMPER=image-mode", "runtime manifest of the app in the dmg"), ("FAKE_FAIL=stapler-validate@mnt", "xcrun stapler failed"), ("FAKE_FAIL=spctl@mnt", "Gatekeeper (execute)")])
def test_a_bad_app_inside_the_final_dmg_fails_and_the_image_is_detached(bench: Bench, tamper: str, needle: str) -> None:
    key, value = tamper.split("=")
    _fails_at(bench, "verify-dmg-app", needle, **{key: value})
    assert len(bench.of("hdiutil-detach")) == 1


def test_a_busy_dmg_is_detached_with_force(bench: Bench) -> None:
    bench.knob(FAKE_FAIL="hdiutil-detach")
    assert sign_bundle.main(bench.argv()) == 0
    assert [call["argv"][:2] for call in bench.of("hdiutil-detach")] == [["detach", bench.of("hdiutil-detach")[0]["argv"][1]], ["detach", "-force"]]


def test_a_dmg_that_cannot_be_detached_fails(bench: Bench) -> None:
    _fails_at(bench, "verify-dmg-app", "detach", FAKE_FAIL="hdiutil-detach hdiutil-detach-force")
