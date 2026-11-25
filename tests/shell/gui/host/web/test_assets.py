"""Static resources: verified or explicit root, SPA fallback, headers and the Host rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.shell.impl.gui.host.web.assets import AssetTree, AssetRootError
from hyprial.shell.impl.gui.install import resources
from tests.shell.gui.host.support import host_config, run_gateway

COMMIT = "a" * 40
REMOTE = "https://example.invalid/hyprial.git"


def build_root(tmp_path: Path, *, manifest: bool) -> Path:
    root = tmp_path / "gui"
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>Hyprial</title>")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (root / "assets" / "app-abc123.js").write_text("console.log('app');")
    (root / "assets" / "app-abc123.css").write_text("body{margin:0}")
    (root / "robots.txt").write_text("User-agent: *")
    if manifest:
        built = resources.build_manifest(
            root, package_version="0.0.0", source_commit=COMMIT, source_remote=REMOTE, gui_version="1"
        )
        resources.write_manifest(root, built)
    return root


def assets(tmp_path: Path, *, manifest: bool = True) -> AssetTree:
    return AssetTree.open(build_root(tmp_path, manifest=manifest))


def get(tmp_path: Path, path: str, *, method: str = "GET", manifest: bool = True, **options):
    holder = assets(tmp_path, manifest=manifest)

    async def scenario(gateway):
        return await gateway.call(path, method=method, **options)

    return run_gateway(scenario, assets=holder)


def test_the_index_is_served_at_the_root_without_caching(tmp_path: Path) -> None:
    reply = get(tmp_path, "/")
    assert reply.status == 200
    assert reply.raw == "<!doctype html><title>Hyprial</title>"
    assert reply.headers["content-type"] == "text/html; charset=utf-8"
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["content-length"] == str(len(reply.raw))


def test_hashed_assets_are_immutable_and_typed(tmp_path: Path) -> None:
    script = get(tmp_path, "/assets/app-abc123.js")
    assert script.status == 200 and script.raw == "console.log('app');"
    assert script.headers["content-type"] in ("text/javascript; charset=utf-8", "application/javascript; charset=utf-8")
    assert script.headers["cache-control"] == "public, max-age=31536000, immutable"
    style = get(tmp_path, "/assets/app-abc123.css")
    assert style.headers["content-type"] == "text/css; charset=utf-8"
    icon = get(tmp_path, "/favicon.svg")
    assert icon.headers["content-type"] == "image/svg+xml; charset=utf-8"
    assert icon.headers["cache-control"] == "no-store"


def test_an_unknown_route_without_an_extension_falls_back_to_the_index(tmp_path: Path) -> None:
    for path in ("/agents", "/agents/42", "/assets/", "/daemon"):
        reply = get(tmp_path, path)
        assert reply.status == 200 and "Hyprial" in reply.raw, path
        assert reply.headers["cache-control"] == "no-store"


def test_a_missing_path_with_an_extension_is_a_host_404(tmp_path: Path) -> None:
    for path in ("/assets/missing.js", "/nope.png", "/a/b.css"):
        reply = get(tmp_path, path)
        assert reply.status == 404 and reply.json["code"] == "NOT_FOUND" and reply.json["ok"] is False, path


def test_head_returns_the_headers_without_a_body(tmp_path: Path) -> None:
    reply = get(tmp_path, "/assets/app-abc123.js", method="HEAD")
    assert reply.status == 200 and reply.raw == ""
    assert reply.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert reply.headers["content-length"] == str(len("console.log('app');"))


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_static_paths_accept_only_get_and_head(tmp_path: Path, method: str) -> None:
    reply = get(tmp_path, "/", method=method, body={})
    assert (reply.status, reply.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")


def test_the_old_placeholder_business_prefix_is_just_another_missing_path(tmp_path: Path) -> None:
    reply = get(tmp_path, "/api/gui/v1/x.json")
    assert (reply.status, reply.json["code"]) == (404, "NOT_FOUND")
    spa = get(tmp_path, "/api/gui/v1/x")
    assert spa.status == 200 and "Hyprial" in spa.raw


def test_the_host_and_origin_rule_applies_to_static_paths(tmp_path: Path) -> None:
    wrong_host = get(tmp_path, "/", host="localhost:9999")
    assert (wrong_host.status, wrong_host.json["code"]) == (403, "HTTP_HOST_DENIED")
    wrong_origin = get(tmp_path, "/", origin="http://hostile.invalid")
    assert (wrong_origin.status, wrong_origin.json["code"]) == (403, "ORIGIN_REJECTED")
    cross_site = get(tmp_path, "/assets/app-abc123.js", headers={"sec-fetch-site": "cross-site"})
    assert cross_site.status == 403
    no_origin = get(tmp_path, "/", omit_origin=True)
    assert no_origin.status == 200


def test_the_launcher_health_probe_reports_hosted_data_and_asset_provenance(tmp_path: Path) -> None:
    reply = get(tmp_path, "/__gui/healthz")
    assert reply.status == 200
    assert reply.json == {
        "ok": True,
        "service": "hyprial-gui",
        "mode": "production",
        "dataMode": "hosted",
        "driver": "daemon",
        "backendConfigured": True,
        "session": "launch-required",
        "devTools": False,
        "assets": "installed",
        "assetsVerified": True,
    }
    assert reply.headers["cache-control"] == "no-store"


def test_an_explicit_unverified_root_is_reported_as_such(tmp_path: Path) -> None:
    reply = get(tmp_path, "/__gui/healthz", manifest=False)
    assert reply.json["assets"] == "explicit-unverified" and reply.json["assetsVerified"] is False
    assert get(tmp_path / "again", "/assets/app-abc123.js", manifest=False).status == 200


def test_health_needs_no_assets_and_reports_the_configured_driver() -> None:
    async def scenario(gateway):
        return await gateway.call("/__gui/healthz")

    reply = run_gateway(scenario, config=host_config(driver="contract-mock"))
    assert reply.status == 200 and reply.json["driver"] == "contract-mock" and "assets" not in reply.json


@pytest.mark.parametrize(
    "raw",
    [b"/a\x00b", b"/a\x1fb", b"/a b.js"],
)
def test_control_characters_are_400(tmp_path: Path, raw: bytes) -> None:
    holder = assets(tmp_path)

    async def scenario(gateway):
        return await gateway.raw(raw)

    status, _, body = run_gateway(scenario, assets=holder)
    assert status == 400 and b"INVALID_REQUEST" in body


@pytest.mark.parametrize("raw", [b"/%2e%2e/x.js", b"/assets/%2E%2E/%2e%2e/x.js", b"/%5c..%5cx.js", b"/a%2f..%2fb.js", b"/%00x.js"])
def test_encoded_traversal_never_reaches_the_filesystem_outside_the_root(tmp_path: Path, raw: bytes) -> None:
    (tmp_path / "x.js").write_text("secret")
    holder = assets(tmp_path)

    async def scenario(gateway):
        return await gateway.raw(raw)

    status, _, body = run_gateway(scenario, assets=holder)
    assert status in (400, 404) and b"secret" not in body


def test_a_symlink_that_escapes_the_root_is_not_served_in_explicit_mode(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    root = build_root(tmp_path, manifest=False)
    (root / "leak.txt").symlink_to(outside)
    holder = AssetTree.open(root)

    async def scenario(gateway):
        return await gateway.call("/leak.txt")

    reply = run_gateway(scenario, assets=holder)
    assert reply.status == 404 and "secret" not in reply.raw


def test_a_verified_root_serves_only_manifest_files_from_the_verified_bytes(tmp_path: Path) -> None:
    root = build_root(tmp_path, manifest=True)
    holder = AssetTree.open(root)
    (root / "assets" / "app-abc123.js").write_text("tampered after start")
    (root / "extra.txt").write_text("not in the manifest")

    async def scenario(gateway):
        script = await gateway.call("/assets/app-abc123.js")
        extra = await gateway.call("/extra.txt")
        return script, extra

    script, extra = run_gateway(scenario, assets=holder)
    assert script.raw == "console.log('app');"
    assert extra.status == 404


def test_a_tampered_file_is_refused_at_start(tmp_path: Path) -> None:
    root = build_root(tmp_path, manifest=True)
    (root / "assets" / "app-abc123.js").write_text("tampered")
    with pytest.raises(AssetRootError, match="GUI_ASSETS_INVALID"):
        AssetTree.open(root)


def test_a_missing_manifest_file_and_a_missing_index_are_refused_at_start(tmp_path: Path) -> None:
    root = build_root(tmp_path, manifest=True)
    (root / "assets" / "app-abc123.css").unlink()
    with pytest.raises(AssetRootError, match="GUI_ASSETS_MISSING"):
        AssetTree.open(root)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssetRootError, match="index.html"):
        AssetTree.open(empty)
    with pytest.raises(AssetRootError):
        AssetTree.open(tmp_path / "absent")


def test_an_invalid_manifest_is_refused_at_start(tmp_path: Path) -> None:
    root = build_root(tmp_path, manifest=False)
    (root / "release.json").write_text("{not json")
    with pytest.raises(AssetRootError, match="GUI_MANIFEST_INVALID"):
        AssetTree.open(root)


def test_a_assets_fault_is_a_500_host_envelope_without_detail() -> None:
    class Broken:
        def health(self):
            return {}

        def serve(self, path, *, head):
            raise RuntimeError("synthetic /secret/path")

    async def scenario(gateway):
        return await gateway.call("/")

    reply = run_gateway(scenario, assets=Broken())
    assert (reply.status, reply.json["code"], reply.json["dispatch"]) == (500, "HOST_INTERNAL_ERROR", "not-dispatched")
    assert "synthetic" not in reply.raw


def test_dot_segments_are_normalised_and_cannot_climb_above_the_root(tmp_path: Path) -> None:
    (tmp_path / "x.js").write_text("secret")
    holder = assets(tmp_path)

    async def scenario(gateway):
        return [await gateway.raw(raw) for raw in (b"/../etc/passwd", b"/assets/../../x.js", b"/./index.html", b"/assets/../index.html")]

    first, climbing, dotted, parent = run_gateway(scenario, assets=holder)
    assert first[0] == 200 and b"Hyprial" in first[2]
    assert climbing[0] == 404 and b"secret" not in climbing[2]
    assert dotted[0] == 200 and parent[0] == 200


TOOL_FILES = {
    "dev-tools/tools-abc123.js": "export const tools = 1;",
    "dev-tools/tools-abc123.css": ".dev-tools-layer{}",
    "dev-tools/docs/swagger-ui.css": ".swagger-ui{}",
    "dev-tools/docs/swagger-ui-bundle.js": "window.SwaggerUIBundle=1;",
}


def tool_root(tmp_path: Path, *, manifest: bool) -> Path:
    root = tmp_path / "gui"
    for relative, text in TOOL_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return build_root(tmp_path, manifest=manifest)


def tool_get(tmp_path: Path, path: str, *, manifest: bool, dev_tools: bool, **options):
    holder = AssetTree.open(tool_root(tmp_path, manifest=manifest), dev_tools=dev_tools)

    async def scenario(gateway):
        return await gateway.call(path, **options)

    return run_gateway(scenario, assets=holder)


@pytest.mark.parametrize("manifest", [True, False])
def test_the_tool_prefix_is_a_host_404_without_developer_tools(tmp_path: Path, manifest: bool) -> None:
    for relative in TOOL_FILES:
        reply = tool_get(tmp_path, f"/{relative}", manifest=manifest, dev_tools=False)
        assert reply.status == 404 and reply.json["code"] == "NOT_FOUND", relative
        assert TOOL_FILES[relative] not in reply.raw
    for path in ("/dev-tools", "/dev-tools/", "/dev-tools/docs", "/dev-tools/panel", "/dev-tools/missing.js"):
        reply = tool_get(tmp_path, path, manifest=manifest, dev_tools=False)
        assert reply.status == 404 and reply.json["code"] == "NOT_FOUND", path
    head = tool_get(tmp_path, "/dev-tools/tools-abc123.js", manifest=manifest, dev_tools=False, method="HEAD")
    assert head.status == 404
    app = tool_get(tmp_path, "/assets/app-abc123.js", manifest=manifest, dev_tools=False)
    assert app.status == 200 and app.raw == "console.log('app');"
    index = tool_get(tmp_path, "/agents", manifest=manifest, dev_tools=False)
    assert index.status == 200 and "Hyprial" in index.raw


@pytest.mark.parametrize("manifest", [True, False])
@pytest.mark.parametrize(
    "path",
    [
        "/DEV-TOOLS/tools-abc123.js",
        "/Dev-Tools/tools-abc123.js",
        "/dev%2Dtools/tools-abc123.js",
        "/%64ev-tools/tools-abc123.js",
        "/dev-tool%73/tools-abc123.js",
        "/dev-tool%C5%BF/tools-abc123.js",
        "/assets/../dev-tools/tools-abc123.js",
        "//dev-tools/tools-abc123.js",
    ],
)
def test_no_other_spelling_of_the_tool_prefix_reaches_a_tool_file(tmp_path: Path, manifest: bool, path: str) -> None:
    reply = tool_get(tmp_path, path, manifest=manifest, dev_tools=False)
    assert TOOL_FILES["dev-tools/tools-abc123.js"] not in reply.raw, path
    assert reply.status in (400, 404), path


def test_a_link_into_the_tool_prefix_is_not_served_without_developer_tools(tmp_path: Path) -> None:
    root = tool_root(tmp_path, manifest=False)
    (root / "assets" / "alias.js").symlink_to(root / "dev-tools" / "tools-abc123.js")
    (root / "alias").symlink_to(root / "dev-tools", target_is_directory=True)

    async def scenario(gateway):
        return [await gateway.call(path) for path in ("/assets/alias.js", "/alias/tools-abc123.js")]

    for reply in run_gateway(scenario, assets=AssetTree.open(root, dev_tools=False)):
        assert reply.status == 404 and TOOL_FILES["dev-tools/tools-abc123.js"] not in reply.raw
    for reply in run_gateway(scenario, assets=AssetTree.open(root, dev_tools=True)):
        assert reply.status == 200 and reply.raw == TOOL_FILES["dev-tools/tools-abc123.js"]


@pytest.mark.parametrize("manifest", [True, False])
def test_the_tool_prefix_is_served_uncached_with_developer_tools(tmp_path: Path, manifest: bool) -> None:
    for relative, text in TOOL_FILES.items():
        reply = tool_get(tmp_path, f"/{relative}", manifest=manifest, dev_tools=True)
        assert reply.status == 200 and reply.raw == text, relative
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["x-content-type-options"] == "nosniff"
    for path in ("/dev-tools", "/dev-tools/", "/dev-tools/panel", "/dev-tools/missing.js"):
        reply = tool_get(tmp_path, path, manifest=manifest, dev_tools=True)
        assert reply.status == 404 and reply.json["code"] == "NOT_FOUND", path
    app = tool_get(tmp_path, "/assets/app-abc123.js", manifest=manifest, dev_tools=True)
    assert app.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("manifest", [True, False])
def test_the_tree_reads_a_file_for_the_host_whatever_the_gate_says(tmp_path: Path, manifest: bool) -> None:
    holder = AssetTree.open(tool_root(tmp_path, manifest=manifest), dev_tools=False)
    assert holder.read("dev-tools/docs/swagger-ui.css") == b".swagger-ui{}"
    assert holder.read("dev-tools/docs/missing.css") is None
    assert holder.read("../outside.txt") is None
