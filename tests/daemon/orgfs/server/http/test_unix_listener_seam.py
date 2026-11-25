"""The orgfs web server's unix listener base comes from the local IPC seam.

``unix:`` listening is a POSIX-only capability: the seam lends the
``socketserver`` base, and ``_prepare_unix_socket`` keeps owning the parent
directory's permission checks. The recording wrapper proves ``build_server``
takes the base from the seam entry point instead of naming the stdlib unix
server class itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.webserver import (
    WebServiceConfig,
    build_server,
)
from hyprial.daemon.impl.orgfs.webserver import server as server_module

from .support import _write_self_signed_cert
from .support import sock_dir as sock_dir


def test_the_unix_server_base_comes_from_the_seam(
    tmp_path: Path,
    sock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cert, key = _write_self_signed_cert(tmp_path / "certs")
    endpoint = sock_dir / "seam" / "web.sock"
    config = WebServiceConfig(
        host="fs.test.example",
        listen=f"unix:{endpoint}",
        cert=cert,
        key=key,
    )
    calls: list[bool] = []
    real = server_module.local_ipc_stream_server_base

    def _recording():
        calls.append(True)
        return real()

    monkeypatch.setattr(
        server_module, "local_ipc_stream_server_base", _recording
    )
    server = build_server(config, resolver=None)
    try:
        assert calls == [True]
        assert endpoint.exists()
    finally:
        server.server_close()
