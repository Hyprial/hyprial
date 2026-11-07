"""Shared isolation helpers for Zenoh capability spikes."""

from __future__ import annotations

from contextlib import contextmanager
import json
import socket
from typing import Iterator

import zenoh


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def isolated_config(*, listen: list[str], connect: list[str]) -> zenoh.Config:
    """Disable multicast/gossip and use only explicit loopback TCP endpoints."""
    return zenoh.Config.from_json5(json.dumps({
        "mode": "peer",
        "listen": {"endpoints": listen},
        "connect": {"endpoints": connect},
        "scouting": {
            "multicast": {"enabled": False},
            "gossip": {"enabled": False},
        },
        "transport": {"shared_memory": {"enabled": False}},
    }))


@contextmanager
def connected_loopback_sessions() -> Iterator[tuple[zenoh.Session, zenoh.Session]]:
    port = reserve_loopback_port()
    endpoint = f"tcp/127.0.0.1:{port}"
    server = zenoh.open(isolated_config(listen=[endpoint], connect=[]))
    try:
        client = zenoh.open(isolated_config(listen=[], connect=[endpoint]))
        try:
            yield server, client
        finally:
            client.close()
    finally:
        server.close()


def emit(result: dict[str, object]) -> None:
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
