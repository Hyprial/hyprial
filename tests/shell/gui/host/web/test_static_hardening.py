"""Static targets the host must not mishandle: bad escapes, dot segments into /daemon, dotfiles."""

from __future__ import annotations

from pathlib import Path

from hyprial.shell.impl.gui.host.web.assets import AssetTree
from tests.shell.gui.host.support import run_gateway
from tests.shell.gui.host.web.test_assets import build_root


def explicit_tree(tmp_path: Path) -> AssetTree:
    root = build_root(tmp_path, manifest=False)
    (root / ".env").write_text("SECRET=1")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]")
    (root / "assets" / ".hidden.js").write_text("hidden")
    return AssetTree.open(root)


def test_invalid_utf8_percent_escapes_are_a_400_not_a_500(tmp_path: Path) -> None:
    holder = explicit_tree(tmp_path)

    async def scenario(gateway) -> list[tuple[int, str]]:
        outcomes = []
        for raw in (b"/%ff", b"/assets/%c3%28.js", b"/%80%80"):
            status, _, body = await gateway.raw(raw)
            outcomes.append((status, body.decode()))
        return outcomes

    for status, body in run_gateway(scenario, assets=holder):
        assert status == 400 and '"code":"INVALID_REQUEST"' in body


def test_dotfiles_and_dot_directories_in_the_asset_root_are_not_served(tmp_path: Path) -> None:
    holder = explicit_tree(tmp_path)

    async def scenario(gateway) -> list[tuple[int, str]]:
        outcomes = []
        for raw in (b"/.env", b"/.git/config", b"/assets/.hidden.js", b"/%2eenv", b"/.well-known/x"):
            status, _, body = await gateway.raw(raw)
            outcomes.append((status, body.decode()))
        return outcomes

    for status, body in run_gateway(scenario, assets=holder):
        assert status == 404 and "SECRET" not in body and "core" not in body and "hidden" not in body


def test_a_dot_segment_that_leaves_daemon_is_a_static_request_that_passes_the_host_check_first(
    tmp_path: Path,
) -> None:
    holder = explicit_tree(tmp_path)

    async def scenario(gateway) -> tuple[tuple[int, str], tuple[int, str], tuple[int, str]]:
        served = await gateway.raw(b"/daemon/../index.html")
        denied = await gateway.raw(b"/daemon/../index.html", headers={"host": "evil.example:4173"})
        session = await gateway.raw(b"/daemon/x/../session", headers={"host": "evil.example:4173"})
        return (served[0], served[2].decode()), (denied[0], denied[2].decode()), (session[0], session[2].decode())

    served, denied, session = run_gateway(scenario, assets=holder)
    assert served[0] == 200 and "<title>Hyprial</title>" in served[1]
    assert denied[0] == 403 and '"code":"HTTP_HOST_DENIED"' in denied[1]
    assert session[0] == 403 and '"code":"HTTP_HOST_DENIED"' in session[1]
