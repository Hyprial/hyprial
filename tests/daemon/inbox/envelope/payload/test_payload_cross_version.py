"""Wire option B: two-way compatibility with a 0.5.5.3.3 node.

The 0.5.5.3.3 side runs in the installed wheel's own interpreter
(``HYPRIAL_0553_PYTHON``, default the uv tool install), isolated with ``-I``
and a private HOME/TMPDIR, so none of this tree is importable there.

* new to old: a frame this tree's sender stamps decodes on 0.5.5.3.3, is
  received into its inbox and reads back through its ``message.read_body``
  row exactly like the unstamped frame; the unknown payload key is ignored.
  The old node stores the payload as it arrived, envelope included.
* old to new: a frame the 0.5.5.3.3 encoder produced is received here with
  no envelope; the stored payload is the old sender's bytes and the class
  comes from local inference.

The test is skipped only when the 0.5.5.3.3 interpreter is absent.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.envelope import (
    EnvelopeStampingTransport,
    derive_message_class,
    message_class_of,
)
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame,
    encode_delivery_frame,
)
from hyprial.kernel import MessageClass, mint_thread_id

OLD_PYTHON = Path(
    os.environ.get(
        "HYPRIAL_0553_PYTHON", "/root/.local/share/uv/tools/hyprial/bin/python"
    )
)
OLD_VERSION = "0.5.5.3.3"
THREAD = mint_thread_id(b"payload-cross-version-thread")
SENDER = "agent:op:node-1:driver"
RECIPIENT = "agent:op:node-2:reader"
FIELDS: dict[str, object] = {
    "message_id": "m-cross-1",
    "conversation_id": "c-cross",
    "sender": SENDER,
    "recipient": RECIPIENT,
    "intent": "request",
    "created_at_ms": 1_700_000_000_000,
    "idempotency_key": "k-cross-1",
    "summary": "S",
}
PAYLOAD = json.dumps(
    {"message": "héllo from new", "topic": None, "origin": {"via": "session"}},
    separators=(",", ":"),
).encode()

_OLD_SIDE = r"""
import base64, importlib.metadata, json, sys, tempfile
from pathlib import Path
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle, InboxMessage, InboxService, MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.history import conversation_messages
from hyprial.daemon.impl.inbox.links.zenoh_link import (
    decode_delivery_frame, encode_delivery_frame,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.queries import (
    _MessageQueriesMixin,
)

request = json.load(sys.stdin)
out = {"version": importlib.metadata.version("hyprial")}
if request["mode"] == "encode":
    fields = dict(request["fields"])
    message = InboxMessage(
        payload=base64.b64decode(request["payload"]),
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        **fields,
    )
    out["frame"] = base64.b64encode(encode_delivery_frame(message)).decode()
else:
    rows = []
    for index, frame in enumerate(request["frames"]):
        with tempfile.TemporaryDirectory() as root:
            service = InboxService(Path(root) / "inbox.sqlite3", MemoryDeliveryTransport())
            message = decode_delivery_frame(base64.b64decode(frame))
            result = service.receive(message)
            (stored,) = service.pending_messages(message.recipient)
            history = conversation_messages(
                service._db,
                participant=message.recipient,
                conversation_id=message.conversation_id,
            )
            row = _MessageQueriesMixin._read_body_row(
                None, stored, pending=True, fetched=False
            )
            rows.append({
                "acknowledged": result.acknowledged,
                "payload": base64.b64encode(stored.payload).decode(),
                "history": [base64.b64encode(item.payload).decode() for item in history],
                "readBody": row,
            })
            service.close()
    out["rows"] = rows
json.dump(out, sys.stdout)
"""

def _old_python_available() -> bool:
    try:
        return OLD_PYTHON.is_file() and os.access(OLD_PYTHON, os.X_OK)
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _old_python_available(),
    reason=f"the {OLD_VERSION} wheel interpreter is unavailable: {OLD_PYTHON}",
)


def _old(tmp_path: Path, request: dict[str, object]) -> dict[str, object]:
    home = tmp_path / "old-home"
    tmp = tmp_path / "old-tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "HYPRIAL_HOME": str(home / ".hyprial"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    completed = subprocess.run(
        [str(OLD_PYTHON), "-I", "-c", _OLD_SIDE],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    answer = json.loads(completed.stdout)
    assert str(answer["version"]).startswith(OLD_VERSION)
    return answer


def _new_message(**overrides: object) -> InboxMessage:
    fields = {**FIELDS, **overrides}
    return InboxMessage(
        payload=PAYLOAD, lifecycle=DeliveryLifecycle.DURABLE_SERVICE, **fields  # type: ignore[arg-type]
    )


class _Capture(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[bytes] = []

    def deliver(self, message: InboxMessage) -> bool:
        self.frames.append(encode_delivery_frame(message))
        return True


def _stamped_frame(message: InboxMessage) -> bytes:
    capture = _Capture()
    assert EnvelopeStampingTransport(capture).deliver(message)
    (frame,) = capture.frames
    return frame


def test_a_0553_node_reads_a_stamped_frame_like_an_unstamped_one(
    tmp_path: Path,
) -> None:
    message = _new_message().with_envelope(thread_id=THREAD)
    stamped = _stamped_frame(message)
    legacy = encode_delivery_frame(message)
    assert set(json.loads(stamped)) == set(json.loads(legacy))
    answer = _old(
        tmp_path,
        {
            "mode": "read",
            "frames": [base64.b64encode(frame).decode() for frame in (stamped, legacy)],
        },
    )
    new_row, old_row = answer["rows"]  # type: ignore[misc]
    assert new_row["acknowledged"] and old_row["acknowledged"]
    assert new_row["readBody"] == old_row["readBody"]
    assert new_row["readBody"]["message"] == "héllo from new"
    assert base64.b64decode(old_row["payload"]) == PAYLOAD
    old_stored = base64.b64decode(new_row["payload"])
    assert old_stored == decode_delivery_frame(stamped).payload
    assert json.loads(old_stored)["envelope"] == {
        "class": "interaction",
        "threadId": THREAD,
        "v": 1,
    }
    assert new_row["history"] == [new_row["payload"]]


def test_this_node_reads_a_0553_frame_through_local_inference(tmp_path: Path) -> None:
    rows = []
    for message_id in ("m-cross-old", "workflow-00000000-0000-4000-8000-0000000000aa"):
        answer = _old(
            tmp_path,
            {
                "mode": "encode",
                "fields": {**FIELDS, "message_id": message_id},
                "payload": base64.b64encode(PAYLOAD).decode(),
            },
        )
        frame = base64.b64decode(str(answer["frame"]))
        message = decode_delivery_frame(frame)
        service = InboxService(
            tmp_path / f"{message_id}.sqlite3", MemoryDeliveryTransport()
        )
        try:
            assert service.receive(message).acknowledged
            (stored,) = service.pending_messages(RECIPIENT)
        finally:
            service.close()
        assert stored.payload == PAYLOAD
        assert (stored.thread_id, stored.conversation_cell_id, stored.message_class) == (
            None,
            None,
            None,
        )
        rows.append(message_class_of(stored))
        assert message_class_of(stored) is derive_message_class(stored)
    assert rows == [MessageClass.INTERACTION, MessageClass.SYSTEM]
