"""Bundled Claude Code CLI minimum-version check in the Agent SDK worker.

The pinned claude-agent-sdk bundles a Claude Code CLI whose version is exposed
as ``claude_agent_sdk._cli_version.__cli_version__``. The worker refuses to
start when that CLI is older than the minimum this release needs. An unreadable
version (module path moved, unparsable string) only warns on stderr: the other
worker tests run against SDK doubles that do not ship the module, and failing
closed there would force every double to grow one.
Reduced subprocess environments preserve an ambient SYSTEMROOT when present;
all other isolation boundaries are unchanged.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk_module

WORKER = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")
_UNREADABLE_WARNING = "cannot read the bundled Claude Code version"

_FAKE_SDK = '''
class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class ResultMessage:
    pass


class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options
'''


def _probe(
    tmp_path: Path, cli_version: str | None, **overrides: str
) -> subprocess.CompletedProcess[str]:
    package = tmp_path / "claude_agent_sdk"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(_FAKE_SDK, encoding="utf-8")
    if cli_version is not None:
        (package / "_cli_version.py").write_text(
            f'__cli_version__ = "{cli_version}"\n', encoding="utf-8"
        )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(tmp_path)}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    env.update(overrides)
    return subprocess.run(
        [sys.executable, str(WORKER), "--probe"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _first_frame(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stderr
    return json.loads(lines[0])


@pytest.mark.parametrize("version", ["2.1.312", "2.1.318-rc1", "2.2.0", "3.0.0"])
def test_supported_bundled_cli_is_accepted(tmp_path: Path, version: str) -> None:
    result = _probe(tmp_path, version)

    assert result.returncode == 0, result.stderr
    assert _first_frame(result)["type"] == "probe"
    assert _UNREADABLE_WARNING not in result.stderr


@pytest.mark.parametrize("version", ["2.1.311", "2.0.999", "1.9.9"])
def test_old_bundled_cli_fails_closed(tmp_path: Path, version: str) -> None:
    result = _probe(tmp_path, version)

    assert result.returncode == 1
    assert version in result.stderr
    assert "2.1.312" in result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("version", [None, "dev"])
def test_unreadable_version_warns_and_proceeds(
    tmp_path: Path, version: str | None
) -> None:
    result = _probe(tmp_path, version)

    assert result.returncode == 0, result.stderr
    assert _first_frame(result)["type"] == "probe"
    assert _UNREADABLE_WARNING in result.stderr


def test_external_cli_skips_the_bundled_version_check(tmp_path: Path) -> None:
    result = _probe(tmp_path, "2.1.1", HYPRIAL_CLAUDE_EXECUTABLE=sys.executable)

    assert result.returncode == 0, result.stderr
    assert _first_frame(result)["type"] == "probe"
    assert "check skipped: HYPRIAL_CLAUDE_EXECUTABLE is set" in result.stderr
    assert "bundles Claude Code" not in result.stderr
