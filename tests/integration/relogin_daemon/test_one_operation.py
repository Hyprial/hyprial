"""Serving relogin is ONE daemon operation on ``hyprial daemon run``."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from hyprial.kernel import ipc_errors

from .conftest import pytestmark  # noqa: F401
from .entry import ISSUER, OWNER, UID

ENTRY = Path(__file__).with_name("entry.py")
WAIT_SECONDS = 60
CLIENT_SECONDS = 75


def _seed_serving_home(lifecycle) -> None:
    (lifecycle.home / "settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "owner": OWNER,
                "identityMode": "casdoor",
                "identityIssuer": ISSUER,
                "userId": UID,
                "username": OWNER,
            }
        ),
        encoding="utf-8",
    )


def _spawn(lifecycle, **extra_env: str) -> subprocess.Popen:
    env = {**lifecycle.env, **extra_env}
    log = (lifecycle.root / f"child{len(lifecycle.children)}.log").open("w")
    child = subprocess.Popen(
        [sys.executable, str(ENTRY)],
        env=env,
        stdout=log,
        stderr=log,
        cwd=str(lifecycle.root),
        start_new_session=True,
    )
    log.close()
    lifecycle.children.append(child)
    lifecycle.ledger.record(child.pid)
    return child


def _stop_child(lifecycle, child: subprocess.Popen) -> None:
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0
    lifecycle.assert_nothing_left(child.pid)


def test_relogin_until_serving_completes_without_client_poll(lifecycle) -> None:
    _seed_serving_home(lifecycle)
    child = _spawn(lifecycle)
    lifecycle.serving(child)

    begun = lifecycle.call(
        "auth.relogin",
        {"operationKey": "l2-relogin-key-0001", "until": "serving"},
    )
    assert begun["completion"]["stage"] == "authorizing"
    assert begun["verification"]["userCode"] == "RL-Z2"

    done = lifecycle.call(
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": WAIT_SECONDS},
        timeout=CLIENT_SECONDS,
    )
    op = done["operation"]
    assert op["state"] == "authenticated"
    assert op["owner"] == OWNER
    assert op["completion"]["stage"] == "serving"
    assert op["completion"]["device"]["ready"] is True
    assert op["completion"]["device"]["deviceId"] == "z2-relogin-device"
    _stop_child(lifecycle, child)


def test_relogin_device_failure_keeps_identity_and_reports_not_ready(lifecycle) -> None:
    _seed_serving_home(lifecycle)
    child = _spawn(lifecycle, L2_DEVICE="fail")
    lifecycle.serving(child)

    begun = lifecycle.call(
        "auth.relogin",
        {"operationKey": "l2-relogin-key-0002", "until": "serving"},
    )
    done = lifecycle.call(
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": WAIT_SECONDS},
        timeout=CLIENT_SECONDS,
    )
    op = done["operation"]
    assert op["state"] == "authenticated"
    assert op["owner"] == OWNER
    assert op["completion"]["stage"] == "serving"
    assert op["completion"]["device"]["ready"] is False
    assert op["completion"]["device"]["error"]["code"] == "DEVICE_ENSURE_FAILED"
    assert op["completion"]["device"]["retry"] == "serving-start"
    _stop_child(lifecycle, child)


def test_relogin_different_uid_is_rejected_with_identity_conflict(lifecycle) -> None:
    _seed_serving_home(lifecycle)
    child = _spawn(lifecycle, L2_COMMIT_UID="uid-other-999")
    lifecycle.serving(child)

    begun = lifecycle.call(
        "auth.relogin",
        {"operationKey": "l2-relogin-key-0003", "until": "serving"},
    )
    done = lifecycle.call(
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": WAIT_SECONDS},
        timeout=CLIENT_SECONDS,
    )
    op = done["operation"]
    assert op["state"] == "unknown"
    assert op["completion"]["stage"] == "stopped"
    assert op["completion"]["failure"]["code"] == ipc_errors.IDENTITY_CONFLICT
    _stop_child(lifecycle, child)


def test_relogin_cancel_before_authorization_stops_cleanly(lifecycle) -> None:
    _seed_serving_home(lifecycle)
    child = _spawn(lifecycle, L2_HOLD="1")
    lifecycle.serving(child)

    begun = lifecycle.call(
        "auth.relogin",
        {"operationKey": "l2-relogin-key-0004", "until": "serving"},
    )
    cancelled = lifecycle.call(
        "auth.operation.cancel",
        {"operationId": begun["operationId"]},
    )
    assert cancelled["cancelled"] is True
    deadline = time.monotonic() + 10.0
    got = None
    while time.monotonic() < deadline:
        got = lifecycle.call("auth.operation.get", {"operationId": begun["operationId"]})
        if got["operation"]["completion"]["stage"] == "stopped":
            break
        time.sleep(0.05)
    assert got is not None
    assert got["operation"]["state"] == "cancelled"
    assert got["operation"]["completion"]["stage"] == "stopped"
    _stop_child(lifecycle, child)
