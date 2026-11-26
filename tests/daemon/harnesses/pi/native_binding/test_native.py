"""Explicit native slots use the real Pi SDK without contacting model services."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest
from hyprial.identity import NativeAuthTarget
from hyprial.daemon.impl.harnesses.model_routes.native import NativeModelBinding
from hyprial.daemon.impl.harnesses.pi.bindings import validated_snapshot
from hyprial.daemon.impl.harnesses.pi.loader import PI_CONTROLLED_LOADER
from tests.daemon.harnesses.pi.connections.support import options, running, refused, reservation


def native_options(tmp_path):
    payload, env = options(tmp_path)
    selected = NativeModelBinding("native-binding", 1, NativeAuthTarget("pi", "openai", "api-key"), "gpt-4.1-mini")
    payload.update(modelBinding=selected.snapshot(), modelDefinitions={}, modelProvider="openai", model=selected.model)
    env.pop("HYPRIAL_PI_CONNECTION_KEY")
    auth = Path(payload["nativeRoot"]) / "auth.json"
    auth.write_text(json.dumps({"openai": {"type": "api_key", "key": "synthetic-native"},
                                "anthropic": {"type": "api_key", "key": "unselected"}}))
    return payload, env, auth


def test_real_sdk_native_selection_receipt_and_eof_release(tmp_path):
    payload, env, _ = native_options(tmp_path)
    with running(payload, env) as (process, rows):
        receipt = json.loads(Path(payload["receiptPath"]).read_text())
        assert validated_snapshot(receipt["modelBinding"])["nativeAuthRef"] == "native:v1:pi:api-key:openai"
        assert "synthetic-native" not in json.dumps(receipt)
        assert reservation(payload).exists()
    assert process.returncode == 0
    assert not reservation(payload).exists()


def test_missing_target_refuses_without_env_fallback(tmp_path):
    payload, env, auth = native_options(tmp_path)
    auth.write_text('{"anthropic":{"type":"api_key","key":"unselected"}}')
    env["OPENAI_API_KEY"] = "ambient-must-not-work"
    refused(payload, env, "MODEL_AUTH_UNAVAILABLE")
    assert not Path(payload["receiptPath"]).exists()
    assert not reservation(payload).exists()


def test_unknown_model_refuses_without_default_or_alias(tmp_path):
    payload, env, _ = native_options(tmp_path)
    payload["model"] = payload["modelBinding"]["model"] = "unknown-exact-model"
    refused(payload, env, "MODEL_COMBINATION_UNSUPPORTED")
    assert not Path(payload["receiptPath"]).exists()


@pytest.mark.parametrize("field,value", [("model", "changed"),
    ("nativeAuthRef", "native:v1:pi:oauth:openai"), ("bindingRef", "another-agent")])
def test_native_resume_cannot_replace_original_selection(tmp_path, field, value):
    payload, env, _ = native_options(tmp_path)
    with running(payload, env):
        pass
    receipt = Path(payload["receiptPath"])
    before = receipt.read_bytes()
    payload["requireExistingSession"] = True
    payload["modelBinding"][field] = value
    refused(payload, env, "MODEL_RESUME_MISMATCH")
    assert receipt.read_bytes() == before
    assert not reservation(payload).exists()


def test_runtime_auth_guard_uses_empty_env_and_rechecks_logout(tmp_path):
    payload, env, auth = native_options(tmp_path)
    module = PI_CONTROLLED_LOADER.with_name("pi_model_binding.mjs").as_uri()
    script = '''
import { boundNativeRuntime } from MODULE;
import { writeFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const snapshot = SNAPSHOT;
const physical = {provider:'openai',id:'gpt-4.1-mini',api:'openai-responses',baseUrl:'https://api.openai.com/v1'};
let observed;
const runtime = { getPhysicalModel: () => physical,
  getAuth: async (model, options) => { observed=options; return {auth:{apiKey:'synthetic'}}; },
  stream: () => 'stream', streamSimple: () => 'simple' };
boundNativeRuntime(runtime, snapshot, __AUTH_PATH__);
await runtime.getAuth(physical);
assert.deepEqual(observed.env, {});
assert.throws(() => runtime.stream(physical, {}, {apiKey:'different'}));
assert.throws(() => runtime.registerProvider('openai', {}));
writeFileSync(__AUTH_PATH__, '{}');
await assert.rejects(runtime.getAuth(physical), {code:'MODEL_AUTH_UNAVAILABLE'});
'''.replace('MODULE', json.dumps(module)).replace('SNAPSHOT', json.dumps(payload['modelBinding'])).replace('__AUTH_PATH__', json.dumps(str(auth)))
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_real_sdk_prepares_native_request_from_selected_file_not_ambient_key(tmp_path):
    payload, env, auth = native_options(tmp_path)
    env["OPENAI_API_KEY"] = "ambient-must-not-work"
    script = '''
import { ModelRuntime } from SDK;
import { boundNativeRuntime } from MODULE;
import assert from 'node:assert/strict';
const snapshot = SNAPSHOT;
const runtime = await ModelRuntime.create({authPath:AUTH_PATH,modelsPath:null,modelsStorePath:STORE_PATH});
boundNativeRuntime(runtime, snapshot, AUTH_PATH);
const model = runtime.getPhysicalModel('openai','gpt-4.1-mini');
const prepared = await runtime.prepareRequest(model, {});
assert.equal(prepared.options.apiKey, 'synthetic-native');
assert.equal(prepared.model.id, 'gpt-4.1-mini');
assert.equal(prepared.model.baseUrl, model.baseUrl);
'''.replace('SDK', json.dumps((Path(payload['piPackageRoot']) / 'dist/index.js').as_uri())).replace(
        'MODULE', json.dumps(PI_CONTROLLED_LOADER.with_name('pi_model_binding.mjs').as_uri())).replace(
        'SNAPSHOT', json.dumps(payload['modelBinding'])).replace('AUTH_PATH', json.dumps(str(auth))).replace(
        'STORE_PATH', json.dumps(str(auth.parent / 'request-models-store.json')))
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
