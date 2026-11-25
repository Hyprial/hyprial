import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

MESSAGE_ID = "11111111-2222-4333-8444-555555555555"


def load_evidence():
    root = Path(__file__).resolve().parents[4] / "contract/e2e"
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("resident_evidence_under_test", root / "_resident_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log_file(root):
    path = root / "state/logs/provider.jsonl"
    path.parent.mkdir(parents=True)
    return path


def test_retains_tool_path_ids_without_raw_payloads_or_secret_files(tmp_path):
    module = load_evidence()
    root = tmp_path / "run"
    log = log_file(root)
    log.write_text(json.dumps({"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "name": "harness_reply", "arguments": {"messageId": MESSAGE_ID, "message": "SECRET-TEXT", "sessionRef": "SECRET-FENCE", "name": "SECRET-ARG-NAME", "code": "SECRET-ARG-CODE", "status": "SECRET-ARG-STATUS"}},
        {"type": "text", "text": "SECRET-OUTPUT"},
    ]}, "error": {"code": "invalid_api_key sk-private", "status": "401 Bearer sk-private"}, "result": {"status": "Bearer sk-private"}, "environment": {"DEEPSEEK_API_KEY": "SECRET-KEY"}}) + "\n")
    secret = root / "home/agents/pi/secrets/auth.jsonl"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"event":"SECRET-AUTH"}\n')
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"event":"SECRET-OUTSIDE"}\n')
    (log.parent / "linked.jsonl").symlink_to(outside)
    destination = tmp_path / "retained.json"
    document = module.retain_protocol_trace(root, destination)
    text = destination.read_text()
    assert "harness_reply" in text and MESSAGE_ID in text
    assert "SECRET-" not in text and "sk-private" not in text and "Bearer" not in text
    assert len(document["records"]) == 1
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", ["sk-abcdef012345", "Bearer sk-secret", "f" * 64, "Ab0+/=" * 20, "free text from a private message"])
def test_free_text_and_token_shapes_are_not_protocol_metadata(value):
    module = load_evidence()
    result = module.protocol_metadata({"event": value, "status": value, "name": value, "messageId": value})
    assert not result


def test_total_byte_budget_and_list_cap(monkeypatch, tmp_path):
    module = load_evidence()
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 1024)
    root = tmp_path / "run"
    log_file(root).write_text('{"event":"worker.started"}\n' * 1000)
    destination = tmp_path / "bounded.json"
    document = module.retain_protocol_trace(root, destination)
    assert destination.stat().st_size <= 1024
    assert document["truncated"] is True and 0 < len(document["records"]) < 1000
    many = module.protocol_metadata({"content": [{"type": "toolCall", "name": "harness_reply"}] * 1000})
    assert len(many["content"]) == module.MAX_LIST_ITEMS


def test_oversize_and_deep_lines_do_not_hide_later_records(tmp_path):
    module = load_evidence()
    root = tmp_path / "run"
    log_file(root).write_text('x' * 900_000 + '\n' + '[' * 5000 + ']' * 5000 + '\n{"event":"worker.started"}\n')
    destination = tmp_path / "after-invalid.json"
    document = module.retain_protocol_trace(root, destination)
    assert document["truncated"] is True
    assert len(document["records"]) == 1
    assert destination.stat().st_size < 1024


def test_failure_captures_before_temporary_root_cleanup(monkeypatch, tmp_path):
    module = load_evidence()
    monkeypatch.setattr(module, "artifacts_root", lambda: tmp_path / "artifacts")
    with module.resident_root(lambda: True) as root:
        log_file(root).write_text('{"event":"worker.started"}\n')
    assert not root.exists()
    assert len(list((tmp_path / "artifacts").glob("e2e006-protocol-*.json"))) == 1


def test_pass_path_writes_no_artifact(monkeypatch, tmp_path):
    module = load_evidence()
    monkeypatch.setattr(module, "artifacts_root", lambda: tmp_path / "artifacts")
    with module.resident_root(lambda: False) as root:
        log_file(root).write_text('{"event":"worker.started"}\n')
    assert not root.exists() and not (tmp_path / "artifacts").exists()


def test_retention_error_never_masks_original(monkeypatch, tmp_path, capsys):
    module = load_evidence()
    monkeypatch.setattr(module, "artifacts_root", lambda: tmp_path / "artifacts")
    def broken(*args):
        raise RecursionError("SECRET-retention-error")
    monkeypatch.setattr(module, "retain_protocol_trace", broken)
    with pytest.raises(ValueError, match="original failure"):
        with module.resident_root(lambda: False):
            raise ValueError("original failure")
    assert "RecursionError" in capsys.readouterr().out


def test_artifact_is_created_private(monkeypatch, tmp_path):
    module = load_evidence()
    root = tmp_path / "run"
    log_file(root).write_text('{"event":"worker.started"}\n')
    real_open = module.os.open
    modes = []
    def opened(path, flags, mode):
        modes.append(mode)
        return real_open(path, flags, mode)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "open", opened)
        module.retain_protocol_trace(root, tmp_path / "private.json")
    assert modes == [0o600]


def test_sigterm_preserves_metadata_and_removes_owned_root(tmp_path):
    root = Path(__file__).resolve().parents[4]
    marker = tmp_path / "root-path"
    script = f'''
import os,signal,sys
from pathlib import Path
sys.path.insert(0,{str(root / "contract/e2e")!r})
from _resident_evidence import resident_root
with resident_root(lambda: False) as root:
    Path({str(marker)!r}).write_text(str(root))
    log=root/'state/logs/provider.jsonl'
    log.parent.mkdir(parents=True)
    log.write_text('{{"event":"worker.started"}}\\n')
    os.kill(os.getpid(),signal.SIGTERM)
'''
    result = subprocess.run([sys.executable, "-c", script], env={**os.environ, "HYPRIAL_E2E_ROOT": str(tmp_path / "evidence")}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 143, result.stderr
    assert not Path(marker.read_text()).exists()
    assert list((tmp_path / "evidence/artifacts").glob("e2e006-protocol-*.json"))
