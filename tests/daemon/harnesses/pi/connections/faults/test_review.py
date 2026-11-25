"""Startup signals, storage diagnostics and unknown cost at the loader boundary."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from hyprial.daemon.impl.harnesses.pi.loader import PI_CONTROLLED_LOADER
from ..support import options, reservation, running
from .test_claims import instrumented


@pytest.mark.parametrize("stage", ["first-handler", "bind-extensions"])
def test_sigterm_before_rpc_signal_registration_stops_and_releases(tmp_path, stage):
    payload, env = options(tmp_path)
    script = '''const on = process.on;
let sent = false;
process.on = function(name, handler) {
  const result = on.call(this, name, handler);
  if (name === "SIGTERM" && !sent) {
    sent = true;
    process.kill(process.pid, "SIGTERM");
  }
  return result;
};'''
    if stage == "bind-extensions":
        module = (Path(payload["piPackageRoot"]) / "dist/core/agent-session.js").as_uri()
        script = '''import { AgentSession } from MODULE;
const bind = AgentSession.prototype.bindExtensions;
AgentSession.prototype.bindExtensions = async function(...args) {
  process.kill(process.pid, "SIGTERM");
  await new Promise((resolve) => setTimeout(resolve, 30));
  return bind.apply(this, args);
};'''.replace("MODULE", json.dumps(module))
    child = instrumented(tmp_path, payload, env, script)
    try:
        child.wait(timeout=10)
        assert child.returncode == 143, child.stderr.read()
        assert not reservation(payload).exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


@pytest.mark.parametrize("mutation", ["token", "invalid-json", "missing-owner"])
def test_owner_change_reports_storage_failure_and_preserves_claim(tmp_path, mutation):
    payload, env = options(tmp_path)
    with running(payload, env) as (child, _):
        owner = reservation(payload) / "owner.json"
        if mutation == "missing-owner":
            owner.unlink()
        else:
            owner.write_text("{" if mutation == "invalid-json" else '{"schemaVersion":1,"ownerToken":"foreign"}')
        child.stdin.close()
        child.wait(timeout=5)
        stderr = child.stderr.read()
        assert child.returncode != 0
        assert "MODEL_COMBINATION_UNSUPPORTED: admission/session-storage" in stderr
        assert reservation(payload).exists()


def test_receipt_symlink_reports_storage_failure_before_sdk(tmp_path):
    payload, env = options(tmp_path)
    receipt = Path(payload["receiptPath"])
    receipt.parent.mkdir()
    target = tmp_path / "foreign.json"
    target.write_text('{"schema":"hyprial-pi-loader-v1"}')
    receipt.symlink_to(target)
    child = instrumented(tmp_path, payload, env, "")
    _, stderr = child.communicate('', timeout=15)
    assert "HYPRIAL_PI_START_FAILURE:MODEL_COMBINATION_UNSUPPORTED\nadmission/session-storage" in stderr
    assert not reservation(payload).exists()
    assert target.read_text() == '{"schema":"hyprial-pi-loader-v1"}'


@pytest.mark.parametrize("cost", [0, None, {"total": 0}])
def test_per_message_placeholder_cost_is_unknown(tmp_path, cost):
    payload, env = options(tmp_path)
    module = PI_CONTROLLED_LOADER.with_name("pi_model_binding.mjs").as_uri()
    script = '''import {boundModelRuntime} from MODULE;
const message = {role: "assistant", usage: {cost: COST, input: 1, output: 1}};
const source = () => ({async *[Symbol.asyncIterator]() {yield {message};}, async result() {return message;}});
const runtime = {stream: source, streamSimple: source};
const definition = {api: "openai-completions", baseUrl: "https://example.invalid/v1"};
boundModelRuntime(runtime, "fixture", "opaque-model", definition);
const stream = runtime.stream({provider: "fixture", id: "opaque-model", ...definition}, {}, {});
for await (const event of stream) console.log(JSON.stringify(event.message));
console.log(JSON.stringify(await stream.result()));
'''.replace("MODULE", json.dumps(module)).replace("COST", json.dumps(cost))
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                            capture_output=True, text=True, env=env, check=True)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert row["usage"]["costStatus"] == "unknown"
        assert row["usage"]["cost"] == dict.fromkeys(("input", "output", "cacheRead", "cacheWrite", "total"))
