from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
from hyprial.daemon.impl.harnesses.actor import HarnessProjection, _Record
from hyprial.daemon.impl.harnesses.dsh import DshApiError, DshHarnessProcess, dsh_worker_home, prepare_worker_home
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

from ..support import (
    _complete_launch,
    _install_fake_dsh,
    _ready_or_fail,
    _spec,
    _wait_for,
)


def test_complete_environment_replaces_ambient_and_survives_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    capture = tmp_path / "complete-env.jsonl"
    sentinel = "SENTINEL-GRANTED-DSH-KEY"
    monkeypatch.setenv("P1BB2_AMBIENT_LEAK", "must-not-reach-child")
    home = dsh_worker_home(tmp_path / "state", "worker-complete")
    launch = _complete_launch(
        {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "agent-home"),
            "DEEPSEEK_API_KEY": sentinel,
            "FAKE_DSH_ENV_CAPTURE": str(capture),
            "FAKE_DSH_MODE": "exit-after-prompt",
        }
    )
    process = DshHarnessProcess(
        _spec("worker-complete"), dsh_home=home, complete_launch=launch
    )
    try:
        _ready_or_fail(process)
        first_generation = process._generation
        assert first_generation is not None
        from hyprial.daemon.impl.api import HarnessDelivery

        assert process.enqueue(
            HarnessDelivery(
                delivery_id="complete-env-reconnect",
                conversation_id="complete-env-reconnect",
                sender="agent:test:sender",
                recipient="agent:test:dsh",
                message="trigger reconnect",
            )
        )
        assert _wait_for(
            lambda: process._generation is not None
            and process._generation is not first_generation,
            timeout=15,
        ), process.last_error
        assert _wait_for(
            lambda: capture.exists()
            and len(capture.read_text(encoding="utf-8").splitlines()) >= 2,
            timeout=10,
        )
        generations = [
            json.loads(line)
            for line in capture.read_text(encoding="utf-8").splitlines()
        ]
        assert len(generations) >= 2
        for environment in generations[:2]:
            assert environment["DEEPSEEK_API_KEY"] == sentinel
            assert environment["ambient"] is None
            assert environment["DSH_HOME"] == str(home)
            assert environment["HOME"] == str(tmp_path / "agent-home")
            assert environment["PATH"] == os.environ["PATH"]
    finally:
        process.stop()


def test_complete_environment_secret_is_redacted_from_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    sentinel = "SENTINEL-COMPLETE-KEY-7f9a"
    home = dsh_worker_home(tmp_path / "state", "worker-complete-redact")
    launch = _complete_launch(
        {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "agent-home"),
            "DEEPSEEK_API_KEY": sentinel,
            "FAKE_DSH_MODE": "leak-key",
            "FAKE_DSH_LEAK": "DEEPSEEK_API_KEY",
        }
    )
    process = DshHarnessProcess(
        _spec("worker-complete-redact"), dsh_home=home, complete_launch=launch
    )
    try:
        assert not process.wait_ready(timeout=6)
        assert _wait_for(
            lambda: b"[REDACTED]" in process.stderr_tail(), timeout=6
        ), process.stderr_tail()
        assert sentinel.encode() not in process.stderr_tail()
        assert sentinel.encode() not in process.io_log()
        assert sentinel not in (process.last_error or "")
    finally:
        process.stop()


@pytest.mark.parametrize("missing", ["PATH", "HOME"])
def test_complete_environment_requires_dsh_runtime_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "agent-home"),
    }
    environment.pop(missing)
    home = dsh_worker_home(tmp_path / "state", f"worker-missing-{missing.lower()}")
    with pytest.raises(DshApiError, match=missing):
        DshHarnessProcess(
            _spec(f"worker-missing-{missing.lower()}"),
            dsh_home=home,
            complete_launch=_complete_launch(environment, grants=()),
        )


def test_complete_environment_refuses_partial_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    launch = _complete_launch(
        {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "agent-home")},
        grants=(),
    )
    home = dsh_worker_home(tmp_path / "state", "worker-conflicting-env")
    process: DshHarnessProcess | None = None
    try:
        with pytest.raises(ValueError, match="partial env mapping"):
            process = DshHarnessProcess(
                _spec("worker-conflicting-env"),
                dsh_home=home,
                env={"LEGACY": "1"},
                complete_launch=launch,
            )
    finally:
        if process is not None:
            process.stop()


