"""Crash and concurrent legacy startup at the real loader's filesystem seam."""

import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from hyprial.daemon.impl.harnesses.pi.loader import PI_CONTROLLED_LOADER
from ..support import options, refused, reservation, running


def instrumented(tmp_path, payload, env, script):
    hook = tmp_path / "preload.mjs"
    hook.write_text('import fs from "node:fs";\nimport { syncBuiltinESMExports } from "node:module";\n' + script + '\nsyncBuiltinESMExports();\n')
    return subprocess.Popen([shutil.which("node"), "--import", str(hook), str(PI_CONTROLLED_LOADER), "run-rpc", json.dumps(payload)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


@pytest.mark.parametrize("stage", ["mkdir", "owner", "receipt"])
def test_process_crash_leaves_non_stealable_claim(tmp_path, stage):
    payload, env = options(tmp_path)
    script = '''const write = fs.writeFileSync;
fs.writeFileSync = (path, ...args) => {
  if (String(path).endsWith("owner.json") && STAGE === "mkdir") process.exit(81);
  const result = write(path, ...args);
  if (String(path).endsWith("owner.json") && STAGE === "owner") process.exit(82);
  return result;
};
const rename = fs.renameSync;
fs.renameSync = (from, to) => {
  const result = rename(from, to);
  if (String(to).includes(".hyprial-loader-receipts") && STAGE === "receipt") process.exit(83);
  return result;
};'''.replace('STAGE', json.dumps(stage))
    child = instrumented(tmp_path, payload, env, script)
    _, stderr = child.communicate('{"type":"get_state","id":"ready"}\n', timeout=20)
    assert child.returncode in {81, 82, 83}, stderr
    assert reservation(payload).exists()
    before = {p.name: p.read_bytes() for p in reservation(payload).iterdir()}
    refused(payload, env, "MODEL_SESSION_BUSY")
    assert before == {p.name: p.read_bytes() for p in reservation(payload).iterdir()}


def test_legacy_and_new_first_launch_cannot_race_receipt(tmp_path):
    payload, env = options(tmp_path)
    legacy = {k: v for k, v in payload.items() if k != "modelBinding"}
    entered, release = tmp_path / "entered", tmp_path / "release"
    script = '''const write = fs.writeFileSync;
fs.writeFileSync = (path, ...args) => {
  if (String(path).includes(".hyprial-loader-receipts")) {
    write(ENTERED, "ready");
    const deadline = Date.now() + 15000;
    while (!fs.existsSync(RELEASE) && Date.now() < deadline) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
  }
  return write(path, ...args);
};'''.replace('ENTERED', json.dumps(str(entered))).replace('RELEASE', json.dumps(str(release)))
    child = instrumented(tmp_path, legacy, env, script)
    try:
        deadline = time.monotonic() + 15
        while not entered.exists():
            assert child.poll() is None, child.stderr.read()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        refused(payload, env, "MODEL_SESSION_BUSY")
    finally:
        release.touch()
        child.communicate('{"type":"get_state","id":"ready"}\n', timeout=20)
    assert child.returncode == 0
    assert "modelBinding" not in json.loads(Path(payload["receiptPath"]).read_text())


def test_owner_token_change_is_not_deleted_by_shutdown(tmp_path):
    payload, env = options(tmp_path)
    with running(payload, env) as (child, _):
        owner = reservation(payload) / "owner.json"
        owner.write_text('{"schemaVersion":1,"ownerToken":"replacement"}')
        child.stdin.close()
        child.wait(timeout=5)
        assert child.returncode != 0
    assert json.loads(owner.read_text())["ownerToken"] == "replacement"


def test_killed_child_requires_new_session_and_preserves_residual(tmp_path):
    payload, env = options(tmp_path)
    with running(payload, env) as (child, _):
        child.kill()
        child.wait(timeout=5)
    refused(payload, env, "MODEL_SESSION_BUSY")
    assert reservation(payload).exists()


@pytest.mark.parametrize("early", [False, True])
def test_failed_disposal_never_releases_writer_claim(tmp_path, early):
    payload, env = options(tmp_path)
    session_module = (Path(payload["piPackageRoot"]) / "dist/core/agent-session.js").as_uri()
    script = f'import {{ AgentSession }} from {json.dumps(session_module)};\n'
    script += 'AgentSession.prototype.abort = async () => { throw new Error("synthetic abort failure"); };\n'
    if early:
        script += 'fs.renameSync = () => { throw new Error("synthetic receipt failure"); };\n'
    child = instrumented(tmp_path, payload, env, script)
    _, stderr = child.communicate('{"type":"get_state","id":"ready"}\n', timeout=20)
    assert child.returncode != 0, stderr
    assert reservation(payload).exists()
    refused(payload, env, "MODEL_SESSION_BUSY")
