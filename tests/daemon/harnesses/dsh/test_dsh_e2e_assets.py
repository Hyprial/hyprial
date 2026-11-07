from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PROMPT = ROOT / "contract" / "e2e" / "dsh-worker-test-prompt.md"
RUNNER = ROOT / "contract" / "e2e" / "run-dsh-worker.py"


def test_dsh_attended_prompt_is_machine_neutral_and_points_to_real_gate() -> None:
    text = PROMPT.read_text(encoding="utf-8")

    assert "git rev-parse --show-toplevel" in text
    old_env_prefix = "H2" + "B_"
    assert old_env_prefix not in text
    # The endpoint override is gone: the worker spawns its own DSH.
    endpoint_name = "HYPRIAL_DSH_" + "ENDPOINT"
    assert endpoint_name not in text
    assert "command -v dsh" in text
    assert "HYPRIAL_DSH_AGENT_PRESET" in text
    assert "DSH_HOME" in text
    assert "HYPRIAL_DSH_REPLY_TIMEOUT" in text
    assert "harness_whoami" in text
    assert "harness_send" in text
    assert "run-dsh-worker.py" in text
    assert "/home/" not in text
    assert "agent:chloemartin" not in text
    assert "session-" not in text.lower()


def test_dsh_e2e_runner_has_no_external_endpoint_override() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    endpoint_name = "HYPRIAL_DSH_" + "ENDPOINT"
    assert endpoint_name not in text
    assert "DEFAULT_DSH_" + "ENDPOINT" not in text
    # The real endpoint and private home are OUTPUTS of the spawned worker.
    assert "dshHome" in text
    # dsh on PATH is a NOT-RUN precondition, not a silent skip.
    assert "command -v" not in text  # done in the workflow step, not Python
    assert 'shutil.which("dsh")' in text


def test_dsh_e2e_runner_is_executable() -> None:
    assert RUNNER.is_file()
    assert RUNNER.stat().st_mode & 0o111


def _runner_module():  # type: ignore[no-untyped-def]
    import importlib.util
    import sys

    sys.path.insert(0, str(RUNNER.parent))
    try:
        spec = importlib.util.spec_from_file_location("dsh_worker_runner", RUNNER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module
    finally:
        sys.path.remove(str(RUNNER.parent))


_ENV_DUMPING_DSH = """#!/usr/bin/env python3
import json, os
with open(os.environ["E2E014_ENV_DUMP"], "w", encoding="utf-8") as handle:
    json.dump(dict(os.environ), handle)
raise SystemExit(5)
"""


def _probe_child_environment(tmp_path, monkeypatch, provider_environment):  # type: ignore[no-untyped-def]
    """Run the real interrupt_probe up to the spawn of its own DSH and return
    the environment that child process actually received."""

    import json
    import os
    import stat
    import time

    import pytest

    runner = _runner_module()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "dsh"
    fake.write_text(_ENV_DUMPING_DSH, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    dump = tmp_path / "child-env.json"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("E2E014_ENV_DUMP", str(dump))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    class _DumpThenNotReady(runner.DshHarnessProcess):  # type: ignore[misc,name-defined]
        # The real class composes and spawns; only readiness is cut short
        # once the child has written what it received.
        def wait_ready(self, timeout: float) -> bool:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not dump.exists():
                time.sleep(0.05)
            return False

    monkeypatch.setattr(runner, "DshHarnessProcess", _DumpThenNotReady)
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(AssertionError, match="not ready"):
        runner.interrupt_probe(
            state, "standard", tmp_path, runner.Report(), 5.0, provider_environment
        )
    assert dump.exists(), "the probe never spawned its DSH"
    return json.loads(dump.read_text(encoding="utf-8"))


def test_dsh_interrupt_probe_hands_the_provider_key_to_its_dsh(tmp_path, monkeypatch) -> None:
    """The in-process probe spawns its own DSH from the RUNNER's environment,
    not the isolated daemon's: the key the runner resolved into daemon.env
    must reach that child.  Without it every probe turn (interrupted or not)
    ended "DSH turn ended with reason error" -- nightly E2E-014 dsh.interrupt
    red, 2026-09-24 onward."""

    child = _probe_child_environment(
        tmp_path, monkeypatch, {"DEEPSEEK_API_KEY": "SENTINEL-PROBE-KEY"}
    )
    assert child["DEEPSEEK_API_KEY"] == "SENTINEL-PROBE-KEY"


def test_dsh_interrupt_probe_invents_no_key_when_none_is_configured(tmp_path, monkeypatch) -> None:
    """Negative control: with no provider environment and no ambient key,
    the child carries no DEEPSEEK_API_KEY -- so the positive test cannot be
    satisfied by a key that is always injected from somewhere."""

    child = _probe_child_environment(tmp_path, monkeypatch, {})
    assert "DEEPSEEK_API_KEY" not in child
