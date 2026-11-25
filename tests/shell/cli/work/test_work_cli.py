"""Work shortcuts use the same organization selection and OrgFS reads as files."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.services import get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


def _invoke(*args):
    result = CliRunner().invoke(app, [*args, "--json"])
    return result, json.loads(result.stdout) if result.stdout else None


def _work(*args):
    return _invoke("org", "work", *args)


def _fs(*args):
    return _invoke("org", "fs", *args)


def _backend(monkeypatch, tmp_path, own_daemon_application, *, orgs=("group-acme",)):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"version": 1, "owner": "allen"}))
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    daemon = own_daemon_application(DaemonApplication(
        state_dir=tmp_path / "state", socket_path=tmp_path / "daemon.sock",
        hyprial_home=home, node_id="node", owner="allen", **daemon_dependencies(),
    ))
    daemon._inbox = SimpleNamespace(pending_messages=lambda actor: ())
    daemon._harnesses = SimpleNamespace(status=lambda: (), projected_worker_session_refs=lambda: {})
    daemon._presence = SimpleNamespace(online_actors=lambda: ())
    daemon._restore_done.set()
    spaces = {}
    facade = daemon._require_orgfs_runtime().facade
    for org in orgs:
        spaces[org] = daemon._handle_orgfs("orgfs.create", {"name": org})["space_id"]
        OrgFsDirectoryStore(facade).put_org_meta(org, {"state": "active"})
        facade.mkdir(spaces[org], "work")
    calls = []

    def request(method, params=None, **kwargs):
        calls.append((method, dict(params or {})))
        try:
            return daemon.handle(method, params or {})
        except DaemonRequestError as error:
            raise get_services().CliError(error.code, str(error), error.data) from error

    monkeypatch.setattr(support, "_daemon_request", request)
    return SimpleNamespace(daemon=daemon, facade=facade, spaces=spaces, calls=calls)


@pytest.mark.parametrize("name", ["plain-name", "自由 笔记.txt", "legacy-item.md"])
def test_show_preserves_arbitrary_document_text_and_standard_read_envelope(
    monkeypatch, tmp_path, own_daemon_application, name
):
    backend = _backend(monkeypatch, tmp_path, own_daemon_application)
    sid = backend.spaces["group-acme"]
    text = "# 自由笔记\n\n```yaml\nstatus: anything\nunknown: [unfinished\n```\n尾行\n"
    written, _ = _fs("write", sid, f"work/{name}", text)
    assert written.exit_code == 0, written.output
    direct, expected = _fs("read", sid, f"work/{name}", "--wait", "0")
    assert direct.exit_code == 0, direct.output
    backend.calls.clear()

    shown, payload = _work("show", name, "--wait", "0")

    assert shown.exit_code == 0, shown.output
    assert payload == expected
    assert payload["text"] == text
    assert payload["version"]
    assert backend.calls == [
        ("org.resolve", {}),
        ("orgfs.read", {"spaceId": sid, "path": f"work/{name}", "waitSeconds": 0.0}),
    ]


def test_show_accepts_nested_work_paths_and_preserves_binary_reads(
    monkeypatch, tmp_path, own_daemon_application
):
    backend = _backend(monkeypatch, tmp_path, own_daemon_application)
    sid = backend.spaces["group-acme"]
    backend.facade.mkdir(sid, "work/notes")
    backend.facade.write_bytes(sid, "work/notes/raw.bin", b"\x00\xff\x10")
    direct, expected = _fs("read", sid, "work/notes/raw.bin", "--wait", "0")
    shown, payload = _work("show", "notes/raw.bin", "--wait", "0")
    assert direct.exit_code == shown.exit_code == 0
    assert payload == expected
    assert payload["contentB64"] == "AP8Q"


def test_ls_returns_the_standard_listing_without_task_filtering(
    monkeypatch, tmp_path, own_daemon_application
):
    backend = _backend(monkeypatch, tmp_path, own_daemon_application)
    sid = backend.spaces["group-acme"]
    backend.facade.write_text(sid, "work/anything.txt", "no task metadata")
    backend.facade.mkdir(sid, "work/folder")
    direct, expected = _fs("ls", sid, "work")
    backend.calls.clear()

    listed, payload = _work("ls")

    assert direct.exit_code == listed.exit_code == 0
    assert payload == expected
    assert {node["name"] for node in payload["nodes"]} == {"anything.txt", "folder"}
    assert backend.calls == [("org.resolve", {}), ("orgfs.ls", {"spaceId": sid, "path": "work"})]


def test_org_choice_stays_daemon_owned_for_default_explicit_and_current(
    monkeypatch, tmp_path, own_daemon_application
):
    backend = _backend(monkeypatch, tmp_path, own_daemon_application, orgs=("group-acme", "group-beta"))
    ambiguous, payload = _work("ls")
    assert ambiguous.exit_code == 1
    assert payload["code"] == "ORG_REQUIRED"
    assert payload["data"]["candidates"] == ["group-acme", "group-beta"]
    unknown, payload = _work("ls", "--org", "group-missing")
    assert unknown.exit_code == 1
    assert payload["code"] == "INVALID_ARGUMENT"
    selected, _ = _invoke("org", "use", "group-beta")
    assert selected.exit_code == 0
    backend.calls.clear()
    current, _ = _work("ls")
    explicit, _ = _work("ls", "--org", "group-acme")
    assert current.exit_code == explicit.exit_code == 0
    assert backend.calls == [
        ("org.resolve", {}), ("orgfs.ls", {"spaceId": backend.spaces["group-beta"], "path": "work"}),
        ("org.resolve", {"org": "group-acme"}), ("orgfs.ls", {"spaceId": backend.spaces["group-acme"], "path": "work"}),
    ]


def test_standard_write_keeps_cas_and_shortcuts_never_retry_or_rewrite(
    monkeypatch, tmp_path, own_daemon_application
):
    backend = _backend(monkeypatch, tmp_path, own_daemon_application)
    sid = backend.spaces["group-acme"]
    initial, _ = _fs("write", sid, "work/plain", "first")
    assert initial.exit_code == 0
    shown, before = _work("show", "plain")
    assert shown.exit_code == 0
    updated, _ = _fs("write", sid, "work/plain", "second", "--expect-version", before["version"])
    assert updated.exit_code == 0
    backend.calls.clear()
    stale, failure = _fs("write", sid, "work/plain", "stale", "--expect-version", before["version"])
    assert stale.exit_code == 1
    assert failure["code"] == "stale-write"
    assert [method for method, _ in backend.calls] == ["orgfs.write"]
    current, payload = _work("show", "plain")
    assert current.exit_code == 0
    assert payload["text"] == "second"
    assert not any(method.startswith("work.") for method, _ in backend.calls)


@pytest.mark.parametrize("path", ["../outside", "/outside", "folder/../outside"])
def test_show_keeps_orgfs_relative_path_rejections(monkeypatch, tmp_path, own_daemon_application, path):
    backend = _backend(monkeypatch, tmp_path, own_daemon_application)
    direct, expected = _fs("read", backend.spaces["group-acme"], f"work/{path}", "--wait", "0")
    shown, payload = _work("show", path, "--wait", "0")
    assert direct.exit_code == shown.exit_code == 1
    assert payload == expected
    assert payload["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("command", [
    "add", "check", "done", "status", "log", "note", "link", "acceptance",
    "repair", "resolve", "keywords", "collect", "finish",
])
def test_retired_work_operations_are_unregistered_without_daemon_calls(monkeypatch, command):
    monkeypatch.setattr(support, "_daemon_request", lambda *args, **kwargs: pytest.fail("retired CLI sent a request"))
    result = CliRunner().invoke(app, ["org", "work", command, "--json"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_work_help_exposes_only_file_read_shortcuts():
    help_result = CliRunner().invoke(app, ["org", "work", "--help"])
    assert help_result.exit_code == 0
    assert "show" in help_result.output and "ls" in help_result.output
    assert "org fs" in help_result.output
    for option in ("--owner", "--assignee", "--status", "--invalid", "--copy", "--space"):
        assert option not in CliRunner().invoke(app, ["org", "work", "ls", "--help"]).output
        assert option not in CliRunner().invoke(app, ["org", "work", "show", "--help"]).output


def test_show_human_output_uses_the_ordinary_fs_renderer(monkeypatch, tmp_path, own_daemon_application):
    backend = _backend(monkeypatch, tmp_path, own_daemon_application)
    sid = backend.spaces["group-acme"]
    backend.facade.write_text(sid, "work/plain.txt", "unchanged\nraw content\n")
    runner = CliRunner()
    direct = runner.invoke(app, ["org", "fs", "read", sid, "work/plain.txt", "--wait", "0"])
    shortcut = runner.invoke(app, ["org", "work", "show", "plain.txt", "--wait", "0"])
    assert direct.exit_code == shortcut.exit_code == 0
    assert direct.stdout == shortcut.stdout
