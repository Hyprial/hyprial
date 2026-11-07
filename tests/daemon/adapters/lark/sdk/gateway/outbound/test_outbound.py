from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
import pytest
from lark_oapi.api.im.v1 import ReplyMessageRequestBody
from hyprial.daemon.impl.adapters.lark import LarkApiError, LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import lark_file_type

from ..support import (
    FakeClient,
    _RecordingLogger,
    _nonzero_response_with_message_id,
)


def test_send_rejects_nonzero_code_even_when_message_id_present() -> None:
    """A platform answer that carries a message id must never count as
    success when code != 0 — and the full answer must reach the log.

    Mutation guard: revert this path to "has a message id => success" and
    the ``pytest.raises(LarkApiError)`` assertion goes red.
    """

    client = FakeClient()
    client.message.create = lambda _request: _nonzero_response_with_message_id()
    logger = _RecordingLogger()
    gateway = LarkSdkGateway(client, logger=logger, gateway_name="hq-adjutant-feishu")

    with pytest.raises(LarkApiError) as error:
        gateway.send_chat("oc_test", "body must never leak", idempotency_key="trap")

    assert error.value.operation == "send chat message"
    assert error.value.code == 230012
    assert error.value.lark_message == "content rejected"

    assert len(logger.records) == 1
    record = logger.records[0]
    assert record["level"] == "warn"
    assert record["event"] == "lark.send"
    assert record["operation"] == "send chat message"
    assert record["code"] == 230012
    assert record["msg"] == "content rejected"
    assert record["nativeMessageId"] == "om_trap"
    assert record["target"] == "oc_test"
    assert record["gateway"] == "hq-adjutant-feishu"
    assert "body must never leak" not in repr(record)


def test_send_logs_full_platform_answer_on_success() -> None:
    """Even a successful send records code + msg + the native message id,
    so an accepted om_ can later be matched to what the platform returned."""

    client = FakeClient()
    logger = _RecordingLogger()
    gateway = LarkSdkGateway(client, logger=logger, gateway_name="hq-adjutant-feishu")

    native_message_id = gateway.send_chat("oc_test", "hello", idempotency_key="ok")

    assert native_message_id == "om_owner_dm"
    assert len(logger.records) == 1
    record = logger.records[0]
    assert record["level"] == "info"
    assert record["event"] == "lark.send"
    assert record["code"] == 0
    assert record["msg"] == "ok"
    assert record["nativeMessageId"] == "om_owner_dm"
    assert record["target"] == "oc_test"


def test_send_reply_rejects_nonzero_code_with_message_id() -> None:
    client = FakeClient()
    client.message.reply = lambda _request: _nonzero_response_with_message_id()
    logger = _RecordingLogger()
    gateway = LarkSdkGateway(client, logger=logger)

    with pytest.raises(LarkApiError) as error:
        gateway.reply("om_parent", "hello", idempotency_key="trap-reply")

    assert error.value.operation == "reply message"
    assert error.value.code == 230012
    assert len(logger.records) == 1
    assert logger.records[0]["level"] == "warn"
    assert logger.records[0]["nativeMessageId"] == "om_trap"


def test_send_with_unparseable_code_raises_lark_api_error() -> None:
    """A response whose ``code`` did not parse must still raise the typed
    error carrying the platform answer — never a TypeError from inside the
    error path — and ``msg=None`` must stay a JSON null, not the string
    "None"."""

    client = FakeClient()
    client.message.create = lambda _request: SimpleNamespace(
        success=lambda: False,
        code=None,
        msg=None,
        data=SimpleNamespace(message_id="om_unparsed"),
    )
    logger = _RecordingLogger()
    gateway = LarkSdkGateway(client, logger=logger)

    with pytest.raises(LarkApiError) as error:
        gateway.send_chat("oc_test", "hello", idempotency_key="unparsed")

    assert error.value.code is None
    assert error.value.lark_message is None
    assert "None" not in str(error.value)
    assert logger.records[0]["msg"] is None
    assert logger.records[0]["code"] is None


