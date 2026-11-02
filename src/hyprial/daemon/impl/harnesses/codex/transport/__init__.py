"""Codex app-server transport: one socket, responses by id, notifications out.

Decision record (E2, design §5): the interactive carrier must observe native
turn boundaries that other app-server clients start, so the connection may not
discard frames while it waits for a response. A single reader thread owns the
socket; requests wait on their id, server requests are still answered
fail-closed, and notifications go to registered listeners.

Kept separate from `app_server.py` so the app-server lifecycle and the wire
protocol stay independently reviewable and under the file-size gate.
"""

from .connection import CodexRpcConnection, open_app_server_connection

__all__ = ["CodexRpcConnection", "open_app_server_connection"]
