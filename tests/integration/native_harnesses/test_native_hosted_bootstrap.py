"""Real adopted-tree bootstrap imports; stub only the final CLI execution boundary."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
CORE = ("shell.impl.cli.runtime.entry", "daemon.impl.application", "daemon.impl.owner_migration",
        "identity.impl.agents.registry")
GRANTS = "identity.impl.agents.runtime.grants"


def _load(name):
    path = ROOT / "contract/e2e" / name
    spec = importlib.util.spec_from_file_location("native_bootstrap_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.base if name == "run-capability-ledger.py" else module


def _run(runner, tmp_path, *, mutated=None, source_root=ROOT, import_root=ROOT):
    for name in ("home", "hyprial", "state", "config", "cache"):
        (tmp_path / name).mkdir()
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"}}
    environment.update({
        "HOME": str(tmp_path / "home"), "HYPRIAL_HOME": str(tmp_path / "hyprial"),
        "HARNESS_STATE_DIR": str(tmp_path / "state"), "TMPDIR": str(tmp_path),
        "HARNESS_SOCKET_PATH": str(tmp_path / "daemon.sock"), "HARNESS_CONFIG": str(tmp_path / "config.json"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "HYPRIAL_NETWORK_ISOLATED": "1", "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "",
        "HYPRIAL_USAGE_DISABLE": "1", "HYPRIAL_OWNER": "bootstrap-counterexample",
        "PYTHONPATH": str(import_root / "src"), "PYTHONDONTWRITEBYTECODE": "1",
        "AT01_SOURCE": str(source_root), "AT01_IMPORT_EVIDENCE": str(tmp_path / "imports.json"),
        "AT01_TIMING_EVIDENCE": str(tmp_path / "timing.json"),
        "AT01_FAULTHANDLER_EVIDENCE": str(tmp_path / "stack.txt"),
        "AT01_PROBE_EVIDENCE": str(tmp_path / "probe.jsonl"),
    })
    bootstrap = runner.BOOTSTRAP if mutated is None else mutated
    assert bootstrap.count("hyprial.cli.main()") == 1
    # Do not pre-import CLI: native entry imports must remain inside measured region.
    bootstrap = bootstrap.replace("hyprial.cli.main()",
                                  "hyprial.cli.main = lambda: print('CLI-STUB-ONLY')\nhyprial.cli.main()")
    child = subprocess.Popen([sys.executable, "-c", bootstrap], env=environment,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise
    assert child.poll() is not None
    return subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)


@pytest.mark.parametrize("name,expected", [
    ("run-host-invite.py", CORE), ("run-capability-ledger.py", (*CORE, GRANTS)),
])
def test_real_native_generated_bootstrap_imports_and_times_owned_providers(tmp_path, name, expected):
    runner = _load(name)
    result = _run(runner, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLI-STUB-ONLY"
    imported = json.loads((tmp_path / "imports.json").read_text())
    timing = json.loads((tmp_path / "timing.json").read_text())
    assert tuple(imported) == expected
    assert tuple(timing["modules"]) == expected
    for module, record in imported.items():
        provider = Path(record["path"])
        assert provider.is_relative_to(ROOT / "src")
        assert hashlib.sha256(provider.read_bytes()).hexdigest() == record["sha256"]
        assert provider == (ROOT / "src/hyprial" / module.replace(".", "/")).with_suffix(".py") or provider == ROOT / "src/hyprial" / module.replace(".", "/") / "__init__.py"
    assert timing["coreWall"] == pytest.approx(sum(timing["modules"][item]["wall"] for item in CORE))
    assert timing["coreCpu"] == pytest.approx(sum(timing["modules"][item]["cpu"] for item in CORE))
    assert timing["allWall"] == pytest.approx(sum(value["wall"] for value in timing["modules"].values()))
    assert timing["sourceCheckWall"] >= 0
    probe = [json.loads(line) for line in (tmp_path / "probe.jsonl").read_text().splitlines()]
    assert probe[0]["hooks"] == {"get_data": True, "read_text": True}
    assert [row["name"] for row in probe if row["kind"] == "mark"] == ["imports-done", "cli-main"]
    assert probe[-1].get("final") is True


def test_native_bootstrap_rejects_missing_required_provider_before_cli(tmp_path):
    runner = _load("run-host-invite.py")
    mutated = runner.BOOTSTRAP.replace("'daemon.impl.application'", "'daemon.impl.missing_application'")
    assert mutated != runner.BOOTSTRAP
    result = _run(runner, tmp_path, mutated=mutated)
    assert result.returncode != 0 and "AssertionError" in result.stderr
    assert "daemon.impl.missing_application" in result.stderr
    assert "CLI-STUB-ONLY" not in result.stdout
    assert not (tmp_path / "imports.json").exists()
    assert not (tmp_path / "timing.json").exists()


def test_native_bootstrap_keeps_actual_selected_source_fence(tmp_path):
    runner = _load("run-host-invite.py")
    result = _run(runner, tmp_path, source_root=tmp_path / "not-the-selected-tree")
    assert result.returncode != 0 and "AssertionError" in result.stderr
    assert "CLI-STUB-ONLY" not in result.stdout
    assert not (tmp_path / "imports.json").exists()


def test_precompile_probe_selects_same_native_core_as_bootstrap():
    runner = _load("run-host-invite.py")
    suite = _load("run-hosted-suite.py")
    assert tuple(runner.CORE_MODULES) == CORE
    assert tuple(suite.BOOTSTRAP_MODULES) == CORE


LEGACY_CORE = ("cli", "daemon.application", "daemon.owner_migration", "agents.registry")


def _original_tree(root):
    for name in (*LEGACY_CORE, "agents.grants"):
        path = root / "src/hyprial" / (name.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def main():\n    pass\n" if name == "cli" else "VALUE = 1\n")
        parent = path.parent
        while parent != root / "src":
            (parent / "__init__.py").touch()
            parent = parent.parent
    return root


@pytest.mark.parametrize("name,expected", [
    ("run-host-invite.py", LEGACY_CORE), ("run-capability-ledger.py", (*LEGACY_CORE, "agents.grants")),
])
def test_selected_original_layout_preserves_actual_provider_names_and_timing(tmp_path, name, expected):
    runner = _load(name)
    source = _original_tree(tmp_path / "original")
    output = tmp_path / "probe"
    output.mkdir()
    result = _run(runner, output, source_root=source, import_root=source)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLI-STUB-ONLY"
    imported = json.loads((output / "imports.json").read_text())
    timing = json.loads((output / "timing.json").read_text())
    assert tuple(imported) == expected and tuple(timing["modules"]) == expected
    assert all(Path(row["path"]).is_relative_to(source / "src") for row in imported.values())
    assert timing["coreWall"] == pytest.approx(sum(timing["modules"][item]["wall"] for item in LEGACY_CORE))
    assert runner.read_import_timing(output / "timing.json", output / "imports.json")["note"] is None
    assert _load("run-hosted-suite.py").bootstrap_modules(source) == LEGACY_CORE


def test_selected_original_layout_missing_provider_fails_closed_before_cli(tmp_path):
    runner = _load("run-host-invite.py")
    source = _original_tree(tmp_path / "original")
    (source / "src/hyprial/daemon/application.py").unlink()
    output = tmp_path / "probe"
    output.mkdir()
    result = _run(runner, output, source_root=source, import_root=source)
    assert result.returncode != 0 and "AssertionError" in result.stderr
    assert "daemon.application" in result.stderr and "CLI-STUB-ONLY" not in result.stdout
    assert not (output / "imports.json").exists()


def test_original_provider_internal_import_error_is_not_retried_in_another_layout(tmp_path):
    runner = _load("run-host-invite.py")
    source = _original_tree(tmp_path / "original")
    (source / "src/hyprial/daemon/application.py").write_text("raise ImportError('ORIGINAL_INNER_IMPORT_FAILED')\n")
    output = tmp_path / "probe"
    output.mkdir()
    result = _run(runner, output, source_root=source, import_root=source)
    assert result.returncode != 0 and "ORIGINAL_INNER_IMPORT_FAILED" in result.stderr
    assert "CLI-STUB-ONLY" not in result.stdout and not (output / "imports.json").exists()


def test_timing_reader_rejects_mixed_complete_core_layouts(tmp_path):
    runner = _load("run-host-invite.py")
    modules = {name: {"wall": 1.0, "cpu": 0.5} for name in (*CORE, *LEGACY_CORE)}
    timing = tmp_path / "timing.json"
    timing.write_text(json.dumps({"modules": modules, "sourceCheckWall": 0.1,
                                  "coreWall": 4.0, "coreCpu": 2.0, "allWall": 8.0, "allCpu": 4.0}))
    result = runner.read_import_timing(timing)
    assert result["coreWall"] is None and result["note"]
