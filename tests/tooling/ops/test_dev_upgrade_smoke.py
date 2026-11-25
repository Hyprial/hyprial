"""Tests for the host-neutral dev-upgrade smoke instrument.

The dangerous branch is ``apply``: it stops the daemon's per-home service.
These tests pin the safety boundary (a wrong host must not reach the runner),
the offline install command, the rollback decision, and the fact that the
handoff grant is never part of the script's readable output surface.  The
service seam itself is covered in ``service/``.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

import _dev_upgrade_smoke as smoke  # noqa: E402
from _dev_upgrade_smoke import base as smoke_base  # noqa: E402

from .fakes import FakeHost, build_apply  # noqa: E402


def write_wheel(path: Path, version: str, commit: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"hyprial-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: hyprial\nVersion: {version}\n",
        )
        archive.writestr(
            "hyprial/_gui/release.json",
            json.dumps({"sourceCommit": commit}),
        )
        archive.writestr("hyprial/_core/actors.abi3.so", b"\x7fELF")
        archive.writestr(
            f"hyprial-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nTag: cp312-abi3-manylinux_2_34_x86_64\n",
        )


def test_inspects_wheel_version_commit_and_native_payload(tmp_path: Path) -> None:
    wheel = tmp_path / "hyprial-0.2.0-py3-none-any.whl"
    write_wheel(wheel, "0.2.0", "abc1234")

    info, problems = smoke.inspect_wheel(wheel, None)

    assert problems == []
    assert info["version"] == "0.2.0"
    assert info["sourceCommit"] == "abc1234"
    assert info["nativeFiles"] == ["hyprial/_core/actors.abi3.so"]
    assert info["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()


def test_rejects_a_wheel_with_the_wrong_expected_sha(tmp_path: Path) -> None:
    wheel = tmp_path / "hyprial-0.2.0-py3-none-any.whl"
    write_wheel(wheel, "0.2.0", "abc1234")

    _, problems = smoke.inspect_wheel(wheel, "0" * 64)

    assert any("does not match expected" in problem for problem in problems)


def test_install_command_is_offline_and_names_the_exact_wheel() -> None:
    command = smoke.build_install_command(
        "uv",
        Path("/wheels/new.whl"),
        Path("/python"),
        [Path("/wheelhouse")],
    )

    assert command[:7] == [
        "uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--offline",
        "--no-index",
    ]
    assert command[command.index("--find-links") + 1] == "/wheelhouse"
    assert command[-1] == "/wheels/new.whl"


def test_file_install_origin_is_a_path_and_an_http_origin_is_not() -> None:
    assert smoke.install_origin_path("file:///home/user/w.whl") == Path(
        "/home/user/w.whl"
    )
    assert smoke.install_origin_path("https://example.invalid/w.whl") is None
    assert smoke.install_origin_path(None) is None


def test_ready_requires_running_expected_version_and_no_crash() -> None:
    payload = {
        "ok": True,
        "daemon": {
            "running": True,
            "pid": 123,
            "version": "0.2.0",
            "lifecycle": {"crashed": False},
        },
    }
    ready, _, _ = smoke.daemon_ready(payload, "0.2.0")
    assert ready is True

    wrong_version, detail, _ = smoke.daemon_ready(payload, "0.1.0")
    assert wrong_version is False
    assert "version" in detail

    payload["daemon"]["lifecycle"]["crashed"] = True
    crashed, detail, _ = smoke.daemon_ready(payload, "0.2.0")
    assert crashed is False
    assert "crashed" in detail


def test_cold_start_auth_is_blocked_not_a_package_failure() -> None:
    assert (
        smoke.classify_cold_start(1, "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN")
        == smoke.BLOCKED
    )
    assert smoke.classify_cold_start(1, "Traceback: boom") == smoke.FAIL


def test_redaction_keeps_the_state_label_but_drops_the_grant() -> None:
    text = (
        "onboarding.device verification_uri=https://example.invalid/device\n"
        "user_code=ABCD-1234\n"
        "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN\n"
    )

    redacted = smoke.redact_auth_material(text)

    assert "example.invalid" not in redacted
    assert "ABCD-1234" not in redacted
    assert "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN" in redacted


class NoRun:
    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []

    def run(self, argv: Sequence[str], **_: Any) -> Any:
        self.calls.append(argv)
        raise AssertionError(f"runner must not be reached: {argv!r}")


def test_apply_guard_rejects_a_wrong_host_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = NoRun()
    monkeypatch.setattr(smoke_base, "Runner", lambda: runner)
    artifacts = tmp_path / "artifacts"
    rc = smoke.main(
        [
            "apply",
            "--new-wheel",
            str(tmp_path / "does-not-exist.whl"),
            "--wheelhouse",
            str(tmp_path),
            "--expect-host",
            "definitely-not-this-host",
            "--yes",
            "--artifacts-dir",
            str(artifacts),
        ]
    )

    assert rc == smoke.EXIT_USAGE
    assert runner.calls == []
    summary = json.loads((artifacts / "summary.json").read_text())
    assert summary["result"] == "ABORTED"
    assert any(
        check["name"] == "apply.guard" and check["status"] == smoke.FAIL
        for check in summary["checks"]
    )


class CandidateNeverServes(FakeHost):
    """The candidate's daemon never answers, so readiness fails."""

    def answering_pid(self) -> int | None:
        if self.installed_version != "0.1.0":
            return None
        return super().answering_pid()


def test_apply_rolls_back_to_the_recorded_old_wheel(tmp_path: Path) -> None:
    instance, runner = build_apply(tmp_path, CandidateNeverServes)

    rc = instance.run_apply()

    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert instance.rollback["ok"] is True
    assert runner.installs == ["0.2.0", "0.1.0"]
    assert runner.active is True
    assert len(runner.service_stops()) == 2
    assert len(runner.service_starts()) == 2
    assert runner.systemctl_calls() == []
    assert any(
        check.name == "daemon.ready" and check.status == smoke.PASS
        for check in instance.checks
    )
