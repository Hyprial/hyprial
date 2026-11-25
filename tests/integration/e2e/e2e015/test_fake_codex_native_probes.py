"""The shared protocol fixture must satisfy the real P2 startup validator."""

import json
import os
from pathlib import Path
import subprocess
import sys

from hyprial.daemon.impl.harnesses.codex.native_env import _validate_codex_native_load


def test_fake_answers_mandatory_native_probes(tmp_path):
    native = tmp_path / "native"
    native.mkdir(mode=0o700)
    config = native / "config.toml"
    config.write_text('cli_auth_credentials_store = "ephemeral"\n')
    config.chmod(0o600)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    fixture = Path(__file__).resolve().parents[4] / "tests/fixtures/harnesses/fake_codex_app_server.py"
    requests = [
        {"id": 1, "method": "initialize", "params": {}},
        {"id": 2, "method": "config/read", "params": {"cwd": str(cwd), "includeLayers": True}},
        {"id": 3, "method": "account/read", "params": {}},
        {"id": 4, "method": "skills/list", "params": {"cwds": [str(cwd)]}},
    ]
    environment = dict(os.environ, CODEX_HOME=str(native))
    for key in ("FAKE_CODEX_LOG", "FAKE_CODEX_STATE", "FAKE_CODEX_RELEASE"):
        environment.pop(key, None)
    result = subprocess.run(
        [sys.executable, str(fixture)], env=environment,
        input="".join(json.dumps(row) + "\n" for row in requests),
        text=True, capture_output=True, check=True, timeout=10,
    )
    responses = {row["id"]: row["result"] for row in map(json.loads, result.stdout.splitlines())}
    evidence = _validate_codex_native_load(
        initialize=responses[1], config_read=responses[2],
        account_read=responses[3], skills_list=responses[4],
        native_root=native, cwd=cwd, model_provider=None,
    )
    assert evidence.codex_home == str(native)
    assert evidence.auth_store == "ephemeral"
