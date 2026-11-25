"""Provenance and reservation acceptance against the actual Pi RPC lifecycle."""

import json
from pathlib import Path
import signal

import pytest

from .support import options, refused, reservation, running
from hyprial.daemon.impl.harnesses.pi.loader import PiLoaderReceipt


def test_real_rpc_receipt_and_eof_release(tmp_path):
    payload, env = options(tmp_path)
    with running(payload, env) as (process, _):
        receipt = json.loads(Path(payload["receiptPath"]).read_text())
        assert receipt["modelBinding"] == payload["modelBinding"]
        assert PiLoaderReceipt.from_json(receipt).model_binding == payload["modelBinding"]
        assert reservation(payload).is_dir()
        process.stdin.close()
        process.wait(timeout=5)
        assert process.returncode == 0
    assert not reservation(payload).exists()


@pytest.mark.parametrize("repeat", [False, True])
def test_sigterm_releases_owner(tmp_path, repeat):
    payload, env = options(tmp_path)
    with running(payload, env) as (process, _):
        process.send_signal(signal.SIGTERM)
        if repeat:
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
    assert not reservation(payload).exists()


def test_two_resources_cannot_write_same_session(tmp_path):
    payload, env = options(tmp_path)
    with running(payload, env) as (first, _):
        before = Path(payload["receiptPath"]).read_bytes()
        refused({**payload, "requireExistingSession": True}, env, "MODEL_SESSION_BUSY")
        assert first.poll() is None
        assert Path(payload["receiptPath"]).read_bytes() == before
    assert not reservation(payload).exists()


def test_residual_empty_claim_is_never_stolen(tmp_path):
    payload, env = options(tmp_path)
    reservation(payload).mkdir(parents=True)
    refused(payload, env, "MODEL_SESSION_BUSY")
    assert reservation(payload).is_dir()
    assert not Path(payload["receiptPath"]).exists()


@pytest.mark.parametrize("mutation", ["model", "connectionRef", "configRevision", "profile", "bindingRef"])
def test_resumed_configuration_is_immutable(tmp_path, mutation):
    payload, env = options(tmp_path)
    with running(payload, env):
        pass
    receipt = Path(payload["receiptPath"])
    before = receipt.read_bytes()
    payload["requireExistingSession"] = True
    snapshot = payload["modelBinding"]
    snapshot[mutation] = 2 if mutation == "configRevision" else "changed"
    refused(payload, env, "MODEL_RESUME_MISMATCH")
    assert receipt.read_bytes() == before
    assert not reservation(payload).exists()


def test_legacy_session_without_snapshot_cannot_gain_provenance(tmp_path):
    payload, env = options(tmp_path)
    session = Path(payload["sessionRoot"]) / "2026-11-25T00-00-00_fixture-session.jsonl"
    session.write_text(json.dumps({"type": "session", "version": 3, "id": payload["sessionId"],
                                  "timestamp": "2026-11-25T00:00:00Z", "cwd": payload["cwd"]}) + '\n')
    payload["requireExistingSession"] = True
    refused(payload, env, "MODEL_RESUME_MISMATCH")
    assert not Path(payload["receiptPath"]).exists()
    assert not reservation(payload).exists()


def test_new_binding_refuses_tui_before_sdk(tmp_path):
    payload, env = options(tmp_path)
    refused(payload, env, "MODEL_COMBINATION_UNSUPPORTED", command="run-tui")
    assert not reservation(payload).exists()


def test_new_receipt_cannot_resume_through_legacy(tmp_path):
    payload, env = options(tmp_path)
    with running(payload, env):
        pass
    before = Path(payload["receiptPath"]).read_bytes()
    del payload["modelBinding"]
    refused(payload, env, "MODEL_RESUME_MISMATCH")
    assert Path(payload["receiptPath"]).read_bytes() == before


def test_missing_key_releases_early_claim(tmp_path):
    payload, env = options(tmp_path)
    del env["HYPRIAL_PI_CONNECTION_KEY"]
    refused(payload, env, "MODEL_AUTH_UNAVAILABLE")
    assert not reservation(payload).exists()
    assert not Path(payload["receiptPath"]).exists()


def test_early_sdk_failure_releases_before_receipt(tmp_path):
    from .support import start

    payload, env = options(tmp_path)
    payload["piPackageRoot"] = str(tmp_path / "missing-sdk")
    child = start(payload, env)
    _, stderr = child.communicate('', timeout=10)
    assert child.returncode != 0
    assert "synthetic-pi-test-key" not in stderr
    assert not reservation(payload).exists()
    assert not Path(payload["receiptPath"]).exists()
