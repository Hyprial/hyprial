"""Execute the Dockerfile's actual uv RUN bodies with /bin/sh stubs.

No Docker build, uv download, /opt chmod or /tmp/hyprial-src removal occurs.
The set-but-empty environment and error text come from card eef7d275
(uv 0.8.15); the host uv version is deliberately irrelevant to this test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


DOCKERFILE = Path(__file__).resolve().parents[3] / 'scripts/build/docker/Dockerfile.hyprial-worker'
KEYS = ("UV_PYTHON_INSTALL_MIRROR", "UV_INDEX_URL")


def _uv_run_blocks() -> list[str]:
    # Dockerfile shell-form continuations, not a second copy of the RUN code.
    lines = DOCKERFILE.read_text().replace("\\\n", "").splitlines()
    blocks = [line.removeprefix("RUN ") for line in lines if line.startswith("RUN ") and "uv venv" in line]
    assert len(blocks) == 2  # hyprial venv and persistent claude SDK venv
    return blocks


def _run_block(tmp_path: Path, index: int, values: dict[str, str], failure: str = ""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    stub = '''#!/bin/sh
set -eu
name="${0##*/}"
printf '%s %s\\n' "$name" "$*" >> "$STUB_LOG"
case "$name" in
  uv)
    # Shell presence tests distinguish UNSET from an exported empty value.
    test "${UV_PYTHON_INSTALL_MIRROR+x}" = "$EXPECT_MIRROR_SET" || exit 81
    test "${UV_INDEX_URL+x}" = "$EXPECT_INDEX_SET" || exit 82
    test "${UV_PYTHON_INSTALL_MIRROR-}" = "$EXPECT_MIRROR_VALUE" || exit 83
    test "${UV_INDEX_URL-}" = "$EXPECT_INDEX_VALUE" || exit 84
    if [ "$1" = "$STUB_FAILURE" ]; then
      printf '%s\\n' 'error: Invalid download URL: relative URL without a base' >&2
      exit 23
    fi
    ;;
  rm) test "$*" = '-rf /tmp/hyprial-src' ;;
  chmod) test "$*" = '-R a+rX /opt/hyprial' ;;
  *) exit 85 ;;
esac
'''
    for name in ("uv", "rm", "chmod"):
        tool = bin_dir / name
        tool.write_text(stub)
        tool.chmod(0o755)
    # No inherited UV/proxy/user settings and no system commands on PATH.
    result = subprocess.run(
        ["/bin/sh", "-c", _uv_run_blocks()[index]],
        env={
            "PATH": str(bin_dir), "HOME": str(tmp_path),
            "STUB_LOG": str(log), "STUB_FAILURE": failure,
            "AGENT_SDK_VERSION": "0.2.125",
            "EXPECT_MIRROR_SET": "x" if values.get(KEYS[0]) else "",
            "EXPECT_INDEX_SET": "x" if values.get(KEYS[1]) else "",
            "EXPECT_MIRROR_VALUE": values.get(KEYS[0], ""),
            "EXPECT_INDEX_VALUE": values.get(KEYS[1], ""),
            **values,
        },
        capture_output=True, text=True, timeout=10, check=False,
    )
    return result, log.read_text().splitlines()


@pytest.mark.parametrize("block", [0, 1])
@pytest.mark.parametrize("values", [
    {},
    {KEYS[0]: "", KEYS[1]: ""},
    {KEYS[0]: "https://mirror.example/python", KEYS[1]: ""},
    {KEYS[0]: "", KEYS[1]: "https://index.example/simple"},
    {KEYS[0]: "https://mirror.example/python", KEYS[1]: "https://index.example/simple"},
])
def test_uv_runs_never_receive_empty_url_environment(tmp_path: Path, block: int, values: dict[str, str]) -> None:
    result, commands = _run_block(tmp_path, block, values)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(commands) == 3
    assert commands[0].startswith("uv venv --python 3.12 ")
    assert commands[1].startswith("uv pip install --python ")
    assert commands[2].startswith("rm " if block == 0 else "chmod ")


@pytest.mark.parametrize("block", [0, 1])
@pytest.mark.parametrize("failure", ["venv", "pip"])
def test_uv_failure_names_both_url_variables_without_echoing_values(
    tmp_path: Path, block: int, failure: str
) -> None:
    values = {KEYS[0]: "https://mirror.example/python", KEYS[1]: "https://index.example/simple"}
    result, commands = _run_block(tmp_path, block, values, failure)
    assert result.returncode == 23  # retain uv's failure status
    assert "Invalid download URL: relative URL without a base" in result.stderr
    for key in KEYS:
        assert key in result.stderr
        assert values[key] not in result.stderr
    assert all(command.startswith("uv ") for command in commands)  # no cleanup after failure


def test_uv_build_args_have_no_empty_defaults() -> None:
    lines = DOCKERFILE.read_text().splitlines()
    for key in KEYS:
        assert f"ARG {key}" in lines
        assert f"ARG {key}=" not in lines
