from __future__ import annotations

import asyncio
import base64
import gc
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.contracts import NativeSubmissionOutcome, freeze_json_v1
from hyprial.daemon.impl.adapters.worker.wire import WorkerIdentity

from .support import instance, operation


tryx = pytest.importorskip("tryx", reason="exact reviewed Tryx wheel is an external test input")


_ACCOUNT_JID = "15550001@s.whatsapp.net"
_CHAT_JID = "120363000000@g.us"


class _ExactDriverWorker:
    def __init__(self, driver) -> None:
        self.driver = driver

    def invoke(self, payload: object, *, timeout: float = 15.0) -> object:
        del timeout
        return self.driver.handle_control("invoke", payload)


def _driver(tmp_path: Path, generation: int):
    from hyprial.daemon.impl.adapters.whatsapp.worker.tryx_driver import (
        TryxNativeDriver,
    )

    session = (
        tmp_path
        / "adapters"
        / "whatsapp"
        / "wa-main"
        / str(generation)
        / "native-session"
    )
    session.mkdir(parents=True)
    context = SimpleNamespace(
        identity=WorkerIdentity("wa-main", "whatsapp", generation),
        session_dir=session,
        emit_callback=lambda value: True,
    )
    return TryxNativeDriver(context)


def _release_driver(driver) -> None:
    driver.store.close()
    driver._loop.close()
    driver.app = None
    driver.backend = None
    gc.collect()


def _make_submit_unknown(driver, provider_message_id: str) -> None:
    def uncertain(node_id, awaitable, timeout: float = 30.0):
        del timeout
        awaitable.close()
        driver.store.note_submission_candidate(node_id, provider_message_id)
        raise TimeoutError("provider outcome unavailable")

    driver._run_submission = uncertain


async def _start_runtime(driver, generation: int):
    from hyprial.daemon.impl.adapters.whatsapp import ArtifactStatus, WhatsAppRuntime

    runtime = WhatsAppRuntime(
        _ExactDriverWorker(driver),
        instance=instance(generation),
        account_jid=_ACCOUNT_JID,
        artifact=ArtifactStatus.available_candidate("reviewed-wheel"),
    )
    await runtime.start(instance(generation), object())
    return runtime


def test_exact_wheel_no_account_native_objects_map_jid_empty_edit_and_quote() -> None:
    from tryx.types import JID, MessageInfo
    from tryx.waproto.whatsapp_pb2 import Message

    from hyprial.daemon.impl.adapters.whatsapp.worker.tryx_driver import (
        _event_kind,
        _jid_text,
        _quoted_message_id,
    )

    assert MessageInfo.edit.__class__.__name__ == "getset_descriptor"
    assert _jid_text(JID("15550001", "s.whatsapp.net")) == "15550001@s.whatsapp.net"
    assert _event_kind("Empty", has_reaction=False) == "message"
    assert _event_kind("MessageEdit", has_reaction=False) == "edit"

    native = Message()
    native.extendedTextMessage.text = "quoted reply"
    native.extendedTextMessage.contextInfo.stanzaId = "wamid-native-parent"
    assert _quoted_message_id(native) == "wamid-native-parent"


