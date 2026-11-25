"""sign_bundle.py ``--check-only`` and ``--verify-only`` (milestone 2).

The packaging script runs ``--check-only`` before the long build and
``--verify-only`` on the finished output, so it never calls an Apple tool
itself. Pinned here: the check signs only a scratch probe (bounded, then
removed), submits nothing and asks the notary service only for ``history``;
a wrong identity, a probe signature that fails (a locked keychain) or a
profile that does not answer is a refusal (2). The verification re-runs every check on the
finished directory (signatures of the app, every inner item and the dmg,
``stapler validate`` of both, both Gatekeeper assessments, SHA256SUMS, the
manifest, the Team ID in BUILD-INFO, the launch check), changes nothing, and
fails (1) when any of them fails. Both modes refuse the wrong arguments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import OTHER_SHA1, PROFILE, SHA1, Bench, tree
from tests.tooling.release.shell.support import load

sign_bundle = load("sign_bundle")
MUTATING = ("codesign-sign", "notarytool-submit", "stapler-staple", "hdiutil-create", "ditto", "ditto-zip")


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    return Bench(tmp_path).install(monkeypatch, sign_bundle)


def _check(bench: Bench, *extra: str, identity: str | None = SHA1, profile: str = PROFILE) -> int:
    argv = ["--check-only", "--notary-profile", profile, "--evidence", str(bench.evidence)]
    if identity is not None:
        argv += ["--identity", identity]
    return sign_bundle.main(argv + list(extra))


def _verify(bench: Bench, *extra: str) -> int:
    return sign_bundle.main(["--verify-only", "--identity", SHA1, "--out", str(bench.out), "--evidence", str(bench.evidence), "--root-parent", str(bench.tmp), *extra])


def _signed(bench: Bench) -> None:
    assert sign_bundle.main(bench.argv()) == 0, bench.evidence_json()
    for name in ("calls.jsonl", "notary-count"):
        (bench.state / name).unlink(missing_ok=True)
    bench.evidence.unlink()


def test_check_only_signs_a_scratch_probe_asks_for_the_history_and_nothing_else(bench: Bench) -> None:
    assert _check(bench) == 0, bench.evidence_json()
    names = bench.names()
    assert [name for name in names if name not in {"uname", "security"}] == ["codesign-sign", "codesign-verify", "notarytool-history"]
    probe = bench.of("codesign-sign")[0]["argv"]
    assert probe[:4] == ["--force", "--timestamp", "--sign", SHA1] and probe[-1].endswith("/probe")
    assert not Path(probe[-1]).exists() and not Path(probe[-1]).parent.exists()
    assert [step["step"] for step in bench.evidence_json()["steps"]] == ["test-signature", "notary-profile"]
    history = bench.of("notarytool-history")[0]["argv"]
    assert history == ["notarytool", "history", "--keychain-profile", PROFILE, "--output-format", "json"]
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "PASS" and evidence["mode"] == "check"
    assert SHA1 not in bench.evidence.read_text(encoding="utf-8")
    assert not bench.out.exists() and not bench.unfinished.exists()


def test_check_only_discovers_the_single_identity(bench: Bench) -> None:
    assert _check(bench, "--discover-identity", identity=None) == 0


@pytest.mark.parametrize(("identity", "profile", "knob", "needle"), [
    ("C" * 40, PROFILE, "", "Developer ID Application"),
    (OTHER_SHA1, PROFILE, "", "Developer ID Application"),
    (SHA1, "has space", "", "profile"),
    (SHA1, PROFILE, "notarytool-history", "notary profile does not answer"),
    (SHA1, PROFILE, "codesign-sign", "test signature with the identity failed"),
])
def test_check_only_refuses_a_wrong_identity_or_a_profile_that_does_not_answer(bench: Bench, identity: str, profile: str, knob: str, needle: str) -> None:
    bench.knob(FAKE_FAIL=knob)
    assert _check(bench, identity=identity, profile=profile) == 2
    for name in MUTATING:
        if name != "codesign-sign":
            assert name not in bench.names()
    assert all(call["argv"][-1].endswith("/probe") for call in bench.of("codesign-sign"))
    if knob == "codesign-sign":
        assert "notarytool-history" not in bench.names()
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "REFUSED"
    assert any(needle in problem for problem in evidence["problems"]), evidence["problems"]


def test_a_probe_signature_that_hangs_fails_within_its_bound(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    real = sign_bundle.Tools.run
    seen = []

    def hanging(self, name, *args, timeout=sign_bundle.DEFAULT_TIMEOUT):
        if name == "codesign" and "--sign" in args:
            seen.append(timeout)
            raise sign_bundle.SignError("codesign --force timeout after 120s (a keychain dialog may be waiting)")
        return real(self, name, *args, timeout=timeout)

    monkeypatch.setattr(sign_bundle.Tools, "run", hanging)
    assert _check(bench) == 2
    assert seen == [sign_bundle.modes.PROBE_SIGN_TIMEOUT] and sign_bundle.modes.PROBE_SIGN_TIMEOUT <= 120
    assert any("keychain dialog" in problem for problem in bench.evidence_json()["problems"])


def test_every_child_of_the_check_has_a_timeout_and_no_stdin(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    real = sign_bundle.subprocess.run
    seen = []

    def recording(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sign_bundle.subprocess, "run", recording)
    assert _check(bench) == 0
    assert len(seen) >= 4
    for kwargs in seen:
        assert kwargs.get("timeout") and kwargs["timeout"] <= sign_bundle.DEFAULT_TIMEOUT
        assert kwargs.get("stdin") is sign_bundle.subprocess.DEVNULL


@pytest.mark.parametrize("extra", [["--app", "x.app"], ["--out", "x"]])
def test_check_only_takes_no_app_and_no_out(bench: Bench, extra: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _check(bench, *extra)
    assert raised.value.code == 2


def test_verify_only_rechecks_the_finished_output_and_changes_nothing(bench: Bench) -> None:
    _signed(bench)
    before = tree(bench.out)
    assert _verify(bench) == 0, bench.evidence_json()
    assert tree(bench.out) == before
    names = bench.names()
    for name in MUTATING:
        assert name not in names
    assert names.count("stapler-validate") == 2 and names.count("spctl") == 2
    verified = [call["argv"][-1] for call in bench.of("codesign-verify")]
    app = str(bench.out / "app" / "HyprialOS.app")
    assert app in verified and any(item.endswith("-signed.dmg") for item in verified)
    shown = {call["argv"][-1] for call in bench.of("codesign-show")}
    assert len(shown) >= 7
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "PASS" and evidence["mode"] == "verify"
    assert [step["step"] for step in evidence["steps"]] == ["verify-signatures", "stapler-validate", "gatekeeper", "checksums", "runtime-launch"]
    assert evidence["verified"]["dmg"].endswith("_aarch64-signed.dmg")


@pytest.mark.parametrize("damage", ["stapler-validate", "spctl", "codesign-verify", "sums", "manifest", "team", "source", "launch"])
def test_verify_only_fails_when_any_check_fails(bench: Bench, monkeypatch: pytest.MonkeyPatch, damage: str) -> None:
    _signed(bench)
    if damage in {"stapler-validate", "spctl", "codesign-verify"}:
        bench.knob(FAKE_FAIL=damage)
    elif damage == "source":
        bench.knob(FAKE_SPCTL_SOURCE="Developer ID")
    elif damage == "sums":
        (bench.out / "SHA256SUMS").write_text("0" * 64 + "  x.dmg\n", encoding="utf-8")
    elif damage == "manifest":
        (bench.out / "runtime-manifest.json").write_text("{}\n", encoding="utf-8")
    elif damage == "team":
        info = bench.out / "BUILD-INFO.txt"
        info.write_text(info.read_text(encoding="utf-8").replace("team id: ", "team id: X"), encoding="utf-8")
    else:
        monkeypatch.setattr(sign_bundle, "runtime_check", lambda runtime, root_parent: (1, "FAIL launch", {}))
    assert _verify(bench) == 1
    assert bench.evidence_json()["verdict"] == "FAIL"
    assert bench.out.is_dir()


def test_verify_only_refuses_a_directory_that_is_not_a_signed_output(bench: Bench) -> None:
    bench.out.mkdir()
    assert _verify(bench) == 2
    assert bench.evidence_json()["verdict"] == "REFUSED"


@pytest.mark.parametrize("argv", [
    ["--verify-only", "--identity", SHA1, "--evidence", "e.json"],
    ["--verify-only", "--identity", SHA1, "--out", "o", "--app", "a.app", "--evidence", "e.json"],
    ["--identity", SHA1, "--notary-profile", PROFILE, "--out", "o", "--evidence", "e.json"],
    ["--identity", SHA1, "--app", "a.app", "--out", "o", "--evidence", "e.json"],
    ["--check-only", "--verify-only", "--identity", SHA1, "--notary-profile", PROFILE, "--evidence", "e.json"],
])
def test_each_mode_refuses_the_arguments_of_another(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(argv)
    assert raised.value.code == 2
