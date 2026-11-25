"""Slice 2 query-handler seam between the transport and ``StagingReceiver``.

Decisions pinned here:

- ``handle_teleport_query(receiver, selector, payload, *, local_node,
  key_space=None) -> list[tuple[str, bytes]]`` is the pure function a
  transport query handler calls.  It answers ``chunk`` and ``abort`` with
  exactly one reply keyed by the concrete queried key (selector parameters
  dropped).  Business failures are ``ok:false`` reply frames, never raised.
- When a frame cannot be trusted, the reply's ``op``/``operationId`` come
  from the key.  Header ``op``/``operationId`` must equal the key's.
- A key for another node is answered with ``TELEPORT_WRONG_TARGET``
  (declarations only cover the local node, so this is defensive).
- ``bind_staging_receiver(session, receiver, *, local_node, key_space=None)``
  declares the handler(s) and returns an object with ``close()``.
- Outside the package only the daemon composition layer
  (``daemon/impl/application``, ``daemon/impl/composition``) and the
  source-side request catalog entry (``request_catalog/teleport``) import
  it; the only public method is ``teleport.run`` (slice 5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.transport import KeySpace

from .support import chunk_requests, sha256_hex, stream_bytes, teleport

SMALL = 512
NODE = "node-b"


def _receiver(tmp_path: Path):
    module = teleport()
    return module.StagingReceiver(module.staging_root(tmp_path / "home"))


def _key(op: str, operation_id: str = "op-1", node: str = NODE) -> str:
    return KeySpace().agent_teleport(node, op, operation_id)


def _single_reply(replies, key: str):
    replies = list(replies)
    assert len(replies) == 1
    reply_key, payload = replies[0]
    assert reply_key == key
    return teleport().decode_reply(payload).header


def _handle(receiver, selector: str, payload: bytes | None):
    return teleport().handle_teleport_query(
        receiver, selector, payload, local_node=NODE
    )


def test_chunk_query_is_acknowledged_with_persisted(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    data = stream_bytes(SMALL * 2)
    request = chunk_requests(data, "op-1", chunk_bytes=SMALL)[0]
    key = _key("chunk")
    header = _single_reply(_handle(receiver, key, module.encode_chunk_request(request)), key)
    assert header == {
        "v": 1,
        "op": "chunk",
        "operationId": "op-1",
        "ok": True,
        "persisted": SMALL,
    }


def test_reply_key_drops_selector_parameters(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    request = chunk_requests(stream_bytes(SMALL), "op-1", chunk_bytes=SMALL)[0]
    key = _key("chunk")
    replies = _handle(receiver, key + "?_x=1", module.encode_chunk_request(request))
    assert _single_reply(replies, key)["ok"] is True


def test_receiver_errors_become_error_replies(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    request = chunk_requests(stream_bytes(SMALL), "op-1", chunk_bytes=SMALL)[0]
    key = _key("chunk")
    header = _single_reply(_handle(receiver, key, module.encode_chunk_request(request)), key)
    assert header["ok"] is False
    assert header["op"] == "chunk"
    assert header["operationId"] == "op-1"
    assert header["error"]["code"] == "TELEPORT_UNKNOWN_OPERATION"
    assert isinstance(header["error"]["message"], str)


@pytest.mark.parametrize("payload", [None, b"", b"\x00\x00\x00\x09{}"])
def test_unreadable_payload_is_bad_frame_reply(tmp_path: Path, payload) -> None:
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    key = _key("chunk")
    header = _single_reply(_handle(receiver, key, payload), key)
    assert header["ok"] is False
    assert header["operationId"] == "op-1"
    assert header["error"]["code"] == "TELEPORT_BAD_FRAME"
    assert receiver.persisted("op-1") == 0


def test_header_must_match_key(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    receiver.prepare("op-2")
    request = chunk_requests(stream_bytes(SMALL), "op-2", chunk_bytes=SMALL)[0]
    key = _key("chunk", "op-1")
    header = _single_reply(_handle(receiver, key, module.encode_chunk_request(request)), key)
    assert header["error"]["code"] == "TELEPORT_BAD_FRAME"
    assert receiver.persisted("op-1") == 0
    assert receiver.persisted("op-2") == 0
    abort_frame = module.encode_frame({"v": 1, "op": "abort", "operationId": "op-1"})
    header = _single_reply(_handle(receiver, key, abort_frame), key)
    assert header["error"]["code"] == "TELEPORT_BAD_FRAME"
    assert (receiver.root / "op-1").is_dir()


def test_key_for_other_node_is_wrong_target(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    request = chunk_requests(stream_bytes(SMALL), "op-1", chunk_bytes=SMALL)[0]
    key = _key("chunk", node="node-elsewhere")
    header = _single_reply(_handle(receiver, key, module.encode_chunk_request(request)), key)
    assert header["error"]["code"] == "TELEPORT_WRONG_TARGET"
    assert receiver.persisted("op-1") == 0


def test_abort_query_removes_staging_and_is_idempotent(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    directory = receiver.prepare("op-1")
    key = _key("abort")
    frame = module.encode_frame({"v": 1, "op": "abort", "operationId": "op-1"})
    first = _single_reply(_handle(receiver, key, frame), key)
    assert first == {"v": 1, "op": "abort", "operationId": "op-1", "ok": True}
    assert not directory.exists()
    second = _single_reply(_handle(receiver, key, frame), key)
    assert second["ok"] is True


def test_full_stream_through_handler_finalizes(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    data = stream_bytes(SMALL * 3 + 1)
    key = _key("chunk")
    for request in chunk_requests(data, "op-1", chunk_bytes=SMALL):
        header = _single_reply(_handle(receiver, key, module.encode_chunk_request(request)), key)
        assert header["persisted"] == request.offset + request.size
    assert receiver.finalize("op-1", stream_sha256=sha256_hex(data)).read_bytes() == data


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "hyprial").is_dir():
            return parent
    raise AssertionError("repository root not found")


COMPOSITION_LAYERS = (
    ("src", "hyprial", "daemon", "impl", "application"),
    ("src", "hyprial", "daemon", "impl", "composition"),
)


def test_only_daemon_composition_imports_the_package() -> None:
    root = _repo_root()
    package_dir = root / "src" / "hyprial" / "daemon" / "impl" / "teleport"
    assert package_dir.is_dir(), "slice 2 library package is missing"
    allowed = [root.joinpath(*parts) for parts in COMPOSITION_LAYERS]
    allowed.append(root / "src" / "hyprial" / "daemon" / "impl" / "request_catalog" / "teleport")
    offenders = [
        str(path.relative_to(root))
        for path in (root / "src" / "hyprial").rglob("*.py")
        if package_dir not in path.parents
        and not any(layer in path.parents for layer in allowed)
        and ("impl.teleport" in path.read_text(encoding="utf-8")
             or "impl import teleport" in path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_only_public_teleport_method_is_teleport_run() -> None:
    root = _repo_root()
    openapi = json.loads(
        (root / "contract" / "daemon-request" / "openapi.json").read_text(encoding="utf-8")
    )
    names = {entry["name"] for entry in openapi["x-hyprial-methods"]}
    assert {name for name in names if "teleport" in name.lower()} == {"teleport.run"}
