"""The notary submission id and the evidence survive a kill; cleanup is truthful (PR #2980 review E, #2965 hints).

- The runner kills a cancelled or timed-out step's process group with
  SIGKILL, so no handler runs. Pinned: sign_bundle.py runs ``notarytool
  submit`` without ``--wait``; while the upload runs it polls ``notarytool
  history`` and saves the evidence (``verdict: INCOMPLETE``) with the new
  id as soon as Apple lists it, so a SIGKILL of the whole process group
  during the upload or during the wait leaves that id on disk. The evidence
  is also rewritten after every step.
- A second SIGTERM during cleanup is ignored and the cleanup finishes.
- A dmg that stays attached: the directory stays, but its signed copies are
  renamed -UNFINISHED and FAILED.txt is written, never inside the mount.
- FAILED.txt says ``stapled: yes (the staple's validation failed)`` when the
  staple worked and only its validation failed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import Bench
from tests.tooling.release.shell.support import PACKAGING, load

sign_bundle = load("sign_bundle")
RUNNER = '''
import hashlib, importlib.util, sys
spec = importlib.util.spec_from_file_location("sign_bundle_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.APPLE_ROOT_SHA256 = hashlib.sha256(b"fake apple root certificate").hexdigest()
module.runtime_check = lambda runtime, root_parent: (0, "PASS", {})
module.NOTARY_POLL_SECONDS = 0.2
raise SystemExit(module.main(sys.argv[2:]))
'''


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    return Bench(tmp_path).install(monkeypatch, sign_bundle)


def _wait_for(path: Path, seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), path


def _killed(bench: Bench, marker: str) -> tuple[subprocess.Popen[str], str]:
    runner = bench.tmp / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    env = {**os.environ, **bench.env, "HOME": str(bench.tmp)}
    process = subprocess.Popen([sys.executable, "-I", str(runner), str(PACKAGING / "sign_bundle.py"), *bench.argv()], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        _wait_for(bench.state / marker)
        submitted = [line.split()[0] for line in (bench.state / "submitted.txt").read_text(encoding="utf-8").splitlines()]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and submitted[0] not in (bench.evidence.read_text(encoding="utf-8") if bench.evidence.exists() else ""):
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    return process, output


def test_a_sigkill_during_the_upload_leaves_the_id_apple_already_lists_on_disk(bench: Bench) -> None:
    bench.knob(FAKE_SUBMIT_HANG="1")
    process, output = _killed(bench, "submit-started")
    assert process.returncode == -signal.SIGKILL
    submitted = [line.split()[0] for line in (bench.state / "submitted.txt").read_text(encoding="utf-8").splitlines()]
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "INCOMPLETE"
    assert [(record["id"], record["status"]) for record in evidence["notary"]] == [(submitted[0], "uploading")]
    assert f"notary app: submission id={submitted[0]} (in the evidence now; upload running)" in output
    assert bench.of("notarytool-wait") == []


def test_a_sigkill_of_the_process_group_during_the_wait_leaves_the_submission_id_on_disk(bench: Bench) -> None:
    bench.knob(FAKE_NOTARY="hang")
    runner = bench.tmp / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    env = {**os.environ, **bench.env, "HOME": str(bench.tmp)}
    process = subprocess.Popen([sys.executable, "-I", str(runner), str(PACKAGING / "sign_bundle.py"), *bench.argv()], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        _wait_for(bench.state / "hang-started")
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    assert process.returncode == -signal.SIGKILL
    submitted = [line.split()[0] for line in (bench.state / "submitted.txt").read_text(encoding="utf-8").splitlines()]
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "INCOMPLETE"
    assert [record["id"] for record in evidence["notary"]] == submitted and len(submitted) == 1
    assert evidence["notary"][0]["status"] == "submitted"
    assert [entry["step"] for entry in evidence["steps"] if entry["status"] == "PASS"][-1] == "runtime-launch"
    assert f"notary app: submitted id={submitted[0]}" in output
    assert not list(bench.evidence.parent.glob("*.partial"))


def test_the_evidence_is_rewritten_after_every_step(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    original = sign_bundle.Run.step

    def recording(self, name, action):
        result = original(self, name, action)
        seen.append([entry["step"] for entry in json.loads(bench.evidence.read_text(encoding="utf-8"))["steps"]])
        return result

    monkeypatch.setattr(sign_bundle.Run, "step", recording)
    assert sign_bundle.main(bench.argv()) == 0
    steps = [entry["step"] for entry in bench.evidence_json()["steps"]]
    assert seen == [steps[: index + 1] for index in range(len(steps))]


def test_a_second_sigterm_during_cleanup_is_ignored(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    original = sign_bundle.Run.fail_cleanup
    seen: list[object] = []

    def interrupted(self, reason):
        seen.append(signal.getsignal(signal.SIGTERM))
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGHUP)
        original(self, reason)

    monkeypatch.setattr(sign_bundle.Run, "fail_cleanup", interrupted)
    bench.knob(FAKE_FAIL="hdiutil-create")
    assert sign_bundle.main(bench.argv()) == 1
    assert seen == [signal.SIG_IGN]
    evidence = bench.evidence_json()
    assert evidence["verdict"] == "FAIL" and evidence["cleanup"] == "removed"
    assert not bench.unfinished.exists()
    assert signal.getsignal(signal.SIGTERM) not in {signal.SIG_IGN, sign_bundle._interrupt}


def test_a_dmg_that_stays_attached_still_gets_its_copies_marked(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    mount = bench.unfinished / "work" / "mnt"
    real = os.path.ismount
    monkeypatch.setattr(sign_bundle.os.path, "ismount", lambda path: Path(path) == mount or real(path))
    bench.knob(FAKE_FAIL="hdiutil-detach hdiutil-detach-force")
    assert sign_bundle.main(bench.argv()) == 1
    evidence = bench.evidence_json()
    assert evidence["cleanup"] == "incomplete"
    assert any("still attached" in problem for problem in evidence["problems"])
    failed = (bench.unfinished / "FAILED.txt").read_text(encoding="utf-8")
    assert "app notarized: yes" in failed and "dmg notarized: yes" in failed and "not a release" in failed
    assert (bench.unfinished / "app" / "HyprialOS-UNFINISHED.app").is_dir()
    assert not list(bench.unfinished.glob("*.dmg"))
    assert (mount / "HyprialOS.app").is_dir() and not (mount / "HyprialOS-UNFINISHED.app").exists()


def test_failed_txt_says_stapled_when_only_the_validation_failed(bench: Bench) -> None:
    bench.knob(FAKE_FAIL="stapler-validate")
    assert sign_bundle.main(bench.argv("--keep-unfinished")) == 1
    failed = (bench.unfinished / "FAILED.txt").read_text(encoding="utf-8")
    assert "app notarized: yes" in failed
    assert "app stapled: yes (the staple's validation failed)" in failed
    assert "dmg stapled: no" in failed
    assert bench.evidence_json()["stapled"] == {"app": "stapled"}


@pytest.mark.parametrize("history", ["readable", "unreadable"])
def test_an_older_submission_of_the_same_name_is_never_taken_for_this_one(bench: Bench, monkeypatch: pytest.MonkeyPatch, history: str) -> None:
    (bench.state / "submitted.txt").write_text("00000000-0000-4000-8000-000000000000 HyprialOS.zip\n", encoding="utf-8")
    monkeypatch.setattr(sign_bundle, "NOTARY_POLL_SECONDS", 0.2)
    bench.knob(FAKE_SUBMIT_SECONDS="1.5", FAKE_FAIL="notarytool-history" if history == "unreadable" else "")
    assert sign_bundle.main(bench.argv()) == 0, bench.evidence_json()["problems"]
    ids = [line.split()[0] for line in (bench.state / "submitted.txt").read_text(encoding="utf-8").splitlines()]
    records = bench.evidence_json()["notary"]
    assert [record["id"] for record in records] == ids[1:]
    assert all("historyId" not in record for record in records)
    polls = len(bench.of("notarytool-history"))
    assert (polls == 2) if history == "unreadable" else (polls > 2)
