"""Four defence-in-depth checks of sign_bundle.py, each pinned on its own (#2965 review hint).

Each case makes exactly one check see the fault (the knobs of ``bundle_fakes``
change only what that check reads) and pins the step and words that stop the
run, so deleting the check turns the case red instead of a later check (or
nothing) catching it:

- the audit of the signed dmg (``sign-dmg``): a dmg signed for another team;
  ``spctl`` and the notary service in the fakes do not look at the team;
- ``check_leaf``'s ``extra.exists()``: a fourth extracted certificate while
  leaf and root are right;
- the ``": accepted"`` clause of ``gatekeeper``: spctl exits 0 and names
  ``source=Notarized Developer ID`` but says ``rejected``;
- ``Signature=adhoc`` in ``audit``: an inner item that reports a Developer ID
  authority, chain, timestamp, team and runtime and ALSO ``Signature=adhoc``.

Also pinned: the test signature right before the first real signature (a
keychain that locked during the build fails in the bound), the BUILD-INFO
first line under the test tool PATH, and the two new path refusals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import VERSION, Bench
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


def test_a_dmg_signed_for_another_team_is_caught_by_the_dmg_audit(bench: Bench) -> None:
    _fails_at(bench, "sign-dmg", "signature audit of the dmg: unexpected Team ID", FAKE_TEAM_FOR='{".dmg": "ZZZZZ99999"}')
    assert len(bench.of("notarytool-submit")) == 1


def test_a_fourth_certificate_in_the_chain_is_caught_by_the_leaf_check(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "not three certificates", FAKE_EXTRA_CERT="1")
    assert bench.of("notarytool-submit") == []


def test_a_rejected_verdict_with_exit_0_and_the_notarized_source_is_caught(bench: Bench) -> None:
    _fails_at(bench, "gatekeeper", "Gatekeeper (execute) did not accept", FAKE_SPCTL_VERDICT="rejected")


def test_an_ad_hoc_signature_line_on_an_otherwise_good_item_is_caught(bench: Bench) -> None:
    _fails_at(bench, "verify-signatures", "deeper/_x.so: ad hoc", FAKE_ADHOC_FOR="_x.so")
    assert bench.of("notarytool-submit") == []


def test_a_test_signature_runs_right_before_the_first_real_one_with_its_bound(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, float]] = []
    original = sign_bundle.Tools.run

    def recording(self, name, *args, timeout=sign_bundle.DEFAULT_TIMEOUT, sink=None):
        if name == "codesign" and "--sign" in args:
            seen.append((Path(args[-1]).name, timeout))
        return original(self, name, *args, timeout=timeout, sink=sink)

    monkeypatch.setattr(sign_bundle.Tools, "run", recording)
    assert sign_bundle.main(bench.argv()) == 0
    assert seen[0] == ("probe", sign_bundle.modes.PROBE_SIGN_TIMEOUT) and sign_bundle.modes.PROBE_SIGN_TIMEOUT <= 120
    assert all(name != "probe" for name, _ in seen[1:])
    steps = [entry["step"] for entry in bench.evidence_json()["steps"]]
    assert steps.index("test-signature") == steps.index("sign-inner") - 1


def test_a_failing_test_signature_stops_before_any_real_signature(bench: Bench) -> None:
    _fails_at(bench, "test-signature", "a test signature with the identity failed", FAKE_FAIL="codesign-sign")
    assert len(bench.probes()) == 1 and bench.signs() == []


def test_the_test_tool_path_is_the_first_line_of_build_info(bench: Bench) -> None:
    assert sign_bundle.main(bench.argv()) == 0
    first = (bench.out / "BUILD-INFO.txt").read_text(encoding="utf-8").splitlines()[0]
    assert first == "TEST TOOLS (HYPRIAL_SIGN_BUNDLE_TOOL_PATH set): NOT a release"


@pytest.mark.parametrize("case", ["out-under-evidence", "root-parent-under-out", "root-parent-under-unfinished"])
def test_out_under_evidence_and_root_parent_under_the_output_are_usage_errors(bench: Bench, case: str) -> None:
    argv = bench.argv()
    if case == "out-under-evidence":
        argv[argv.index("--out") + 1] = str(bench.evidence / "signed")
    else:
        base = bench.out if case == "root-parent-under-out" else bench.unfinished
        argv[argv.index("--root-parent") + 1] = str(base / "parent")
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(argv)
    assert raised.value.code == 2
    assert bench.calls() == [] and not bench.evidence.exists()
    assert not bench.out.exists() and not bench.unfinished.exists()
