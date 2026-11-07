from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


RUNNER = Path(__file__).resolve().parents[4] / 'contract/e2e/run-host-invite.py'


def _load_runner():
    spec = importlib.util.spec_from_file_location("host_invite_readiness", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stop_exact_child(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_readiness_timeout_captures_blocked_import_stacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner.os, "nice", lambda _increment: None)
    blocked_source = tmp_path / "blocked-source"
    package = blocked_source / "src" / "hyprial"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import time\n"
        "Path(os.environ['AT01_TEST_IMPORT_MARKER']).write_text('blocked')\n"
        "def blocked_import():\n"
        "    time.sleep(60)\n"
        "blocked_import()\n"
    )

    scenario_root = tmp_path / "scenario-evidence"
    scenario_root.mkdir()
    scenario = runner.Scenario(scenario_root, tmp_path / "production.sock")
    marker = scenario_root / "blocked-import.marker"
    scenario.env.update(
        PYTHONPATH=str(blocked_source / "src"),
        AT01_SOURCE=str(blocked_source),
        AT01_TEST_IMPORT_MARKER=str(marker),
        AT01_READINESS_TIMEOUT_SECONDS="0.5",
    )

    try:
        with pytest.raises(AssertionError) as caught:
            scenario.start()

        message = str(caught.value)
        assert message.startswith("isolated daemon readiness timeout")
        assert "imports=missing" in message
        assert "daemon.jsonl lines=0" in message
        assert "elapsed=" in message

        dump = scenario_root / "daemon-1-sigusr2.txt"
        assert marker.exists(), "the child never reached the deliberately blocked import"
        assert dump.exists(), "the timeout did not create a SIGUSR2 stack dump"
        text = dump.read_text()
        assert "cli.py" in text and "in blocked_import" in text

        sample = scenario_root / "daemon-1-sample.txt"
        if sys.platform == "darwin":
            assert sample.exists() and sample.stat().st_size > 0
        else:
            assert "sample=skipped" in message
    finally:
        _stop_exact_child(scenario.process)
