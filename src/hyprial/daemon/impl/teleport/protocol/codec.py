"""Encode / decode prepare, receive, commit and abort requests and replies.

Decisions:

- Built on the slice 2 frame codec: canonical JSON header, empty body.  A
  request or reply whose ``op`` differs from the decoder's op, or whose body
  is not empty, is ``TELEPORT_BAD_FRAME``.
- An ``ok:false`` reply raises ``TeleportError`` with the remote code and
  message instead of returning a message.
- Encoders write every field, defaults included; only ``AbortRequest.reason``
  is omitted when ``None``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from hyprial.daemon.impl.teleport.errors import TELEPORT_VERSION, TeleportError
from hyprial.daemon.impl.teleport.frames import (
    Frame,
    decode_frame,
    decode_reply,
    encode_frame,
)
from hyprial.daemon.impl.teleport.protocol import fields as f
from hyprial.daemon.impl.teleport.protocol.messages import (
    DEFAULT_STRICT_TIMEOUT_SECONDS,
    AbortReply,
    AbortRequest,
    CommitReply,
    CommitRequest,
    PrepareReply,
    PrepareRequest,
    ReceiveReply,
    ReceiveRequest,
    RoutineOutcome,
    RoutinePayload,
    RoutineRestore,
)


def _head(op: str, operation_id: str) -> dict[str, Any]:
    return {"v": TELEPORT_VERSION, "op": op, "operationId": operation_id}


def _ok_head(op: str, operation_id: str) -> dict[str, Any]:
    return {**_head(op, operation_id), "ok": True}


def _request(op: str, data: bytes) -> Frame:
    frame = decode_frame(data)
    if frame.header["op"] != op:
        raise f.bad(f"not a {op} frame")
    if frame.body:
        raise f.bad(f"{op} frame must have an empty body")
    return frame


def _reply(op: str, data: bytes) -> Frame:
    frame = decode_frame(data)
    if frame.header["op"] != op:
        raise f.bad(f"not a {op} frame")
    if frame.body:
        raise f.bad(f"{op} frame must have an empty body")
    reply = decode_reply(data)
    if reply.header["ok"] is False:
        error = reply.header["error"]
        raise TeleportError(error["code"], error["message"])
    return reply


def _items(
    header: Mapping[str, Any], name: str, build: Callable[[Mapping[str, Any]], Any]
) -> tuple[Any, ...]:
    entries = f.sequence(f.need(header, name), name)
    return tuple(build(f.mapping(entry, f"{name} item")) for entry in entries)


def encode_prepare_request(message: PrepareRequest) -> bytes:
    return encode_frame(
        {
            **_head("prepare", message.operation_id),
            "sourceNode": message.source_node,
            "targetNode": message.target_node,
            "owner": message.owner,
            "agent": message.agent,
            "sourceAgentUri": message.source_agent_uri,
            "harness": message.harness,
            "routines": list(message.routines),
            "dryRun": message.dry_run,
        }
    )


def decode_prepare_request(data: bytes) -> PrepareRequest:
    header = _request("prepare", data).header
    dry_run = header["dryRun"] if "dryRun" in header else False
    return PrepareRequest(
        operation_id=header["operationId"],
        source_node=f.text(f.need(header, "sourceNode"), "sourceNode"),
        target_node=f.text(f.need(header, "targetNode"), "targetNode"),
        owner=f.text(f.need(header, "owner"), "owner"),
        agent=f.text(f.need(header, "agent"), "agent"),
        source_agent_uri=f.text(f.need(header, "sourceAgentUri"), "sourceAgentUri"),
        harness=f.text(f.need(header, "harness"), "harness"),
        routines=f.texts(f.need(header, "routines"), "routines", allow_empty_items=False),
        dry_run=f.boolean(dry_run, "dryRun"),
    )


def encode_prepare_reply(message: PrepareReply) -> bytes:
    return encode_frame(
        {
            **_ok_head("prepare", message.operation_id),
            "targetNode": message.target_node,
            "recipientPublicKey": f.encode_public_key(message.recipient_public_key),
            "dryRun": message.dry_run,
        }
    )


def decode_prepare_reply(data: bytes) -> PrepareReply:
    header = _reply("prepare", data).header
    return PrepareReply(
        operation_id=header["operationId"],
        target_node=f.text(f.need(header, "targetNode"), "targetNode"),
        recipient_public_key=f.decode_public_key(
            f.need(header, "recipientPublicKey"), "recipientPublicKey"
        ),
        dry_run=f.boolean(f.need(header, "dryRun"), "dryRun"),
    )


def _routine_payload(entry: Mapping[str, Any]) -> RoutinePayload:
    return RoutinePayload(
        name=f.text(f.need(entry, "name"), "routine name"),
        yaml=f.plain_text(f.need(entry, "yaml"), "routine yaml"),
        enabled=f.boolean(f.need(entry, "enabled"), "routine enabled"),
    )


def encode_receive_request(message: ReceiveRequest) -> bytes:
    return encode_frame(
        {
            **_head("receive", message.operation_id),
            "manifestDigest": message.manifest_digest,
            "streamSha256": message.stream_sha256,
            "harness": message.harness,
            "args": list(message.args),
            "sessionRef": message.session_ref,
            "strictTimeoutSeconds": message.strict_timeout_seconds,
            "routines": [
                {"name": item.name, "yaml": item.yaml, "enabled": item.enabled}
                for item in message.routines
            ],
        }
    )


def decode_receive_request(data: bytes) -> ReceiveRequest:
    header = _request("receive", data).header
    args = header["args"] if "args" in header else []
    timeout = (
        header["strictTimeoutSeconds"]
        if "strictTimeoutSeconds" in header
        else DEFAULT_STRICT_TIMEOUT_SECONDS
    )
    return ReceiveRequest(
        operation_id=header["operationId"],
        manifest_digest=f.digest(f.need(header, "manifestDigest"), "manifestDigest"),
        stream_sha256=f.digest(f.need(header, "streamSha256"), "streamSha256"),
        harness=f.text(f.need(header, "harness"), "harness"),
        session_ref=f.optional_text(f.need(header, "sessionRef"), "sessionRef"),
        routines=_items(header, "routines", _routine_payload),
        args=f.texts(args, "args", allow_empty_items=True),
        strict_timeout_seconds=f.positive_seconds(timeout, "strictTimeoutSeconds"),
    )


def _routine_restore(entry: Mapping[str, Any]) -> RoutineRestore:
    return RoutineRestore(
        name=f.text(f.need(entry, "name"), "routine name"),
        restore_enabled=f.boolean(f.need(entry, "restoreEnabled"), "restoreEnabled"),
    )


def encode_receive_reply(message: ReceiveReply) -> bytes:
    return encode_frame(
        {
            **_ok_head("receive", message.operation_id),
            "agentUri": message.agent_uri,
            "sessionRef": message.session_ref,
            "routines": [
                {"name": item.name, "restoreEnabled": item.restore_enabled}
                for item in message.routines
            ],
        }
    )


def decode_receive_reply(data: bytes) -> ReceiveReply:
    header = _reply("receive", data).header
    return ReceiveReply(
        operation_id=header["operationId"],
        agent_uri=f.text(f.need(header, "agentUri"), "agentUri"),
        session_ref=f.optional_text(f.need(header, "sessionRef"), "sessionRef"),
        routines=_items(header, "routines", _routine_restore),
    )


def encode_commit_request(message: CommitRequest) -> bytes:
    return encode_frame(_head("commit", message.operation_id))


def decode_commit_request(data: bytes) -> CommitRequest:
    return CommitRequest(operation_id=_request("commit", data).header["operationId"])


def _routine_outcome(entry: Mapping[str, Any]) -> RoutineOutcome:
    return RoutineOutcome(
        name=f.text(f.need(entry, "name"), "routine name"),
        enabled=f.boolean(f.need(entry, "enabled"), "routine enabled"),
        resumed=f.boolean(f.need(entry, "resumed"), "routine resumed"),
    )


def encode_commit_reply(message: CommitReply) -> bytes:
    return encode_frame(
        {
            **_ok_head("commit", message.operation_id),
            "routines": [
                {"name": item.name, "enabled": item.enabled, "resumed": item.resumed}
                for item in message.routines
            ],
        }
    )


def decode_commit_reply(data: bytes) -> CommitReply:
    header = _reply("commit", data).header
    return CommitReply(
        operation_id=header["operationId"],
        routines=_items(header, "routines", _routine_outcome),
    )


def encode_abort_request(message: AbortRequest) -> bytes:
    header = _head("abort", message.operation_id)
    if message.reason is not None:
        header["reason"] = message.reason
    return encode_frame(header)


def decode_abort_request(data: bytes) -> AbortRequest:
    header = _request("abort", data).header
    reason = f.plain_text(header["reason"], "reason") if "reason" in header else None
    return AbortRequest(operation_id=header["operationId"], reason=reason)


def encode_abort_reply(message: AbortReply) -> bytes:
    return encode_frame(_ok_head("abort", message.operation_id))


def decode_abort_reply(data: bytes) -> AbortReply:
    return AbortReply(operation_id=_reply("abort", data).header["operationId"])
