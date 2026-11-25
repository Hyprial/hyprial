"""The shipped pi carrier registers the TUI process birth fence."""

from __future__ import annotations

import json
import subprocess

import pytest

from hyprial.daemon.impl.mcp.channel.ownership import (
    _process_identities_match,
    _read_process_identity,
)

from ._pi_attach_support import Carrier, DAEMON_IPC, NODE, TYPEBOX

pytestmark = pytest.mark.skipif(
    NODE is None or TYPEBOX is None or not TYPEBOX.is_dir(),
    reason="node and a pi installation (bundled typebox) are required",
)


def test_pi_registration_uses_the_tui_process_and_daemon_birth_scheme(tmp_path) -> None:
    carrier = Carrier(tmp_path)
    try:
        assert carrier.command({"cmd": "start"})["ready"] is True
        (frame,) = carrier.wait_for_frames("session.register", 1)
        params = frame["params"]
        observed = _read_process_identity(carrier.process.pid)

        assert params["processPid"] == carrier.process.pid
        assert observed is not None
        assert _process_identities_match(params["processIdentity"], observed)
    finally:
        carrier.stop()


def test_pi_boundary_start_uses_the_daemon_kernel_component() -> None:
    assert NODE is not None
    started = "Thu Oct  8 00:00:00 2026"
    script = f"""
import {{ processBirthIdentity }} from {json.dumps(DAEMON_IPC.as_uri())};
Date.now = () => 1000999;
process.uptime = () => 1;
const identity = processBirthIdentity(123, {{
  platform: "darwin",
  readPs: () => {json.dumps(started)},
}});
process.stdout.write(identity ?? "");
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    claimed = completed.stdout
    observed = (
        "darwin-starttime:1000:999000;darwin-startsec:1000;"
        f"ps-lstart:{started}"
    )
    assert claimed == f"ps-lstart:{started}"
    assert _process_identities_match(claimed, observed)
    assert _process_identities_match("darwin-startsec:999", observed)


def test_pi_win32_birth_identity_matches_daemon_filetime_grammar() -> None:
    from hyprial.daemon.impl.mcp.channel.ownership import (
        _OwnerProcessStatus,
        _owner_process_status,
    )

    assert NODE is not None
    stamp = "134082216001234567"
    script = f"""
import {{ processBirthIdentity }} from {json.dumps(DAEMON_IPC.as_uri())};
const valid = processBirthIdentity(123, {{
  platform: "win32",
  readWinFiletime: () => {json.dumps(stamp)},
  readPs: () => {{ throw new Error("win32 must not invoke ps"); }},
}});
const missing = processBirthIdentity(123, {{
  platform: "win32",
  readWinFiletime: () => null,
  readPs: () => "Thu Oct  8 00:00:00 2026",
}});
process.stdout.write(JSON.stringify({{ valid, missing }}));
"""
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(completed.stdout)
    claimed = payload["valid"]
    assert claimed == f"win-filetime:{stamp}"
    assert payload["missing"] is None
    assert _process_identities_match(claimed, f"win-filetime:{stamp}")
    assert not _process_identities_match(claimed, "win-filetime:134082216009999999")
    assert (
        _owner_process_status(
            123,
            claimed,
            signal_process=lambda _pid, _sig: None,
            read_identity=lambda _pid: "win-filetime:134082216009999999",
        )
        is _OwnerProcessStatus.IDENTITY_MISMATCH
    )
