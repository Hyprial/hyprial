"""The public debug launcher rejects invalid arguments before external work."""

from __future__ import annotations

from pathlib import Path
import subprocess


LAUNCHER = Path(__file__).resolve().parents[3] / "scripts/build/docker/run-gui-debug.sh"


def test_unknown_mode_is_rejected_before_external_tools(tmp_path: Path) -> None:
    result = subprocess.run(
        ("bash", str(LAUNCHER), "unknown", "4382"),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "MODE must be normal or dev" in result.stderr


def test_invalid_port_is_rejected_before_external_tools(tmp_path: Path) -> None:
    result = subprocess.run(
        ("bash", str(LAUNCHER), "normal", "65536"),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "port must be 1-65535" in result.stderr
