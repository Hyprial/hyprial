# Decisions (phase D, package D4b, 2026-11-25):
# - Design §1.2: ``org fs rm --permanent`` sends ``permanent: true`` and the
#   plain form sends no ``permanent`` (the daemon default is a soft removal);
#   ``org fs trash restore SPACE NODE`` takes no version and sends none.
# - The daemon request is replaced at the CLI's single request seam, as in
#   tests/shell/cli/orgfs/test_orgfs_uri_cli.py.
"""``org fs rm --permanent`` and ``org fs trash restore`` on the CLI surface."""

from __future__ import annotations

from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime.entry import app


def _calls(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls: list[tuple[str, dict]] = []

    def request(method, params=None, **_kwargs):
        calls.append((method, dict(params or {})))
        if method == "orgfs.restore":
            return {"path": "docs/a.md", "deleted": False}
        return {"ok": True}

    monkeypatch.setattr("hyprial.shell.impl.cli.commands.common.support._daemon_request", request)
    return calls


def test_fs_rm_sends_permanent_only_when_asked(monkeypatch, tmp_path) -> None:
    calls = _calls(monkeypatch, tmp_path)
    runner = CliRunner()

    soft = runner.invoke(app, ["org", "fs", "rm", "space-1", "docs/a.md", "--json"])
    assert soft.exit_code == 0, soft.output
    permanent = runner.invoke(
        app, ["org", "fs", "rm", "space-1", "docs/a.md", "--permanent", "--json"]
    )
    assert permanent.exit_code == 0, permanent.output

    assert calls == [
        ("orgfs.remove", {"spaceId": "space-1", "path": "docs/a.md"}),
        ("orgfs.remove", {"spaceId": "space-1", "path": "docs/a.md", "permanent": True}),
    ]
    assert "--permanent" in runner.invoke(app, ["org", "fs", "rm", "--help"]).output


def test_fs_trash_restore_takes_no_version(monkeypatch, tmp_path) -> None:
    calls = _calls(monkeypatch, tmp_path)
    runner = CliRunner()

    restored = runner.invoke(
        app, ["org", "fs", "trash", "restore", "space-1", "id:node-1", "--no-recursive", "--json"]
    )
    assert restored.exit_code == 0, restored.output
    assert calls == [
        ("orgfs.restore", {"spaceId": "space-1", "node": "id:node-1", "recursive": False}),
    ]

    calls.clear()
    extra = runner.invoke(
        app, ["org", "fs", "trash", "restore", "space-1", "id:node-1", "version-1", "--json"]
    )
    assert extra.exit_code != 0
    assert calls == []
