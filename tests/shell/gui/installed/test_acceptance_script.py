"""Interpreter and wheel-source guards of the installed GUI acceptance script."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from scripts.verify import verify_installed_gui as acceptance


def _probe(monkeypatch, tmp_path: Path, stdout: str) -> tuple[bool, str]:
    executable = tmp_path / "python3.12"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(acceptance, "command", lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout, ""))
    return acceptance.probe_interpreter(str(executable))


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "python": str(tmp_path / "missing-python3.12"), "wheel": None, "port": None, "build_timeout": 1.0,
        "install_timeout": 1.0, "service_timeout": 1.0, "browser_timeout": 1.0, "keep": False, "evidence": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_interpreter_probe_rejects_malformed_probe_without_raising(tmp_path, monkeypatch) -> None:
    valid, detail = _probe(monkeypatch, tmp_path, "not-json")
    assert valid is False
    assert "not JSON" in detail


def test_interpreter_probe_requires_cpython_312(tmp_path, monkeypatch) -> None:
    valid, detail = _probe(monkeypatch, tmp_path, json.dumps({"v": [3, 11, 9], "i": "cpython", "g": None}))
    assert valid is False
    assert "CPython 3.12" in detail


def test_interpreter_probe_refuses_a_free_threaded_build(tmp_path, monkeypatch) -> None:
    valid, detail = _probe(monkeypatch, tmp_path, json.dumps({"v": [3, 12, 4], "i": "cpython", "g": 1}))
    assert valid is False
    assert "free-threaded" in detail


def test_interpreter_probe_accepts_gil_cpython_312(tmp_path, monkeypatch) -> None:
    assert _probe(monkeypatch, tmp_path, json.dumps({"v": [3, 12, 4], "i": "cpython", "g": 0})) == (True, "CPython 3.12.4 (GIL)")


def test_run_reports_blocked_when_interpreter_is_missing(tmp_path) -> None:
    code, line, evidence = acceptance.run(_args(tmp_path))
    assert code == acceptance.BLOCKED
    assert line.startswith("BLOCKED python ")
    assert evidence["verdict"] == line


def test_a_given_wheel_skips_the_build_and_a_missing_one_is_blocked(tmp_path, monkeypatch) -> None:
    def no_build(*_a, **_k):
        raise AssertionError("a given wheel must not trigger a command")

    monkeypatch.setattr(acceptance, "command", no_build)
    wheel = tmp_path / "hyprial-0-cp312-abi3-linux_x86_64.whl"
    wheel.write_bytes(b"")
    steps: list[acceptance.Step] = []
    assert acceptance.obtain_wheel(_args(tmp_path, wheel=wheel), tmp_path, steps, {}) == [wheel.resolve()]
    assert [(step.name, step.status) for step in steps] == [("given-wheel", "PASS")]
    steps = []
    assert acceptance.obtain_wheel(_args(tmp_path, wheel=tmp_path / "absent.whl"), tmp_path, steps, {}) == []
    assert [(step.name, step.status) for step in steps] == [("given-wheel", "BLOCKED")]
