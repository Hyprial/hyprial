"""Context exit owns every server resource, including incomplete handshakes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPOSITORY = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)


def test_context_exit_stops_an_accepted_silent_peer_worker(tmp_path: Path) -> None:
    """Keep the peer open through exit; the child cleans even a failing witness."""

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.identity.devices.client.lifetime.probe_context_exit",
        ],
        cwd=REPOSITORY,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    (tmp_path / "context-exit.json").write_text(completed.stdout, encoding="utf-8")
    assert result["default_timeout"] is None
    assert result["accepted"] == 1
    assert all(result["cleanup"].values()), result
    if os.name == "posix":
        assert result["fd_oracle"] == "AVAILABLE"
        assert result["fds_restored"] is True
    else:
        assert result["fd_oracle"] == "NOT_AVAILABLE"
        assert result["fd_unavailable_reason"] == "unsupported platform"
        assert result["baseline_fds"] is None
        assert result["fds_restored"] is None
    assert result["requests"] == 0
    assert result["worker_alive_after_exit"] is False, (
        "LoopbackAuthority context returned with its worker still alive",
        result,
    )
    assert result["listener_closed_after_exit"] is True
    assert result["temporary_removed_after_exit"] is True
    if os.name == "posix":
        assert result["server_fds_after_exit"] == []
    else:
        assert result["server_fds_after_exit"] is None
