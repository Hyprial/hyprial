"""org invite short-link presentation and degradation."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.runtime import entry as cli
from tests.shell.cli.org.test_cli_org import _invite_result, _org_ipc_env


def test_org_invite_short_link_is_primary_and_long_link_stays(
    monkeypatch, tmp_path: Path
) -> None:
    from hyprial.shell.impl.invites import shortlink

    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_k: _invite_result())
    monkeypatch.setattr(
        shortlink,
        "shorten_invite_link",
        lambda link: "https://hyprial.ai/s/abc",
    )

    result = CliRunner().invoke(
        cli.app, ["org", "invite", "group-acme", "--user", "bob", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["short"] == "https://hyprial.ai/s/abc"
    assert payload["link"] == _invite_result()["link"]
    assert "secretpayload" not in (result.stderr or "")


def test_org_invite_short_link_failure_keeps_the_long_link(
    monkeypatch, tmp_path: Path
) -> None:
    from hyprial.shell.impl.invites import shortlink
    from hyprial.shell.impl.invites.shortlink import ShortLinkUnavailable

    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_k: _invite_result())

    def unavailable(link):
        raise ShortLinkUnavailable("short link service timed out")

    monkeypatch.setattr(shortlink, "shorten_invite_link", unavailable)

    result = CliRunner().invoke(
        cli.app, ["org", "invite", "group-acme", "--user", "bob", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["short"] is None
    assert payload["link"] == _invite_result()["link"]
    assert "short link service timed out" in (result.stderr or "")


def test_org_invite_human_output_marks_short_and_secret(
    monkeypatch, tmp_path: Path
) -> None:
    from hyprial.shell.impl.invites import shortlink

    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_k: _invite_result())
    monkeypatch.setattr(
        shortlink,
        "shorten_invite_link",
        lambda link: "https://hyprial.ai/s/abc",
    )

    result = CliRunner().invoke(
        cli.app, ["org", "invite", "group-acme", "--user", "bob"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("https://hyprial.ai/s/abc\n")
    assert f"长链：{_invite_result()['link']}" in result.stdout
    assert "私发，勿贴群" in result.stdout


def test_org_invite_unlogged_short_link_is_silent(monkeypatch, tmp_path: Path) -> None:
    from hyprial.shell.impl.invites import shortlink
    from hyprial.shell.impl.invites.shortlink import ShortLinkUnavailable

    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_k: _invite_result())

    def unavailable(link):
        raise ShortLinkUnavailable("SHORT_LINK_NOT_LOGGED_IN")

    monkeypatch.setattr(shortlink, "shorten_invite_link", unavailable)
    result = CliRunner().invoke(
        cli.app, ["org", "invite", "group-acme", "--user", "bob", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["short"] is None
    assert "SHORT_LINK_NOT_LOGGED_IN" in (result.stderr or "")


def test_org_invite_corrupt_login_still_prints_the_long_link(
    monkeypatch, tmp_path: Path
) -> None:
    home = _org_ipc_env(monkeypatch, tmp_path)
    secrets = home / "secrets"
    secrets.mkdir()
    (secrets / "login.json").write_bytes(b"\xff\xfe-not-utf8")
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_k: _invite_result())

    result = CliRunner().invoke(
        cli.app, ["org", "invite", "group-acme", "--user", "bob"]
    )

    assert result.exit_code == 0, result.output
    assert _invite_result()["link"] in result.stdout
    assert "SHORT_LINK_FAILED" in (result.stderr or "")
