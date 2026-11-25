"""Bounded bootstrap client over the daemon's already authenticated raw socket."""

import socket
import struct
import time

from ..runtime.schema import (
    BOOTSTRAP_VERSION, decode, encode, fail, parse_descriptor,
)

PREFIX = b"HCA1\n"
LIMIT = 65536
TIMEOUT = 10


def bootstrap(dial, target, tailnet_id):
    deadline = time.monotonic() + TIMEOUT
    connection = dial(target)
    if not isinstance(connection, socket.socket):
        fail("TAILNET_BOOTSTRAP_INVALID", "bootstrap")
    try:
        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("bootstrap deadline")
            connection.settimeout(seconds)

        def receive(size):
            chunks = bytearray()
            while len(chunks) < size:
                remaining()
                chunk = connection.recv(size - len(chunks))
                if not chunk:
                    raise ValueError("incomplete bootstrap frame")
                chunks.extend(chunk)
            return bytes(chunks)

        request = encode({"version": BOOTSTRAP_VERSION, "tailnetId": tailnet_id})
        if len(request) > LIMIT:
            raise ValueError("bootstrap request too large")
        remaining()
        connection.sendall(PREFIX + struct.pack("!I", len(request)) + request)
        if receive(len(PREFIX)) != PREFIX:
            raise ValueError("invalid bootstrap prefix")
        size = struct.unpack("!I", receive(4))[0]
        if size == 0 or size > LIMIT:
            raise ValueError("invalid bootstrap length")
        result = parse_descriptor(decode(receive(size)))
        remaining()
        if connection.recv(1):
            raise ValueError("trailing bootstrap bytes")
        if result.tailnet_id != tailnet_id or result.target != target:
            fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT", "bootstrap")
        return result
    except (OSError, ValueError, UnicodeError, RecursionError):
        fail("TAILNET_BOOTSTRAP_INVALID", "bootstrap")
    finally:
        connection.close()