def test_exact_wheel_no_account_store_and_client_construct_on_stable_path(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.whatsapp.worker.tryx_driver import (
        _durable_directory,
    )

    session = tmp_path / "adapters" / "whatsapp" / "wa-main" / "8" / "native-session"
    durable = _durable_directory(session)
    durable.mkdir(parents=True)
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from types import SimpleNamespace; import sys; "
            "from hyprial.daemon.impl.adapters.worker.wire import WorkerIdentity; "
            "from hyprial.daemon.impl.adapters.whatsapp.worker.tryx_driver import TryxNativeDriver; "
            "session=Path(sys.argv[1]); session.mkdir(parents=True); "
            "context=SimpleNamespace(identity=WorkerIdentity('wa-main','whatsapp',8),"
            "session_dir=session,emit_callback=lambda value: True); "
            "driver=TryxNativeDriver(context); assert driver.app.get_client() is not None; "
            "assert driver.store.path.parent.name == 'durable'; print('TRYX_NO_ACCOUNT_OK')",
            str(session),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert "TRYX_NO_ACCOUNT_OK" in smoke.stdout
    assert durable == tmp_path / "adapters" / "whatsapp" / "wa-main" / "durable"


@pytest.mark.parametrize("evidence", ["receipt", "echo"])
def test_exact_wheel_text_unknown_replay_and_reconcile_survive_w1_restart(
    tmp_path: Path, evidence: str
) -> None:
    child_marker = f"text-{evidence}"
    if os.environ.get("HYPRIAL_WHATSAPP_EXACT_CHILD") != child_marker:
        environment = dict(os.environ)
        environment["HYPRIAL_WHATSAPP_EXACT_CHILD"] = child_marker
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from tests.daemon.adapters.whatsapp.test_tryx_wheel import "
                "test_exact_wheel_text_unknown_replay_and_reconcile_survive_w1_restart as run; "
                "run(Path(sys.argv[1]), sys.argv[2])",
                str(tmp_path),
                evidence,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        return

    async def scenario() -> None:
        node = operation("text-unknown")
        first_driver = _driver(tmp_path, 7)
        try:
            first_runtime = await _start_runtime(first_driver, 7)
            _make_submit_unknown(first_driver, "wamid-text-unknown")
            prior = await first_runtime.submit(node)
            assert isinstance(prior, NativeSubmissionOutcome)
            assert prior.submissions[0].state == "unknown"
        finally:
            _release_driver(first_driver)

        restarted_driver = _driver(tmp_path, 8)
        try:
            provider_calls = 0

            def forbidden_resend(node_id, awaitable, timeout: float = 30.0):
                nonlocal provider_calls
                del node_id, timeout
                provider_calls += 1
                awaitable.close()
                raise AssertionError("unknown submission was resent")

            restarted_driver._run_submission = forbidden_resend
            restarted_runtime = await _start_runtime(restarted_driver, 8)
            replay = await restarted_runtime.submit(node)
            assert replay.submissions[0].state == "unknown"
            assert provider_calls == 0

            fresh_runtime = await _start_runtime(restarted_driver, 8)
            still_unknown = await fresh_runtime.reconcile(node, prior)
            assert still_unknown.submissions[0].state == "unknown"
            assert restarted_driver.store.settle_native_evidence(
                "wamid-text-unknown", evidence
            )
            settled = await fresh_runtime.reconcile(node, prior)
            assert settled.submissions[0].state == "accepted"
            assert settled.submissions[0].provider_message_id == "wamid-text-unknown"
            assert provider_calls == 0

            changed = SimpleNamespace(
                node_id=node.node_id,
                kind=node.kind,
                idempotency_key=node.idempotency_key,
                sealed_request=freeze_json_v1(
                    {
                        "chatJid": _CHAT_JID,
                        "text": "changed reply",
                        "quotedMessageId": "wamid-parent",
                        "partIndex": 0,
                    }
                ),
            )
            with pytest.raises(Exception, match="changed request"):
                await fresh_runtime.reconcile(changed, prior)
        finally:
            _release_driver(restarted_driver)

    asyncio.run(scenario())


def test_exact_wheel_media_unknown_restage_is_reconcile_only_across_w1_restart(
    tmp_path: Path,
) -> None:
    child_marker = "media"
    if os.environ.get("HYPRIAL_WHATSAPP_EXACT_CHILD") != child_marker:
        environment = dict(os.environ)
        environment["HYPRIAL_WHATSAPP_EXACT_CHILD"] = child_marker
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from tests.daemon.adapters.whatsapp.test_tryx_wheel import "
                "test_exact_wheel_media_unknown_restage_is_reconcile_only_across_w1_restart as run; "
                "run(Path(sys.argv[1]))",
                str(tmp_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        return

    sealed_request = {
        "chatJid": _CHAT_JID,
        "handleId": "media-handle-1",
        "quotedMessageId": "wamid-parent",
    }

    def stage(driver, generation: int) -> str:
        scope = {
            "instanceId": "wa-main",
            "generation": generation,
            "accountJid": _ACCOUNT_JID,
        }
        begun = driver.handle_control(
            "invoke",
            {
                "operation": "media.upload-begin",
                "nodeId": "media-unknown",
                "chatJid": _CHAT_JID,
                "name": "photo.jpg",
                "mediaType": "image/jpeg",
                "size": 5,
                "sha256": "6105d6cc76af400325e94d588ce511be5bfdbb73b437dc51eca43917d7a43e3d",
                **scope,
            },
        )
        upload_id = begun["uploadId"]
        driver.handle_control(
            "invoke",
            {
                "operation": "media.upload-chunk",
                "uploadId": upload_id,
                "dataBase64": base64.b64encode(b"image").decode(),
                **scope,
            },
        )
        finished = driver.handle_control(
            "invoke",
            {
                "operation": "media.upload-finish",
                "uploadId": upload_id,
                "sha256": "6105d6cc76af400325e94d588ce511be5bfdbb73b437dc51eca43917d7a43e3d",
                **scope,
            },
        )
        assert finished["staged"] is True
        return upload_id

    def send(driver, generation: int, upload_id: str, request: dict[str, object]):
        return driver.handle_control(
            "invoke",
            {
                "operation": "media.upload-send",
                "uploadId": upload_id,
                "nodeId": "media-unknown",
                "kind": "file-upload",
                "idempotencyKey": "idem:media-unknown",
                "chatJid": _CHAT_JID,
                "quotedMessageId": "wamid-parent",
                "sealedRequest": request,
                "instanceId": "wa-main",
                "generation": generation,
                "accountJid": _ACCOUNT_JID,
            },
        )

    async def scenario() -> None:
        first_driver = _driver(tmp_path, 7)
        try:
            await _start_runtime(first_driver, 7)
            first_upload = stage(first_driver, 7)
            _make_submit_unknown(first_driver, "wamid-media-unknown")
            prior = send(first_driver, 7, first_upload, sealed_request)
            assert prior["state"] == "unknown"
        finally:
            _release_driver(first_driver)

        restarted_driver = _driver(tmp_path, 8)
        try:
            await _start_runtime(restarted_driver, 8)
            second_upload = stage(restarted_driver, 8)
            assert second_upload != first_upload
            provider_calls = 0

            def forbidden_resend(node_id, awaitable, timeout: float = 30.0):
                nonlocal provider_calls
                del node_id, timeout
                provider_calls += 1
                awaitable.close()
                raise AssertionError("unknown media was resent")

            restarted_driver._run_submission = forbidden_resend
            replay = send(restarted_driver, 8, second_upload, sealed_request)
            assert replay["state"] == "unknown"
            assert provider_calls == 0

            third_upload = stage(restarted_driver, 8)
            changed_request = {**sealed_request, "handleId": "different-media"}
            with pytest.raises(ValueError, match="changed request"):
                send(restarted_driver, 8, third_upload, changed_request)
            assert provider_calls == 0
        finally:
            _release_driver(restarted_driver)

    asyncio.run(scenario())
