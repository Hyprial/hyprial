"""Regenerate committed schema-2 compatibility outputs with the actual base.

Run: uv run python scripts/verify/regenerate_adapter_model_goldens.py
Requires the immutable base git object and test dependencies; never fetches it.
All database and ownership operations use disposable paths, not the user's home.
"""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.daemon.desired_state.compat import BASE, base_runtime  # noqa: E402
from tests.daemon.desired_state.test_adapter_model import legacy_document  # noqa: E402
from tests.daemon.desired_state.test_adapter_rollback import seed_agent_pins  # noqa: E402
from tests.integration.smolvm.runtime.test_smolvm_runtime_state import vm_spec  # noqa: E402
from hyprial.daemon.impl.adapters.rollback import downgrade_adapter_state  # noqa: E402
from hyprial.daemon.impl.desired_state import DesiredStateStore  # noqa: E402
from hyprial.daemon.impl.state_db import StateDatabase  # noqa: E402
from hyprial.kernel import DesiredStateError  # noqa: E402


def main():
    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory(prefix="adapter-golden-") as temp:
        root = Path(temp).resolve()
        patch.setenv("HYPRIAL_SCHEMA2_EVIDENCE", "1")
        patch.setenv("HYPRIAL_HOME", str(root / "home"))
        patch.setenv("HARNESS_STATE_DIR", str(root))
        api = base_runtime(patch)
        result = {
            "baseSha": BASE,
            "generate": "uv run python scripts/verify/regenerate_adapter_model_goldens.py",
            "evidence": "HYPRIAL_SCHEMA2_EVIDENCE=1 uv run pytest tests/daemon/desired_state/test_adapter_rollback.py tests/integration/smolvm/runtime/test_smolvm_runtime_state.py -q",
            "schema1": api.DesiredState.from_json(legacy_document(1)).to_json(),
            "schema2": api.DesiredState.from_json(legacy_document(2)).to_json(),
        }
        try:
            api.DesiredState.from_json({**result["schema2"], "schemaVersion": 3})
        except DesiredStateError as error:
            result["schema3Error"] = str(error)
        else:
            raise AssertionError("base unexpectedly accepted schema 3")
        pins = seed_agent_pins(root)
        original_pins = pins.read_bytes()
        with sqlite3.connect(pins) as db:
            result["pinsRowsJson"] = json.dumps(db.execute("SELECT adapter,agent FROM pins ORDER BY adapter").fetchall(), separators=(",", ":"))
        database = StateDatabase(root / "lifecycle-operations.sqlite3")
        shadow = api.DesiredStateSqliteShadow(database)
        shadow.write_document(result["schema2"])
        DesiredStateStore(root / "desired-state.json")
        downgrade_adapter_state(root)
        result["schema2Restored"] = api.DesiredState.from_json(shadow.read_document()).to_json()
        runtime_root = root / "runtime"
        runtime_root.mkdir()
        runtime_db = StateDatabase(runtime_root / "lifecycle-operations.sqlite3")
        runtime_shadow = api.DesiredStateSqliteShadow(runtime_db)
        document = legacy_document(2)
        document["providers"].append(vm_spec().to_json())
        result["schema2WithRuntime"] = api.DesiredState.from_json(document).to_json()
        runtime_shadow.write_document(result["schema2WithRuntime"])
        DesiredStateStore(runtime_root / "desired-state.json")
        downgrade_adapter_state(runtime_root)
        api.downgrade_state(runtime_root, root / "export")
        result["schema1AfterRuntimeDowngrade"] = api.DesiredState.from_json(runtime_shadow.read_document()).to_json()
        assert pins.read_bytes() == original_pins
        target = ROOT / "tests/daemon/desired_state/adapter-model-goldens.json"
        target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"PASS actual base {BASE}; wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
