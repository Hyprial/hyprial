"""Agent teleport slice 2: wire, chunk sender and target staging (library only).

Nothing here is registered into daemon startup, the request catalog, OpenAPI or
the CLI; later slices consume this package.  See ``contract/agent-teleport``.
"""

from hyprial.daemon.impl.teleport.errors import (
    TELEPORT_CHUNK_BYTES,
    TELEPORT_ERROR_CODES,
    TELEPORT_OPS,
    TELEPORT_VERSION,
    TeleportError,
)
from hyprial.daemon.impl.teleport.frames import (
    ChunkRequest,
    Frame,
    decode_chunk_request,
    decode_frame,
    decode_reply,
    encode_chunk_request,
    encode_error_reply,
    encode_frame,
    encode_reply,
)
from hyprial.daemon.impl.teleport.handler import (
    bind_staging_receiver,
    handle_teleport_query,
)
from hyprial.daemon.impl.teleport.keys import TeleportKey, parse_teleport_key
from hyprial.daemon.impl.teleport.sender import SendResult, send_stream
from hyprial.daemon.impl.teleport.staging import StagingReceiver, staging_root

__all__ = [
    "ChunkRequest",
    "Frame",
    "SendResult",
    "StagingReceiver",
    "TELEPORT_CHUNK_BYTES",
    "TELEPORT_ERROR_CODES",
    "TELEPORT_OPS",
    "TELEPORT_VERSION",
    "TeleportError",
    "TeleportKey",
    "bind_staging_receiver",
    "decode_chunk_request",
    "decode_frame",
    "decode_reply",
    "encode_chunk_request",
    "encode_error_reply",
    "encode_frame",
    "encode_reply",
    "handle_teleport_query",
    "parse_teleport_key",
    "send_stream",
    "staging_root",
]
