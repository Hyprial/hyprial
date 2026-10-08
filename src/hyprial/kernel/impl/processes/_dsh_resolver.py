"""Minimal spawn entry point for cancellable DSH hostname resolution.

Keep this outside ``hyprial.harnesses``: importing that package also imports the
daemon and every harness SDK. A fresh resolver would spend the HTTP probe's
budget importing unrelated runtimes before it even called getaddrinfo.
"""

from __future__ import annotations

import socket
from multiprocessing.connection import Connection


def resolve_hostname(sender: Connection, host: str, port: int) -> None:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        message: tuple[object, ...] = (True, addresses)
    except BaseException as error:  # noqa: BLE001 - serialized to the parent
        message = (False, type(error).__name__, str(error))
    try:
        sender.send(message)
    except (BrokenPipeError, EOFError, OSError):
        pass  # Parent cancelled and closed its end of the resolver boundary.
    finally:
        sender.close()