def test_complete_environment_receipt_and_mcp_preset_exclude_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    sentinel = "SENTINEL-PRESET-MUST-NOT-CARRY"
    channel = build_worker_channel(
        actor="agent:test:node:worker-receipt",
        session_ref="receipt-session",
        hyprial_home=tmp_path / "hyprial-home",
        state_dir=tmp_path / "state",
        node_id="node",
        owner="test",
    )
    home = dsh_worker_home(channel.state_dir, "worker-receipt")
    launch = _complete_launch(
        {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "agent-home"),
            "DEEPSEEK_API_KEY": sentinel,
        },
        actor=channel.actor,
        grants=(("deepseek-worker-receipt-r1", 7),),
    )
    process = DshHarnessProcess(
        _spec("worker-receipt"),
        dsh_home=home,
        worker_channel=channel,
        complete_launch=launch,
    )
    try:
        _ready_or_fail(process)
        logger = process._logger
        assert logger is not None
        entries = [
            json.loads(line)
            for line in logger.path.read_text(encoding="utf-8").splitlines()
        ]
        receipts = [
            entry for entry in entries if entry["event"] == "worker.environment.receipt"
        ]
        assert receipts
        receipt = receipts[-1]
        assert receipt["actor"] == channel.actor
        assert receipt["grants"] == [
            {"grantId": "deepseek-worker-receipt-r1", "revision": 7}
        ]
        preset = next(home.glob(".agent-presets/*/agent.cordis.yml"))
        persisted = preset.read_text(encoding="utf-8")
        assert "DEEPSEEK_API_KEY" not in persisted
        assert sentinel not in persisted
        assert sentinel not in logger.path.read_text(encoding="utf-8")
    finally:
        process.stop()


def test_two_workers_get_distinct_ports_and_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_dsh(tmp_path, monkeypatch)
    first_home = dsh_worker_home(tmp_path / "state", "worker-1")
    second_home = dsh_worker_home(tmp_path / "state", "worker-2")
    assert first_home != second_home
    first = DshHarnessProcess(_spec("worker-1"), dsh_home=first_home)
    second = DshHarnessProcess(_spec("worker-2"), dsh_home=second_home)
    try:
        _ready_or_fail(first)
        _ready_or_fail(second)
        assert first.endpoint is not None and second.endpoint is not None
        assert first.endpoint != second.endpoint
        assert first.dsh_home != second.dsh_home
    finally:
        first.stop()
        second.stop()


def test_credentials_stay_in_env_and_never_reach_argv_log_or_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "SENTINEL_DEEPSEEK_KEY_9d3f"
    _install_fake_dsh(tmp_path, monkeypatch)
    key_capture = tmp_path / "child-key.txt"
    monkeypatch.setenv("DEEPSEEK_API_KEY", sentinel)
    monkeypatch.setenv("FAKE_DSH_KEY_CAPTURE", str(key_capture))
    home = dsh_worker_home(tmp_path / "state", "worker-cred")
    spec = _spec("worker-cred")
    process = DshHarnessProcess(spec, dsh_home=home)
    try:
        _ready_or_fail(process)
        # Positive control: the child really received the key via env.
        assert key_capture.read_text(encoding="utf-8") == sentinel
        # Negative: nowhere hyprial writes or reports.
        argv = "\0".join(process.argv or ())
        assert sentinel not in argv
        assert sentinel not in process.io_log().decode("utf-8", errors="replace")
        projection = HarnessProjection()
        projection.publish(
            {"dsh:worker-cred": _Record(spec=spec, process=process)}, 1
        )
        status_json = json.dumps(projection.status())
        assert sentinel not in status_json
        assert "dshHome" in status_json
    finally:
        process.stop()


def test_worker_home_patch_disables_the_web_surface_context(
    tmp_path: Path,
) -> None:
    home = dsh_worker_home(tmp_path / "state", "worker-patch")
    prepare_worker_home(home)
    patch = home / "profiles" / "web" / "cordis.patch.yml"
    text = patch.read_text(encoding="utf-8")
    assert "surfaceContext: false" in text
    assert "printUrl: true" in text
    assert "trustedHosts: []" in text
    # Idempotent: a second prepare keeps the managed content.
    prepare_worker_home(home)
    assert patch.read_text(encoding="utf-8") == text


def test_an_unsupported_dsh_fails_fast_and_names_the_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dsh 0.1.5 changed its client API (token-to-cookie login, new routes and
    envelope).  Until HYPRIAL speaks it, a worker on that dsh must fail at
    once with a readable reason -- not sit "never ready" like a hang -- and
    the launch token in its banner must never reach a log or an error."""

    import time

    _install_fake_dsh(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_DSH_MODE", "token-banner")
    monkeypatch.setenv("FAKE_DSH_VERSION", "0.1.5-rc.3")
    home = dsh_worker_home(tmp_path / "state", "worker-new-dsh")
    process = DshHarnessProcess(_spec(), dsh_home=home)
    try:
        started = time.monotonic()
        # The readable reason must appear at once -- well inside the banner
        # budget -- instead of the worker sitting "never ready".
        while time.monotonic() - started < 5 and "DSH_UNSUPPORTED_VERSION" not in (
            process.last_error or ""
        ):
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        assert not process.wait_ready(timeout=0.1)
        last_error = process.last_error or ""
        assert "DSH_UNSUPPORTED_VERSION" in last_error
        assert "0.1.5-rc.3" in last_error  # what is installed
        assert "0.1.0-rc.7" in last_error  # what is known to work
        assert "npm" in last_error  # what to do
        assert elapsed < 5, "the reason did not surface at once"
        for text in (last_error, bytes(process.io_log()).decode("utf-8", "replace"),
                     bytes(process.stderr_tail()).decode("utf-8", "replace")):
            assert "SENTINEL-DSH-LAUNCH-TOKEN" not in text
    finally:
        process.stop()