def test_sdk_gateway_uploads_and_sends_file_with_file_message_type() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)

    # Bytes come from path_authz; the SDK boundary never opens a path.
    evidence = gateway.send_chat_file(
        "oc_test",
        "proof.txt",
        b"proof-body\n",
        media_type="text/plain",
        idempotency_key="operation:file:0",
    )

    upload = client.file.creates[0].request_body
    assert upload.file_type == "stream"
    assert upload.file_name == "proof.txt"
    assert upload.file.getvalue() == b"proof-body\n"
    sent = client.message.creates[0]
    assert sent.receive_id_type == "chat_id"
    assert sent.request_body.receive_id == "oc_test"
    assert sent.request_body.msg_type == "file"
    assert json.loads(sent.request_body.content) == {"file_key": "file_uploaded"}
    assert sent.request_body.uuid == str(
        uuid5(NAMESPACE_URL, "hyprial:lark-route-file:operation:file:0")
    )
    assert evidence == {
        "kind": "file",
        "fileName": "proof.txt",
        "mediaType": "text/plain",
        "sizeBytes": len(b"proof-body\n"),
        "fileType": "stream",
        "fileKey": "file_sent",
        "nativeMessageId": "om_owner_dm",
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("voice.opus", "opus"),
        ("movie.mp4", "mp4"),
        ("report.PDF", "pdf"),
        ("letter.docx", "doc"),
        ("table.xlsx", "xls"),
        ("slides.pptx", "ppt"),
        ("proof.txt", "stream"),
    ],
)
def test_lark_file_type_uses_documented_native_types_or_stream(
    name: str, expected: str
) -> None:
    assert lark_file_type(Path(name)) == expected


def test_sdk_gateway_uploads_and_sends_image_without_regressing_image_path() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)

    evidence = gateway.send_chat_image(
        "oc_test",
        "pixel.png",
        b"png-body",
        media_type="image/png",
        idempotency_key="operation:image:0",
    )

    upload = client.image.creates[0].request_body
    assert upload.image_type == "message"
    assert upload.image.getvalue() == b"png-body"
    sent = client.message.creates[0]
    assert sent.request_body.msg_type == "image"
    assert json.loads(sent.request_body.content) == {"image_key": "img_uploaded"}
    assert sent.request_body.uuid == str(
        uuid5(NAMESPACE_URL, "hyprial:lark-route-image:operation:image:0")
    )
    assert evidence == {
        "kind": "image",
        "fileName": "pixel.png",
        "mediaType": "image/png",
        "sizeBytes": len(b"png-body"),
        "imageKey": "img_sent",
        "nativeMessageId": "om_owner_dm",
    }


def test_owner_dm_derives_a_stable_bounded_lark_uuid() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)
    business_key = "operation:" + "very-long-owner-segment-" * 8

    gateway.send_owner_dm("ou_owner", "first", idempotency_key=business_key)
    gateway.send_owner_dm("ou_owner", "retry", idempotency_key=business_key)

    uuids = [request.request_body.uuid for request in client.message.creates]
    assert uuids[0] == uuids[1]
    assert uuids[0] != business_key
    assert len(uuids[0]) <= 50


def test_reply_derives_a_stable_bounded_lark_uuid() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)
    business_key = "native-message:" + "very-long-response-kind-" * 8

    reply_text = 'same \\ escaped "body" 界'
    gateway.reply("om_in", reply_text, idempotency_key=business_key)
    gateway.reply("om_in", reply_text, idempotency_key=business_key)

    bodies = [request.request_body for request in client.message.replies]
    assert all(isinstance(body, ReplyMessageRequestBody) for body in bodies)
    assert [body.content for body in bodies] == [
        json.dumps({"text": reply_text}, ensure_ascii=False),
        json.dumps({"text": reply_text}, ensure_ascii=False),
    ]
    uuids = [body.uuid for body in bodies]
    assert uuids[0] == uuids[1]
    assert uuids[0] == str(
        uuid5(NAMESPACE_URL, f"hyprial:lark-reply:{business_key}")
    )
    assert len(uuids[0]) <= 50
