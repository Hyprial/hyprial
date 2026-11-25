"""sign_bundle.py end to end against fake macOS tools, on every platform.

The whole flow runs in-process (``main``) with the fake tools of
``bundle_fakes`` on the tool PATH; the launch check of the signed runtime
(``runtime_check``, the real one runs verify_runtime.py) is replaced by a
recorder. Pinned here: inside-out signing with hardened runtime and a
secure timestamp, never ``--deep`` and never an entitlement or an ad-hoc
identity; the manifest regenerated before the app signature; the order
notarize app zip, staple app, build dmg, sign dmg, notarize dmg, staple dmg,
Gatekeeper, then the app inside the mounted final dmg; the exact output
naming and file set; fail-closed on every failure with no output directory
left (or, with --keep-unfinished, every signed copy renamed -UNFINISHED and
FAILED.txt stating what was notarized); the input bundle untouched; no home
path in what the tool prints or records; a test tool PATH announced and
recorded; an interrupted notary wait keeps its submission id.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import MACHO, PROFILE, RUNTIME_MACHOS, SHA1, TEAM, VERSION, Bench, tree, write_manifest, write_zip
from tests.tooling.release.shell.support import load

sign_bundle = load("sign_bundle")
DMG = f"HyprialOS_{VERSION}_aarch64-signed.dmg"


class RuntimeCheck:
    """Stand-in for the launch check of the signed runtime."""

    def __init__(self, bench: Bench, code: int = 0) -> None:
        self.bench = bench
        self.code = code
        self.seen: list[dict] = []

    def __call__(self, runtime: Path, root_parent: Path):
        problems, _ = sign_bundle.verify_runtime.check_manifest(runtime)
        self.seen.append({"runtime": runtime, "at": len(self.bench.calls()), "problems": problems})
        line = "PASS" if self.code == 0 else "FAIL version"
        return self.code, line, {"verdict": line}


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    bench = Bench(tmp_path)
    return bench.install(monkeypatch, sign_bundle, RuntimeCheck(bench))


def _run(bench: Bench, *extra: str, **knobs: str) -> int:
    bench.knob(**knobs)
    return sign_bundle.main(bench.argv(*extra))


def _signs(bench: Bench) -> list[list[str]]:
    return bench.signs()


def test_a_signed_bundle_comes_out_under_a_name_no_ad_hoc_build_uses(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(bench) == 0, bench.evidence_json()
    assert not bench.unfinished.exists()
    assert sorted(path.name for path in bench.out.iterdir()) == sorted(["app", "BUILD-INFO.txt", DMG, "runtime-manifest.json", "SHA256SUMS"])
    assert [path.name for path in (bench.out / "app").iterdir()] == ["HyprialOS.app"]
    assert (bench.out / "app" / "HyprialOS.app" / "Contents" / "Info.plist").is_file()
    sums = (bench.out / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sums == [f"{hashlib.sha256((bench.out / name).read_bytes()).hexdigest()}  {name}" for name in (DMG, "runtime-manifest.json")]
    info = (bench.out / "BUILD-INFO.txt").read_text(encoding="utf-8")
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "PASS"
    assert "Developer ID" in info and "hardened runtime" in info and "not ad hoc" in info
    assert f"team id: {TEAM}" in info
    for submission in evidence["notary"]:
        assert submission["status"] == "Accepted"
        assert submission["id"] in info
    assert [submission["label"] for submission in evidence["notary"]] == ["app", "dmg"]
    assert "\n".join(sums) in info
    assert ".unfinished" not in info and str(bench.tmp) not in info
    assert f"gatekeeper dmg: {DMG}: accepted source=Notarized Developer ID" in info
    assert SHA1 not in info and SHA1 not in json.dumps(evidence)
    printed = capsys.readouterr()
    for text in (printed.out, printed.err, bench.evidence.read_text(encoding="utf-8"), info):
        assert str(bench.tmp) not in text and os.path.realpath(bench.tmp) not in text
    assert "~/" in printed.out and "~/" in json.dumps(evidence)
    assert f"{bench.evidence.stem}-notary-" not in json.dumps(evidence["commands"])
    assert "HYPRIAL_SIGN_BUNDLE_TOOL_PATH" in printed.err and "test only" in printed.err
    assert evidence["tools"] == "overridden"
    assert "tools: overridden" in info and "NOT a release" in info


def test_signing_is_inside_out_hardened_timestamped_never_deep_and_without_entitlements(bench: Bench) -> None:
    assert _run(bench) == 0
    signs = _signs(bench)
    app = bench.unfinished / "app" / "HyprialOS.app"
    runtime = app / "Contents" / "Resources" / "runtime"
    targets = [Path(argv[-1]) for argv in signs]
    inner = targets[: len(RUNTIME_MACHOS)]
    assert {target.relative_to(runtime).as_posix() for target in inner} == RUNTIME_MACHOS
    assert [len(target.parts) for target in inner] == sorted((len(target.parts) for target in inner), reverse=True)
    assert targets[len(inner)] == app
    assert targets[len(inner) + 1].name == DMG
    assert len(targets) == len(inner) + 2
    for argv in signs:
        assert "--deep" not in argv
        assert argv[argv.index("--sign") + 1] == SHA1
        assert "--force" in argv and "--timestamp" in argv
        assert not any(arg.startswith("--timestamp=") for arg in argv)
        assert "--entitlements" not in argv
    for argv in signs[:-1]:
        assert argv[argv.index("--options") + 1] == "runtime"
    verifies = [call["argv"] for call in bench.of("codesign-verify") if Path(call["argv"][-1]) == app]
    assert any("--deep" in argv and "--strict" in argv for argv in verifies)


def test_the_manifest_is_regenerated_after_inner_signing_and_before_the_app_signature(bench: Bench) -> None:
    before = (bench.app / "Contents" / "Resources" / "runtime" / "runtime-manifest.json").read_bytes()
    assert _run(bench) == 0
    app_sign = next(call for call in bench.of("codesign-sign") if call["argv"][-1].endswith("HyprialOS.app"))
    final = (bench.out / "runtime-manifest.json").read_bytes()
    assert app_sign["manifest_sha256"] == hashlib.sha256(final).hexdigest()
    assert final == (bench.out / "app" / "HyprialOS.app" / "Contents" / "Resources" / "runtime" / "runtime-manifest.json").read_bytes()
    assert final != before
    manifest = bench.evidence_json()["manifest"]
    assert manifest["beforeSha256"] == hashlib.sha256(before).hexdigest()
    assert manifest["afterSha256"] == hashlib.sha256(final).hexdigest()
    assert manifest["changedFiles"] == len(RUNTIME_MACHOS)
    check = sign_bundle.runtime_check
    assert len(check.seen) == 1 and check.seen[0]["problems"] == []
    assert check.seen[0]["at"] < bench.names().index("notarytool-submit")


def test_the_app_is_notarized_and_stapled_before_the_dmg_is_built_signed_notarized_and_stapled(bench: Bench) -> None:
    assert _run(bench) == 0
    names = [name for name in bench.names() if name in {"ditto-zip", "notarytool-submit", "notarytool-wait", "stapler-staple", "stapler-validate", "hdiutil-create", "codesign-sign", "spctl", "hdiutil-attach", "hdiutil-detach"}]
    tail = names[names.index("ditto-zip"):]
    assert tail == [
        "ditto-zip", "notarytool-submit", "notarytool-wait", "stapler-staple", "stapler-validate",
        "hdiutil-create", "codesign-sign", "notarytool-submit", "notarytool-wait", "stapler-staple", "stapler-validate",
        "spctl", "spctl", "hdiutil-attach", "stapler-validate", "spctl", "hdiutil-detach",
    ]
    submits = [call["argv"] for call in bench.of("notarytool-submit")]
    assert Path(submits[0][2]).suffix == ".zip" and Path(submits[1][2]).name == DMG
    for argv in submits:
        assert argv == ["notarytool", "submit", argv[2], "--keychain-profile", PROFILE, "--output-format", "json"]
    submitted = [line.split()[0] for line in (bench.state / "submitted.txt").read_text(encoding="utf-8").splitlines()]
    waits = [call["argv"] for call in bench.of("notarytool-wait")]
    assert [argv[2] for argv in waits] == submitted == [record["id"] for record in bench.evidence_json()["notary"]]
    for argv in waits:
        assert argv == ["notarytool", "wait", argv[2], "--keychain-profile", PROFILE, "--timeout", "30m", "--output-format", "json"]
    for call in bench.calls():
        for forbidden in ("--password", "--key", "--key-id", "--issuer", "--apple-id", "--team-id"):
            assert forbidden not in call["argv"], call["name"]
    zipped = bench.of("ditto-zip")[0]["argv"]
    assert "-c" in zipped and "-k" in zipped and "--keepParent" in zipped
    spctl = [call["argv"] for call in bench.of("spctl")]
    assert spctl[0][:3] == ["--assess", "--type", "execute"] and spctl[0][-1].endswith("HyprialOS.app")
    assert spctl[1][:5] == ["--assess", "--type", "open", "--context", "context:primary-signature"] and spctl[1][-1].endswith(DMG)


def test_the_dmg_holds_the_stapled_app_and_an_applications_link(bench: Bench) -> None:
    assert _run(bench) == 0
    image = bench.image(DMG)
    assert sorted(path.name for path in image.iterdir()) == ["Applications", "HyprialOS.app"]
    assert (image / "Applications").is_symlink() and str((image / "Applications").readlink()) == "/Applications"
    assert (image / "HyprialOS.app" / "Contents" / "CodeResources").read_text(encoding="utf-8") == "ticket"
    create = bench.of("hdiutil-create")[0]["argv"]
    assert create[create.index("-volname") + 1] == "HyprialOS"


def test_the_input_bundle_is_never_modified(bench: Bench) -> None:
    before = tree(bench.app)
    assert _run(bench) == 0
    assert tree(bench.app) == before
    for argv in _signs(bench):
        assert not Path(argv[-1]).is_relative_to(bench.app)


@pytest.mark.parametrize("statuses", ["Invalid", "Accepted,Invalid", "Rejected", "In Progress"])
def test_a_notarization_that_is_not_accepted_fails_closed_with_its_log_saved(bench: Bench, statuses: str) -> None:
    code = _run(bench, FAKE_NOTARY=statuses)
    assert code == 1
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "FAIL"
    assert not bench.out.exists() and not bench.unfinished.exists()
    failing = evidence["notary"][-1]
    assert failing["status"] != "Accepted"
    log = bench.evidence.parent / failing["log"]
    assert log.is_file() and "/" not in failing["log"]
    assert json.loads(log.read_text(encoding="utf-8"))["jobId"] == failing["id"]
    if statuses.startswith("Accepted"):
        assert len(bench.of("notarytool-submit")) == 2
    else:
        assert len(bench.of("notarytool-submit")) == 1
        assert "hdiutil-create" not in bench.names()


def test_a_notary_wait_that_runs_out_keeps_the_submission_id_and_says_not_to_resubmit(bench: Bench) -> None:
    assert _run(bench, FAKE_NOTARY="timeout") == 1
    evidence = bench.evidence_json()
    record = evidence["notary"][0]
    assert record["id"] == (bench.state / "timeout-id").read_text(encoding="utf-8")
    assert record["exit"] == 124
    assert "Timeout" in record["detail"]
    assert any("do not resubmit" in problem and record["id"] in problem for problem in evidence["problems"])
    assert len(bench.of("notarytool-submit")) == 1
    assert not bench.out.exists() and not bench.unfinished.exists()


def test_the_notary_wait_can_be_lengthened_but_not_dropped(bench: Bench) -> None:
    assert _run(bench, "--notary-timeout-minutes", "90") == 0
    waits = bench.of("notarytool-wait")
    assert len(waits) == 2
    for call in waits:
        assert call["argv"][call["argv"].index("--timeout") + 1] == "90m"
    with pytest.raises(SystemExit):
        sign_bundle.main(bench.argv("--notary-timeout-minutes", "0"))


@pytest.mark.parametrize("failing", ["codesign-sign", "codesign-verify", "notarytool-submit", "stapler-staple", "stapler-validate", "hdiutil-create", "ditto", "spctl", "timestamp"])
def test_any_failing_tool_fails_closed_and_never_falls_back_to_ad_hoc(bench: Bench, failing: str) -> None:
    code = _run(bench, FAKE_FAIL=failing)
    assert code == 1
    assert bench.evidence_json()["verdict"] == "FAIL"
    assert not bench.out.exists() and not bench.unfinished.exists()
    for argv in _signs(bench):
        assert argv[argv.index("--sign") + 1] == SHA1
        assert "--deep" not in argv


def test_a_gatekeeper_verdict_from_another_source_fails(bench: Bench) -> None:
    assert _run(bench, FAKE_SPCTL_SOURCE="Developer ID") == 1
    assert not bench.out.exists()
    assert any("Notarized Developer ID" in problem for problem in bench.evidence_json()["problems"])


def test_a_failing_launch_of_the_signed_runtime_stops_before_any_submission(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sign_bundle, "runtime_check", RuntimeCheck(bench, code=1))
    assert _run(bench) == 1
    assert "notarytool-submit" not in bench.names()
    assert not bench.out.exists() and not bench.unfinished.exists()


def test_an_archive_holding_mach_o_stops_before_any_signature(bench: Bench) -> None:
    runtime = bench.app / "Contents" / "Resources" / "runtime"
    write_zip(runtime / "lib" / "bundled.whl", {"x/_c.so": MACHO + b"in wheel"})
    write_manifest(runtime)
    assert _run(bench) == 1
    assert "codesign-sign" not in bench.names()
    evidence = bench.evidence_json()
    assert evidence["archives"]["withMacho"] == ["Contents/Resources/runtime/lib/bundled.whl"]
    assert not bench.out.exists() and not bench.unfinished.exists()


@pytest.mark.parametrize(("knobs", "app_notarized"), [({"FAKE_NOTARY": "Invalid"}, False), ({"FAKE_FAIL": "hdiutil-create"}, True), ({"FAKE_NOTARY": "Accepted,Invalid"}, True)])
def test_keep_unfinished_marks_every_signed_copy_and_states_what_was_notarized(bench: Bench, knobs: dict, app_notarized: bool) -> None:
    assert _run(bench, "--keep-unfinished", **knobs) == 1
    assert not bench.out.exists()
    failed = (bench.unfinished / "FAILED.txt").read_text(encoding="utf-8")
    entries = [path for path in bench.unfinished.rglob("*") if path.suffix in {".app", ".zip", ".dmg"} and not any(part.endswith(".app") for part in path.relative_to(bench.unfinished).parts[:-1])]
    assert entries and all(path.stem.endswith("-UNFINISHED") and path.suffix != ".dmg" for path in entries), entries
    assert (bench.unfinished / "app" / "HyprialOS-UNFINISHED.app").is_dir()
    submissions = bench.evidence_json()["notary"]
    app_id = submissions[0]["id"] if submissions else ""
    assert f"app notarized: {'yes' if app_notarized else 'no'}" in failed
    assert f"app stapled: {'yes' if app_notarized else 'no'}" in failed
    assert "dmg notarized: no" in failed and "not a release" in failed
    assert all(item["id"] in failed for item in submissions)
    if not app_notarized:
        assert app_id and (bench.unfinished / "work" / "HyprialOS-UNFINISHED.zip").is_file()
    assert str(bench.tmp) not in failed


@pytest.mark.parametrize("knobs", [{"FAKE_NOTARY": "timeout"}, {"FAKE_NOTARY_EXIT": "1"}])
def test_keep_unfinished_says_unknown_when_a_submission_may_still_be_accepted(bench: Bench, knobs: dict) -> None:
    assert _run(bench, "--keep-unfinished", **knobs) == 1
    failed = (bench.unfinished / "FAILED.txt").read_text(encoding="utf-8")
    record = bench.evidence_json()["notary"][0]
    assert record["id"] and f"app notarized: unknown (submission {record['id']}" in failed
    assert "app stapled: no" in failed and "dmg notarized: no" in failed


def test_a_cleanup_that_leaves_the_directory_behind_is_reported(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sign_bundle.shutil, "rmtree", lambda *args, **kwargs: None)
    assert _run(bench, FAKE_FAIL="ditto") == 1
    evidence = bench.evidence_json()
    assert bench.unfinished.exists()
    assert evidence["cleanup"] == "incomplete"
    assert any("left behind" in problem for problem in evidence["problems"])


def test_a_failed_notary_log_fetch_is_recorded_not_hidden(bench: Bench) -> None:
    assert _run(bench, FAKE_FAIL="notarytool-log") == 0
    for record in bench.evidence_json()["notary"]:
        assert "log" not in record and record["logError"].startswith("exit 1")


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_the_notary_wait_keeps_the_submission_id(bench: Bench, number: int) -> None:
    started = bench.state / "hang-started"

    def interrupt() -> None:
        deadline = time.monotonic() + 60
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if started.exists():
            os.kill(os.getpid(), number)

    sender = threading.Thread(target=interrupt, daemon=True)
    sender.start()
    code = _run(bench, FAKE_NOTARY="hang")
    sender.join()
    assert code == 1
    evidence = bench.evidence_json()
    job = (bench.state / "hang-id").read_text(encoding="utf-8")
    assert evidence["notary"][0]["id"] == job
    assert any(f"signal {int(number)}" in problem and job in problem and "do not resubmit" in problem for problem in evidence["problems"])
    assert not bench.out.exists() and not bench.unfinished.exists()


def test_the_tool_source_never_signs_ad_hoc() -> None:
    source = (sign_bundle.HERE / "sign_bundle.py").read_text(encoding="utf-8")
    assert '"--sign", "-"' not in source and "'--sign', '-'" not in source
    assert '"--deep", "--sign"' not in source
    assert "set -x" not in source


def test_an_unexpected_error_still_fails_closed_with_evidence(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(runtime: Path, root_parent: Path):
        raise RuntimeError("launcher exploded")

    monkeypatch.setattr(sign_bundle, "runtime_check", broken)
    assert _run(bench) == 1
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "FAIL"
    assert any("RuntimeError" in problem and "launcher exploded" in problem for problem in evidence["problems"])
    assert "notarytool-submit" not in bench.names()
    assert not bench.out.exists() and not bench.unfinished.exists()


@pytest.mark.parametrize("where", ["out", "unfinished"])
def test_evidence_inside_the_output_is_refused_before_anything(bench: Bench, where: str) -> None:
    base = bench.out if where == "out" else bench.unfinished
    argv = bench.argv()
    argv[argv.index("--evidence") + 1] = str(base / "evidence.json")
    with pytest.raises(SystemExit) as raised:
        sign_bundle.main(argv)
    assert raised.value.code == 2
    assert bench.calls() == []
    assert not bench.out.exists() and not bench.unfinished.exists()
