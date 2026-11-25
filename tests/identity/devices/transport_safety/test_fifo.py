from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from hyprial.identity import write_device_record
from tests.identity.devices.test_transport_credential import _device_record, _stage
from tests.identity.devices.transport_safety.inventory import inventory


@pytest.mark.parametrize(
    "target", ["device", "device-identity", "enrollment", "staged-key", "committed-key"]
)
def test_stage_rejects_fifo_promptly_without_mutation(
    tmp_path: Path, target: str
) -> None:
    home = tmp_path / "home"
    operation_id, device_uid = str(uuid4()), uuid4().hex
    if target.endswith("-key"):
        staged = _stage(home)
        operation_id, device_uid = staged.operation_id, staged.device_uid
        path = staged._key_path
        if target == "committed-key":
            path = home / "secrets/transport/device-key.pem"
            staged._key_path.replace(path)
    else:
        write_device_record(home, _device_record(device_uid))
        path = home / f"state/tailcat/{target}.json"
        if target == "enrollment":
            path = home / "state/transport/enrollment.json"
            path.parent.mkdir(mode=0o700)
    path.unlink(missing_ok=True)
    os.mkfifo(path, 0o600)
    before = inventory(tmp_path)
    child = Path(__file__).with_name("fifo_child.py")
    timed_out = False
    with subprocess.Popen(
        [sys.executable, str(child), str(home), operation_id, device_uid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    ) as process:
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 10)
            assert readable, "owned child did not reach stage within startup bound"
            assert process.stdout.readline() == b"ENTERING_STAGE\n"
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        assert process.poll() is not None, "owned child was not reaped"

    assert inventory(tmp_path) == before, "FIFO rejection changed filesystem inventory"
    assert not timed_out, "stage blocked on FIFO; owned child was killed/waited/reaped"
    assert process.returncode == 0
    assert stdout == b"TRANSPORT_STORAGE_UNSAFE\n"
    assert stderr == b""
