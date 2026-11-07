"""Load the accepted schema-2 implementation for explicit rollback tests only."""
from pathlib import Path
import subprocess
import os
import json
import sys
import types

BASE = "ee383f80c1505805ea1bb128157ed09896604eca"


def golden_documents():
    return json.loads(Path(__file__).with_name("adapter-model-goldens.json").read_text())


def base_runtime(monkeypatch):
    if os.environ.get("HYPRIAL_SCHEMA2_EVIDENCE") != "1":
        import pytest
        pytest.skip("explicit base-execution evidence: set HYPRIAL_SCHEMA2_EVIDENCE=1; not CI evidence")
    prefix = "hyprial.daemon.impl._schema2_fixture"
    package = types.ModuleType(prefix)
    root = Path(__file__).resolve().parents[3] / "src/hyprial/daemon/impl/desired_state"
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, prefix, package)

    def load(path, name):
        source = subprocess.run(["git", "show", f"{BASE}:{path}"], check=True,
                                capture_output=True, text=True, timeout=30).stdout
        module = types.ModuleType(name)
        module.__package__ = name.rsplit(".", 1)[0]
        monkeypatch.setitem(sys.modules, name, module)
        exec(compile(source, path, "exec"), module.__dict__)
        return module

    launch = load("src/hyprial/kernel/impl/facts/launch_spec.py", f"{prefix}.launch")
    documents = load("src/hyprial/daemon/impl/desired_state/documents.py", f"{prefix}.documents")
    documents.HarnessLaunchSpec = launch.HarnessLaunchSpec
    sqlite = load("src/hyprial/daemon/impl/desired_state/sqlite.py", f"{prefix}.sqlite")
    load("src/hyprial/daemon/impl/desired_state/store_io.py", f"{prefix}.store_io")
    store = load("src/hyprial/daemon/impl/desired_state/store.py", f"{prefix}.store")
    store.DesiredStateSqliteShadow = sqlite.DesiredStateSqliteShadow
    runtime = load("src/hyprial/daemon/impl/transfer/landing/runtime.py", f"{prefix}.runtime")
    runtime.DesiredState = documents.DesiredState
    runtime.HarnessLaunchSpec = launch.HarnessLaunchSpec
    runtime.DesiredStateStore = store.DesiredStateStore
    runtime.DesiredStateSqliteShadow = sqlite.DesiredStateSqliteShadow
    return runtime
