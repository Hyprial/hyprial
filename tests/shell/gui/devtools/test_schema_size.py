# Decision record (developer tools, PR 4b-C, 2026-11-17):
# - The API docs page shows the schema the session host fetches from the real
#   request_http server, and the host refuses an upstream body above
#   MAX_RESPONSE_BYTES. The document grows with every daemon method, so the
#   size is measured here on the bytes a real RequestHttpServer serves with
#   every HTTP-exposed method of the local catalog available, not on the
#   repository snapshot (which is a different, smaller document).
# - The assertion keeps a quarter of the limit free. When it fails the docs
#   page still works; the failure is the notice to decide, before the limit is
#   reached, between a larger schema limit for that one route and a smaller
#   served document. Do not raise HEADROOM to make it pass.
"""The real request schema fits the session host's upstream limit with room to spare."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hyprial import __version__
from hyprial.daemon import daemon_catalog
from hyprial.shell.impl.gui.host.envelope import MAX_RESPONSE_BYTES
from hyprial.shell.impl.gui.host.web.devtools import SCHEMA_PATH, DevTools
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.gui.host.support import FakeUpstream, Gateway, run_gateway

OWNER = "owner"
HEADROOM = 0.25
DOCS = {"swagger-ui-bundle.js": b"window.SwaggerUIBundle=function(){};", "swagger-ui.css": b".swagger-ui{}"}


def _request_once(method: str, params: dict[str, Any], **_ignored: Any) -> dict[str, Any]:
    if method == "ping":
        return {
            "version": __version__,
            "nodeId": "schema-size-node",
            "owner": OWNER,
            "epoch": "epoch-1",
            "phase": "serving",
            "restorePending": False,
        }
    entries = []
    for entry in daemon_catalog()["methods"]:
        available = bool(entry["httpExposed"])
        entries.append(
            {
                **entry,
                "declared": True,
                "state": "available" if available else "disabled",
                "callable": available,
                "reason": None if available else "SERVICE_NOT_READY",
            }
        )
    return {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "epoch": "epoch-1",
        "packageVersion": __version__,
        "methods": entries,
    }


def _served_schema(tmp_path: Path) -> bytes:
    config = RequestHttpConfig(
        socket_path=tmp_path / "daemon.sock",
        owner=OWNER,
        actor=f"user:{OWNER}",
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=tmp_path / "runtime",
        source=tmp_path / "config.json",
    )
    server = RequestHttpServer(config, request_once=_request_once)
    server.start()
    try:
        endpoint = urlsplit(server.endpoint)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=30)
        try:
            connection.request("GET", "/openapi.json", headers={"Authorization": f"Bearer {server.token}"})
            response = connection.getresponse()
            body = response.read()
            assert response.status == 200
        finally:
            connection.close()
    finally:
        server.shutdown()
    return body


def test_the_real_request_schema_leaves_a_quarter_of_the_host_limit_free(tmp_path: Path) -> None:
    body = _served_schema(tmp_path)
    document = json.loads(body)
    exposed = [entry["name"] for entry in daemon_catalog()["methods"] if entry["httpExposed"]]
    assert len(exposed) > 100
    assert all(f"/daemon/request/{name}" in document["paths"] for name in exposed)
    allowed = int(MAX_RESPONSE_BYTES * (1 - HEADROOM))
    assert len(body) <= allowed, (
        f"the served request schema is {len(body)} bytes; the session host refuses more than "
        f"{MAX_RESPONSE_BYTES} and this test keeps {HEADROOM:.0%} free ({allowed}). "
        "Decide on a schema limit or a smaller document before the API docs page breaks."
    )

    upstream = FakeUpstream(openapi=document)

    async def scenario(gateway: Gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call(SCHEMA_PATH, headers=opened.headers(), omit_origin=True)
        assert reply.status == 200
        assert len(reply.json["paths"]) == len(exposed) + 1

    run_gateway(scenario, upstream=upstream, dev_tools=DevTools(docs=DOCS))
