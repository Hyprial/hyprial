from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SCENARIOS = Path(__file__).resolve().parents[3] / 'contract/e2e'
sys.path.insert(0, str(SCENARIOS))

from _presence_trace import PresenceTrace  # noqa: E402


def _runner(name: str):
    spec = importlib.util.spec_from_file_location(name, SCENARIOS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ring_is_bounded_and_retains_only_whitelisted_fields() -> None:
    trace = PresenceTrace(capacity=2)
    trace.record_cli(
        "node-a", "hosts",
        {"hosts": [{"nodeId": "node-b", "status": "online", "sessionRef": "SECRET"}]},
    )
    trace.record_cli(
        "node-a", "ps",
        {
            "zenoh": {
                "listen": ["tcp/node-a:7447"],
                "connect": ["tcp/user:token@node-b:7448", "tcp/" + "x" * 1000 + ":7448"],
                "sessionRef": "SECRET",
            },
            "daemon": {"running": True, "phase": "serving", "credential": "SECRET"},
            "payload": "SECRET",
        },
    )
    document = trace.document()
    assert document["droppedRows"] == 1
    assert len(document["rows"]) == 2
    text = json.dumps(document)
    assert "SECRET" not in text and "token" not in text
    assert document["rows"][0]["connect"] == ["<redacted>", "<redacted>"]
    assert document["rows"][1]["event"] == "ping"


def test_e2e001_diagnostic_failures_preserve_cli_result(tmp_path, monkeypatch) -> None:
    runner = _runner("run-two-machine-roundtrip.py")
    trace = PresenceTrace(capacity=8)
    monkeypatch.setattr(runner, "PRESENCE_TRACE", trace)
    monkeypatch.setattr(
        runner, "sh",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, '{"ok":true,"hosts":[{"nodeId":"node-a","status":"online"}]}', ""
        ),
    )
    original = trace.record_cli

    def failed_record(*_args, **_kwargs):
        raise RuntimeError("diagnostic sink failed")

    monkeypatch.setattr(trace, "record_cli", failed_record)
    result = runner.exec_json("node-c", "hosts")
    assert result["hosts"][0]["nodeId"] == "node-a"
    monkeypatch.setattr(trace, "record_cli", original)
    monkeypatch.setattr(runner, "docker", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("inspect failed")))
    runner._capture_docker_state("failure")
    [snapshot] = [row for row in trace.document()["rows"] if row["event"] == "docker-state"]
    assert set(snapshot["states"]) == {runner.NODES[0], runner.NODES[1], runner.NODE_C}
    assert {value["errorType"] for value in snapshot["states"].values()} == {"OSError"}

    monkeypatch.setattr(runner, "ALL_NODES", ())
    monkeypatch.setattr(runner, "artifacts_root", lambda: tmp_path)
    monkeypatch.setenv("HYPRIAL_E2E_ROOT", str(tmp_path))
    monkeypatch.setattr(trace, "write", lambda _path: (_ for _ in ()).throw(OSError("write failed")))
    evidence = runner.capture_failure()
    assert evidence.is_dir()


def test_e2e021_diagnostic_failures_preserve_existing_result(tmp_path, monkeypatch) -> None:
    runner = _runner("run-orgfs-forwarding-redial.py")
    trace = PresenceTrace(capacity=8)
    monkeypatch.setattr(runner, "PRESENCE_TRACE", trace)
    daemon = SimpleNamespace(
        node_id="node-a",
        command=lambda *_args, **_kwargs: {"hosts": [{"nodeId": "node-b"}]},
    )
    monkeypatch.setattr(trace, "record_cli", lambda *_args: (_ for _ in ()).throw(RuntimeError("capture failed")))
    assert runner._observed(daemon, "hosts", "--json") == {"hosts": [{"nodeId": "node-b"}]}
    monkeypatch.setattr(trace, "write", lambda _path: (_ for _ in ()).throw(OSError("write failed")))
    runner._flush_presence(tmp_path)


def test_e2e001_original_failure_survives_inspect_and_trace_loss(
    tmp_path, monkeypatch, capsys
) -> None:
    runner = _runner("run-two-machine-roundtrip.py")
    monkeypatch.setattr(sys, "argv", ["runner"])
    monkeypatch.setattr(runner, "_install_signal_handlers", dict)
    monkeypatch.setattr(runner, "run_docker", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("original verdict")))
    monkeypatch.setattr(runner, "docker", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("inspect unavailable")))
    monkeypatch.setattr(runner, "ALL_NODES", ())
    monkeypatch.setattr(runner, "artifacts_root", lambda: tmp_path)
    monkeypatch.setenv("HYPRIAL_E2E_ROOT", str(tmp_path))
    monkeypatch.setattr(runner.PRESENCE_TRACE, "write", lambda _path: (_ for _ in ()).throw(OSError("trace unavailable")))
    monkeypatch.setattr(runner, "remove_resources", list)
    monkeypatch.setattr(runner, "self_check", list)

    assert runner.main() == 1
    output = capsys.readouterr().out
    assert "RESULT FAIL E2E-001 original verdict" in output
    assert "presence trace lost (OSError)" in output
    assert "cleanup:" not in output
