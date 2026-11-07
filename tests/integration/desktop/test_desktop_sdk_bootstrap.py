import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.agent_sdk import sdk_worker_command


def test_bundled_sdk_needs_no_uv(monkeypatch, tmp_path):
    entry = tmp_path / "path with spaces" / "worker.py"
    entry.parent.mkdir()
    entry.write_text("pass\n")
    monkeypatch.setenv("HYPRIAL_BUNDLED_SDK_BOOTSTRAP", str(entry))
    monkeypatch.setenv("PATH", "")
    assert sdk_worker_command() == (sys.executable, "-I", "-S", "-B", str(entry))


@pytest.mark.parametrize("entry", ["relative.py", ""])
def test_bundled_sdk_invalid_entry_does_not_fall_back_to_download(monkeypatch, entry):
    monkeypatch.setenv("HYPRIAL_BUNDLED_SDK_BOOTSTRAP", entry)
    with pytest.raises(RuntimeError, match="existing absolute"):
        sdk_worker_command()


def test_missing_bundled_sdk_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRIAL_BUNDLED_SDK_BOOTSTRAP", str(Path(tmp_path) / "missing.py"))
    with pytest.raises(RuntimeError, match="existing absolute"):
        sdk_worker_command()
