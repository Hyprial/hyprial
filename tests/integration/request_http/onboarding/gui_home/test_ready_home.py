"""Contract and safety-guard tests for ``tests.support.gui_home``.

Decision record (S2, 2026-11-21):
- Every safety guard in acceptance plan section 3.3 has dedicated negative
  coverage: missing/non-1 ``HYPRIAL_NETWORK_ISOLATED``, missing ``--home`` /
  ignored ``HYPRIAL_HOME``, default/real/simulated HOME paths, symlink targets,
  and pre-existing ``settings.json``, device record, or marker files.
- Tests allocate scratch directories under ``hyprial_test_tmp_root()`` rather
  than pytest's ``tmp_path`` so they work even when a remote runner sets
  ``TMPDIR`` inside a checkout under the caller's real HOME.
- The live-daemon contract test starts a real isolated ``DaemonProcess`` and
  ``running_http`` over a prepared home (without ``HYPRIAL_OWNER`` or
  ``HYPRIAL_NODE_ID`` overrides) and proves that ``onboarding.status`` projects
  ``authenticated=True``, ``deviceReady=True``, and ``initialized=True``, and
  that ``onboarding.firstRun.skip`` transitions ``firstRun`` to ``recorded``
  with ``skipped=True``. If product readiness rules change, this test fails.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from hyprial.identity import DeviceRecord, device_record_path, write_device_record
from hyprial.kernel import daemon_request_once, default_hyprial_home
from tests.integration.request_http.conftest import (
    DaemonProcess,
    Isolation,
    OWNER,
    running_http,
)
from tests.support.gui_home import ReadyHomeError, prepare_ready_home
from tests.support.gui_home.prepare import MARKER_FILENAME
from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import real_account_home


@pytest.fixture
def isolated_root() -> Path:
    with tempfile.TemporaryDirectory(
        prefix="gui-home-", dir=hyprial_test_tmp_root()
    ) as raw:
        yield Path(raw)


def _isolated_env_for(root: Path, home: Path, state: Path, user: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HYPRIAL_", "HARNESS_"))
    }
    env.update(
        {
            "HOME": str(user),
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_SERVICE_AUTOINSTALL": "0",
            "HYPRIAL_DAEMON_AUTOSTART": "0",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    return env


def test_rejects_when_network_isolated_is_unset_or_not_one(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = isolated_root / "isolated-home"
    monkeypatch.delenv("HYPRIAL_NETWORK_ISOLATED", raising=False)
    with pytest.raises(ReadyHomeError, match="HYPRIAL_NETWORK_ISOLATED"):
        prepare_ready_home(target, owner="demo-owner")
    assert not target.exists()

    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "0")
    with pytest.raises(ReadyHomeError, match="HYPRIAL_NETWORK_ISOLATED"):
        prepare_ready_home(target, owner="demo-owner")
    assert not target.exists()


def test_cli_requires_explicit_home_and_ignores_hyprial_home(isolated_root: Path):
    ambient = isolated_root / "ambient-home"
    ambient.mkdir(mode=0o700)
    env = dict(os.environ)
    env["HYPRIAL_NETWORK_ISOLATED"] = "1"
    env["HYPRIAL_HOME"] = str(ambient)
    completed = subprocess.run(
        [sys.executable, "-m", "tests.support.gui_home", "prepare", "--owner", "demo-owner", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode != 0
    assert "--home" in completed.stderr
    assert list(ambient.iterdir()) == []

    with pytest.raises(ReadyHomeError, match="explicit home"):
        prepare_ready_home("", owner="demo-owner")


def test_rejects_default_home_real_home_simulated_home_and_symlinks(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")

    for forbidden in (
        default_hyprial_home()[0],
        Path("~/.hyprial").expanduser(),
        real_account_home(),
        real_account_home() / "gui-home-guard-probe",
    ):
        with pytest.raises(ReadyHomeError):
            prepare_ready_home(forbidden, owner="demo-owner")

    simulated_home = isolated_root / "simulated-home"
    simulated_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(simulated_home))
    for under_simulated in (simulated_home, simulated_home / "sub-home"):
        with pytest.raises(ReadyHomeError, match="HOME"):
            prepare_ready_home(under_simulated, owner="demo-owner")

    real_dir = isolated_root / "real-target"
    real_dir.mkdir(mode=0o700)
    link_dir = isolated_root / "symlink-target"
    link_dir.symlink_to(real_dir)
    with pytest.raises(ReadyHomeError, match="symbolic link"):
        prepare_ready_home(link_dir, owner="demo-owner")


def test_rejects_existing_settings_device_or_nonempty_dir_and_allows_empty_subdirs(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")

    with_settings = isolated_root / "with-settings"
    with_settings.mkdir(mode=0o700)
    sentinel = '{"version": 1, "owner": "keep-me"}'
    (with_settings / "settings.json").write_text(sentinel, encoding="utf-8")
    with pytest.raises(ReadyHomeError, match="settings.json"):
        prepare_ready_home(with_settings, owner="demo-owner")
    assert (with_settings / "settings.json").read_text(encoding="utf-8") == sentinel

    with_device = isolated_root / "with-device"
    with_device.mkdir(mode=0o700)
    write_device_record(
        with_device,
        DeviceRecord(
            device_id="d.0123456789abcdef0123456789abcdef",
            owner="keep-me",
            server_public="nodekey:keep-server",
            client_public="nodekey:keep-client",
        ),
    )
    before_device = device_record_path(with_device).read_bytes()
    with pytest.raises(ReadyHomeError, match="device record"):
        prepare_ready_home(with_device, owner="demo-owner")
    assert device_record_path(with_device).read_bytes() == before_device

    with_file = isolated_root / "with-file"
    with_file.mkdir(mode=0o700)
    (with_file / "unrelated.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ReadyHomeError, match="existing files"):
        prepare_ready_home(with_file, owner="demo-owner")

    precreated = isolated_root / "precreated-empty-subdirs"
    (precreated / "state").mkdir(parents=True, mode=0o700)
    ready = prepare_ready_home(precreated, owner="demo-owner")
    assert (ready.home / MARKER_FILENAME).is_file()
    with pytest.raises(ReadyHomeError):
        prepare_ready_home(precreated, owner="demo-owner")


def test_cli_prepare_emits_json_and_refuses_second_run(isolated_root: Path):
    target = isolated_root / "cli-home"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HYPRIAL_", "HARNESS_"))
    }
    env["HYPRIAL_NETWORK_ISOLATED"] = "1"
    first = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.support.gui_home",
            "prepare",
            "--home",
            str(target),
            "--owner",
            "cli-demo-owner",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["ok"] is True
    assert payload["owner"] == "cli-demo-owner"
    assert payload["nodeId"].startswith("d.")
    assert Path(payload["markerPath"]).name == MARKER_FILENAME
    assert Path(payload["markerPath"]).is_file()
    assert all(Path(path).is_file() for path in payload["writtenFiles"])

    second = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.support.gui_home",
            "prepare",
            "--home",
            str(target),
            "--owner",
            "cli-demo-owner",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert second.returncode != 0
    assert second.stderr.strip()


def test_prepared_home_projects_ready_and_records_first_run_skip_on_real_daemon(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    home = isolated_root / "home"
    state = isolated_root / "state"
    user = isolated_root / "user"
    runtime = isolated_root / "runtime"
    user.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)

    ready = prepare_ready_home(home, owner=OWNER, state_dir=state)
    assert ready.marker_path.is_file()
    assert not (ready.operations_root / "first-run.json").exists()

    env = _isolated_env_for(isolated_root, home, state, user)
    isolation = Isolation(isolated_root, home, state, user, runtime, env)
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        ping = daemon_request_once("ping", {}, socket_path=isolation.socket_path, timeout=5)
        assert ping.get("nodeId") == ready.node_id

        with running_http(isolation) as http:
            status, _, before = http.request(
                "POST", "/daemon/request/onboarding.status", body={}
            )
            assert status == 200, before
            before_result = before["result"]
            assert before_result["ok"] is True
            assert before_result["phase"] == "serving"
            assert before_result["authenticated"] is True
            assert before_result["owner"] == OWNER
            assert before_result["deviceReady"] is True
            assert before_result["initialized"] is True
            assert before_result["nextActions"] == []
            assert before_result["firstRun"]["state"] == "none"
            assert before_result["firstRun"]["revision"] == 0
            assert before_result["firstRun"].get("skipped", False) is False

            status, _, skipped = http.request(
                "POST",
                "/daemon/request/onboarding.firstRun.skip",
                body={
                    "expectedRevision": before_result["firstRun"]["revision"],
                    "operationKey": "gui-home-first-run-skip-01",
                },
            )
            assert status == 200, skipped
            skip_result = skipped["result"]
            assert skip_result["ok"] is True
            assert skip_result["firstRun"]["state"] == "recorded"
            assert skip_result["firstRun"]["skipped"] is True
            assert skip_result["firstRun"]["revision"] == 1

            status, _, after = http.request(
                "POST", "/daemon/request/onboarding.status", body={}
            )
            assert status == 200, after
            after_result = after["result"]
            assert after_result["authenticated"] is True
            assert after_result["deviceReady"] is True
            assert after_result["initialized"] is True
            assert after_result["firstRun"]["state"] == "recorded"
            assert after_result["firstRun"]["skipped"] is True
            assert after_result["firstRun"]["revision"] == 1
    finally:
        daemon.stop()
