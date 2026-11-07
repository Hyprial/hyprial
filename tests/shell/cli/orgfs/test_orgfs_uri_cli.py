'orgfs URIs on the CLI surface: `fs resolve <uri>` and the human web link.\n\nCliRunner tests against a monkeypatched daemon request (same pattern as\ntests/integration/orgfs/commands/test_orgfs_cli_daemon.py); each names the kill-mutation that must\nturn it red.\n'

from __future__ import annotations

import json

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.fs import commands as fs_module
from hyprial.kernel.impl.primitives.uri import canonical_orgfs_uri

OWNER = "owner@example.com"


def test_fs_resolve_cli_accepts_the_single_argument_uri_form(monkeypatch, tmp_path) -> None:
    """hyprial fs resolve <uri> picks the spaceId through the one reader and
    sends the full URI as the path; the daemon re-checks the binding."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls = []

    def request(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"nodes": []}

    monkeypatch.setattr('hyprial.shell.impl.cli.commands.common.support._daemon_request', request)
    runner = CliRunner()

    uri = canonical_orgfs_uri(OWNER, "6b69777a-3e0e-4d2b-9d8c-3f6a7b1c2d4e", "ab" * 16)
    result = runner.invoke(app, ["org", "fs", "resolve", uri, "--json"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ("orgfs.resolve", {"spaceId": "6b69777a-3e0e-4d2b-9d8c-3f6a7b1c2d4e", "path": uri})
    ]

    # A path next to a URI target is a Typer usage error.
    rejected = runner.invoke(app, ["org", "fs", "resolve", uri, "extra"])
    assert rejected.exit_code != 0

    # The legacy <space-id> <path> form is byte-identical to before.
    calls.clear()
    legacy = runner.invoke(app, ["org", "fs", "resolve", "space-1", "docs/a.md", "--json"])
    assert legacy.exit_code == 0, legacy.output
    assert calls == [("orgfs.resolve", {"spaceId": "space-1", "path": "docs/a.md"})]


def test_cli_human_output_derives_the_web_url_on_another_domain(
    monkeypatch, tmp_path
) -> None:
    """§7a surface test on a non-hyprial domain: nothing assumes ours.
    Kill-mutation: hard-coding the web host in the derivation helper turns
    this red (together with the static guard)."""

    home = tmp_path / "home"
    home.mkdir(parents=True)
    # The tailnet cutover retired the tsnet node.json FQDN derivation: the
    # web host comes only from settings `orgfs.web_host`, never guessed.
    (home / "settings.json").write_text(
        json.dumps({"orgfs": {"web_host": "fs.corp-b.example"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    space_id = "6b69777a-3e0e-4d2b-9d8c-3f6a7b1c2d4e"
    node_id = "ab" * 16
    uri = canonical_orgfs_uri(OWNER, space_id, node_id)

    def request(method, params=None, **_kwargs):
        assert method == "orgfs.resolve"
        return {
            "nodes": [
                {
                    "space_id": space_id,
                    "node_id": node_id,
                    "name": "readme.md",
                    "uri": uri,
                }
            ]
        }

    monkeypatch.setattr('hyprial.shell.impl.cli.commands.common.support._daemon_request', request)
    runner = CliRunner()

    human = runner.invoke(app, ["org", "fs", "resolve", uri])
    assert human.exit_code == 0, human.output
    # Rich's Pretty hard-wraps long values; compare against the unwrapped text.
    unwrapped = human.output.replace("\n", "")
    assert f"https://fs.corp-b.example/{OWNER}/{space_id}/{node_id}" in unwrapped

    # The wire form stays host-free: the derived link is human-output only.
    machine = runner.invoke(app, ["org", "fs", "resolve", uri, "--json"])
    assert machine.exit_code == 0, machine.output
    assert "webUrl" not in machine.output
    assert uri in machine.output


def test_local_file_commands_send_only_the_daemon_resolved_caller_identity(
    monkeypatch, tmp_path
) -> None:
    """m4: import/export have no caller-selected identity on their wire request."""

    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    monkeypatch.delenv("HYPRIAL_WORKER_SESSION_REF", raising=False)
    monkeypatch.setattr(fs_module, "_local_operator_identity", lambda: "user:local-owner")
    calls = []
    monkeypatch.setattr(
        fs_module,
        "_fs_run",
        lambda method, params, *, json_output: calls.append((method, params)),
    )
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    destination = tmp_path / "destination.bin"
    runner = CliRunner()

    imported = runner.invoke(
        app, ["org", "fs", "import", "space-1", "doc.bin", str(source), "--json"]
    )
    exported = runner.invoke(
        app, ["org", "fs", "export", "space-1", "doc.bin", str(destination), "--json"]
    )

    assert imported.exit_code == 0, imported.output
    assert exported.exit_code == 0, exported.output
    assert [(method, params["from"]) for method, params in calls] == [
        ("orgfs.import", "user:local-owner"),
        ("orgfs.export", "user:local-owner"),
    ]
    assert all("onBehalfOf" not in params for _method, params in calls)

    caller_chosen = runner.invoke(
        app,
        [
            "org", "fs",
            "export",
            "space-1",
            "doc.bin",
            str(destination),
            "--from",
            "user:attacker",
        ],
    )
    assert caller_chosen.exit_code != 0
    assert len(calls) == 2
