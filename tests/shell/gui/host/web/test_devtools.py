# Decision record (developer tools on the session host, PR 4b-H, 2026-11-16):
# - The docs paths are judged twice, with and without the DevTools record, over
#   an asset tree and over a live upstream. Without the record a docs asset is
#   404 whatever its name looks like. /docs is then the single-page fallback of
#   a tree and 404 in front of a live upstream (owner decision Q-2); the live
#   upstream is a handler that fails the test when the host contacts it for a
#   path the host must answer itself.
# - No response of these paths may contain the upstream token of the run or the
#   launch grant; the page and the script are compared against both.
"""The API docs page and its files, and the docs paths of a host without developer tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from hyprial.shell.impl.gui.host.web.assets import AssetTree
from hyprial.shell.impl.gui.host.web.devtools import (
    ATTACH_PATH,
    DOCS_CSP,
    SCHEMA_PATH,
    VIEW_STORAGE_KEY,
    DevTools,
    DocsAssetsError,
    collect_docs,
    is_tool_path,
    read_docs_directory,
)
from tests.shell.gui.host.support import GRANT, UPSTREAM_TOKEN, host_config, run_gateway
from tests.shell.gui.host.web.test_live import Chunks, seen_nothing, vite

INDEX = "<!doctype html><title>Hyprial</title>"
BUNDLE = b"window.SwaggerUIBundle=function(){};"
STYLE = b".swagger-ui{color:black}"
DOCS = {"swagger-ui-bundle.js": BUNDLE, "swagger-ui.css": STYLE}
DOCS_PATHS = ("/docs", "/docs-assets/hyprial-docs.js", "/docs-assets/swagger-ui-bundle.js", "/docs-assets/swagger-ui.css")


def tree(tmp_path: Path, *, dev_tools: bool) -> AssetTree:
    root = tmp_path / "gui"
    root.mkdir(exist_ok=True)
    (root / "index.html").write_text(INDEX)
    return AssetTree.open(root, dev_tools=dev_tools)


def get(tmp_path: Path, path: str, dev_tools: DevTools | None, *, driver: str = "daemon", **options: Any) -> Any:
    async def scenario(gateway: Any) -> Any:
        return await gateway.call(path, **options)

    return run_gateway(
        scenario,
        config=host_config(driver=driver),
        assets=tree(tmp_path, dev_tools=dev_tools is not None),
        dev_tools=dev_tools,
    )


def test_the_docs_page_is_a_static_shell_under_a_same_origin_policy(tmp_path: Path) -> None:
    reply = get(tmp_path, "/docs", DevTools(docs=DOCS))
    assert reply.status == 200
    assert reply.headers["content-type"] == "text/html; charset=utf-8"
    assert reply.headers["content-security-policy"] == DOCS_CSP
    assert "script-src 'self'" in DOCS_CSP and "connect-src 'self'" in DOCS_CSP
    assert "frame-ancestors 'none'" in DOCS_CSP and "default-src 'none'" in DOCS_CSP
    assert reply.headers["x-frame-options"] == "DENY"
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["content-length"] == str(len(reply.raw.encode()))
    for name in ("hyprial-docs.js", "swagger-ui-bundle.js", "swagger-ui.css"):
        assert f"/docs-assets/{name}" in reply.raw
    assert "<script>" not in reply.raw and "onload" not in reply.raw
    assert "Hyprial</title>" not in reply.raw


def test_the_docs_script_attaches_and_never_names_a_credential(tmp_path: Path) -> None:
    reply = get(tmp_path, "/docs-assets/hyprial-docs.js", DevTools(docs=DOCS))
    assert reply.status == 200
    assert reply.headers["content-type"] == "text/javascript; charset=utf-8"
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["x-content-type-options"] == "nosniff"
    for needle in (ATTACH_PATH, SCHEMA_PATH, VIEW_STORAGE_KEY, "x-hyprial-view", "x-hyprial-generation", "x-hyprial-csrf"):
        assert needle in reply.raw, needle
    assert "delete headers.Authorization" in reply.raw
    assert "Bearer" not in reply.raw and "token" not in reply.raw.lower()
    assert "localStorage" not in reply.raw and "document.cookie" not in reply.raw
    assert "location.hash" not in reply.raw and "location.search" not in reply.raw


def test_the_swagger_files_are_the_bytes_read_at_start(tmp_path: Path) -> None:
    bundle = get(tmp_path, "/docs-assets/swagger-ui-bundle.js", DevTools(docs=DOCS))
    assert bundle.status == 200 and bundle.raw.encode() == BUNDLE
    assert bundle.headers["content-type"] == "text/javascript; charset=utf-8"
    style = get(tmp_path, "/docs-assets/swagger-ui.css", DevTools(docs=DOCS))
    assert style.status == 200 and style.raw.encode() == STYLE
    assert style.headers["content-type"] == "text/css; charset=utf-8"
    assert style.headers["cache-control"] == "no-store"
    head = get(tmp_path, "/docs-assets/swagger-ui.css", DevTools(docs=DOCS), method="HEAD")
    assert head.status == 200 and head.raw == "" and head.headers["content-length"] == str(len(STYLE))


@pytest.mark.parametrize(
    "path",
    [
        "/docs-assets/",
        "/docs-assets/unknown.js",
        "/docs-assets/unknown",
        "/docs-assets/LICENSE",
        "/docs-assets/swagger-ui.css/x",
        "/docs-assets/../docs-assets/nope.css",
        "/docs-assets/%2e%2e/index.html.map",
    ],
)
def test_an_unknown_docs_asset_is_a_host_404(tmp_path: Path, path: str) -> None:
    reply = get(tmp_path, path, DevTools(docs=DOCS))
    assert reply.status == 404 and reply.json["code"] == "NOT_FOUND", path


@pytest.mark.parametrize(
    ("dev_tools", "driver"),
    [(None, "daemon"), (DevTools(docs=None), "daemon"), (DevTools(docs=DOCS), "contract-mock")],
)
def test_without_the_docs_capability_the_docs_paths_are_not_the_page(
    tmp_path: Path, dev_tools: DevTools | None, driver: str
) -> None:
    page = get(tmp_path, "/docs", dev_tools, driver=driver)
    assert page.status == 200 and page.raw == INDEX
    assert "content-security-policy" not in page.headers
    for path in (*DOCS_PATHS[1:], "/docs-assets/unknown", "/docs-assets/"):
        reply = get(tmp_path, path, dev_tools, driver=driver)
        assert reply.status == 404 and reply.json["code"] == "NOT_FOUND", path
        assert BUNDLE.decode() not in reply.raw and STYLE.decode() not in reply.raw


@pytest.mark.parametrize("path", ["/docs/", "/docs/index.html", "/Docs", "/docs.html"])
def test_only_the_exact_docs_path_is_the_page(tmp_path: Path, path: str) -> None:
    reply = get(tmp_path, path, DevTools(docs=DOCS))
    assert "content-security-policy" not in reply.headers
    assert "swagger-ui" not in reply.raw


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_the_docs_paths_keep_the_host_origin_and_method_rules(tmp_path: Path, path: str) -> None:
    dev_tools = DevTools(docs=DOCS)
    foreign_host = get(tmp_path, path, dev_tools, host="example.invalid:4173")
    assert (foreign_host.status, foreign_host.json["code"]) == (403, "HTTP_HOST_DENIED")
    foreign_origin = get(tmp_path, path, dev_tools, origin="http://127.0.0.1:9999")
    assert (foreign_origin.status, foreign_origin.json["code"]) == (403, "ORIGIN_REJECTED")
    cross_site = get(tmp_path, path, dev_tools, headers={"sec-fetch-site": "cross-site"})
    assert (cross_site.status, cross_site.json["code"]) == (403, "ORIGIN_REJECTED")
    posted = get(tmp_path, path, dev_tools, method="POST", body={})
    assert (posted.status, posted.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_no_docs_response_contains_a_secret_of_the_run(tmp_path: Path, path: str) -> None:
    reply = get(tmp_path, path, DevTools(docs=DOCS))
    assert reply.status == 200
    assert UPSTREAM_TOKEN not in reply.raw and GRANT not in reply.raw
    assert UPSTREAM_TOKEN not in str(dict(reply.headers)) and "set-cookie" not in reply.headers


def live_get(path: str, handler: Any, dev_tools: DevTools | None) -> Any:
    async def scenario(gateway: Any) -> Any:
        return await gateway.call(path)

    return run_gateway(scenario, live=vite(handler), dev_tools=dev_tools)


@pytest.mark.parametrize(
    "path",
    [
        "/docs",
        "/docs-assets/hyprial-docs.js",
        "/docs-assets/swagger-ui-bundle.js",
        "/docs-assets/unknown",
        "/dev-tools/tools-abc.js",
        "/dev-tools/docs/swagger-ui.css",
        "/DEV-TOOLS/tools-abc.js",
        "/dev%2Dtools/tools-abc.js",
        "/dev-tool%C5%BF/tools-abc.js",
        "/dev-tools",
    ],
)
def test_a_live_host_without_developer_tools_answers_the_gated_paths_itself(path: str) -> None:
    reply = live_get(path, seen_nothing, None)
    assert reply.status == 404 and reply.json["code"] == "NOT_FOUND", path


def test_a_live_host_never_relays_the_docs_path() -> None:
    def source(request: httpx.Request) -> httpx.Response:
        assert request.url.path in ("/docs/", "/dev-tools/tools-abc.js")
        return httpx.Response(200, stream=Chunks(b"vite"), headers={"content-type": "text/html"})

    for dev_tools in (None, DevTools(docs=None)):
        refused = live_get("/docs", seen_nothing, dev_tools)
        assert refused.status == 404 and refused.json["code"] == "NOT_FOUND"
    other = live_get("/docs/", source, None)
    assert other.status == 200 and other.raw == "vite"
    page = live_get("/docs", seen_nothing, DevTools(docs=DOCS))
    assert page.status == 200 and page.headers["content-security-policy"] == DOCS_CSP
    asset = live_get("/docs-assets/swagger-ui.css", seen_nothing, DevTools(docs=DOCS))
    assert asset.status == 200 and asset.raw.encode() == STYLE
    tool_source = live_get("/dev-tools/tools-abc.js", source, DevTools(docs=DOCS))
    assert tool_source.status == 200 and tool_source.raw == "vite"


def test_the_prefix_is_compared_case_folded_not_lower_cased() -> None:
    assert "dev-tool\u017f".lower() != "dev-tools"
    for spelling in ("dev-tools/x.js", "/DEV-TOOLS/x.js", "Dev-Tools", "dev-tool\u017f/x.js", "DEV-TOOL\u017f"):
        assert is_tool_path(spelling), spelling
    for spelling in ("dev-tools.js", "dev-toolss/x.js", "assets/dev-tools/x.js", "devtools/x.js", ""):
        assert not is_tool_path(spelling), spelling


def test_the_docs_script_drops_the_opener_before_anything_else(tmp_path: Path) -> None:
    script = get(tmp_path, "/docs-assets/hyprial-docs.js", DevTools(docs=DOCS)).raw
    assert script.count("window.opener") == 1
    assert script.index("window.opener = null") < script.index("sessionStorage") < script.index("fetch(")


def test_the_docs_files_are_all_or_nothing() -> None:
    assert collect_docs(DOCS.get) == DOCS
    assert collect_docs({"swagger-ui.css": STYLE}.get) is None
    assert collect_docs({}.get) is None


def test_an_explicit_docs_directory_is_read_once_or_refused(tmp_path: Path) -> None:
    directory = tmp_path / "swagger"
    with pytest.raises(DocsAssetsError):
        read_docs_directory(directory)
    directory.mkdir()
    (directory / "swagger-ui.css").write_bytes(STYLE)
    with pytest.raises(DocsAssetsError):
        read_docs_directory(directory)
    (directory / "swagger-ui-bundle.js").write_bytes(BUNDLE)
    (directory / "unrelated.js").write_text("never served")
    found = read_docs_directory(directory)
    assert found == DOCS
    (directory / "swagger-ui.css").write_bytes(b"changed after start")
    assert found["swagger-ui.css"] == STYLE
