"""``hyprial org work`` round-trip, org selection, and CAS retries."""

from __future__ import annotations

import json
from functools import wraps
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging import work_items as work_commands
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class _FakeInbox:
    def pending_messages(self, _actor: str) -> tuple[()]:
        return ()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()


class _FakeHarnesses:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str | None] = {}

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str | None]:
        return dict(self.refs)

    def status(self) -> tuple[()]:
        return ()


class FakeWorkDaemon:
    def __init__(self, app: DaemonApplication, monkeypatch: pytest.MonkeyPatch) -> None:
        self.app = app
        self.spaces: dict[str, str] = {}
        self.stale_remaining = 0
        self.writes = 0
        facade_type = type(app._require_orgfs_runtime().facade)
        original = facade_type.write_text

        @wraps(original)
        def write_text(facade, space_id, node, content, **kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("create_only") or kwargs.get("expect_version") is not None:
                self.writes += 1
            if kwargs.get("expect_version") is not None and self.stale_remaining:
                self.stale_remaining -= 1
                raise OrgFsError("stale-write", {"message": "changed concurrently"})
            return original(facade, space_id, node, content, **kwargs)

        monkeypatch.setattr(facade_type, "write_text", write_text)

    def request(self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        return self.app.handle(method, params or {})

    def register(self, actor: str, session_ref: str) -> None:
        name = actor.rsplit(":", 1)[-1]
        self.app.desired_state.upsert_harness(
            HarnessLaunchSpec(
                harness="claude", name=name, headless=True, session_ref=session_ref
            )
        )
        harnesses = self.app._harnesses
        assert isinstance(harnesses, _FakeHarnesses)
        harnesses.refs[("claude", name)] = session_ref


def _invoke(runner: CliRunner, *args: str):
    result = runner.invoke(_cli_root_app, ["org", "work", *args, "--json"])
    body = json.loads(result.stdout) if result.stdout else None
    return result, body


def _backend(
    monkeypatch,
    tmp_path,
    own_daemon_application,
    *,
    orgs: tuple[str, ...] = ("group-acme",),
) -> FakeWorkDaemon:
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "daemon.sock",
            hyprial_home=tmp_path / "home",
            node_id="node",
            owner="allen",
            **_deep_module_daemon_dependencies(),
        )
    )
    app._inbox = _FakeInbox()  # type: ignore[assignment]
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._restore_done.set()
    app._ipc_peer.pid = 9000
    monkeypatch.setattr(work_commands, "_read_process_parent", lambda _pid: 1)
    monkeypatch.setattr(
        work_commands, "process_birth_identity", lambda pid: f"birth:{pid}"
    )
    spaces = {
        org: app._handle_orgfs("orgfs.create", {"name": org})["space_id"]
        for org in orgs
    }
    for space_id in spaces.values():
        app._handle_orgfs("orgfs.mkdir", {"spaceId": space_id, "path": "work"})
    primary_space_id = spaces[orgs[0]]
    fake = FakeWorkDaemon(app, monkeypatch)
    fake.spaces = spaces

    def request(method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if method == "org.list":
            return {
                "orgs": [
                    {"org": org, "spaceId": space_id}
                    for org, space_id in spaces.items()
                ],
                "leftOrgs": [],
            }
        payload = dict(params or {})
        if payload.get("spaceId") == "space-work":
            payload["spaceId"] = primary_space_id
        try:
            result = fake.request(method, payload, **kwargs)
        except DaemonRequestError as error:
            raise _cli_get_services().CliError(error.code, str(error), error.data) from error
        return result

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return fake


def _configured(
    monkeypatch,
    tmp_path,
    own_daemon_application,
    *,
    orgs: tuple[str, ...] = ("group-acme",),
) -> FakeWorkDaemon:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "allen"}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    return _backend(
        monkeypatch, tmp_path, own_daemon_application, orgs=orgs
    )


def test_every_work_subcommand_round_trips_through_orgfs(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    fake = _configured(monkeypatch, tmp_path, own_daemon_application)
    runner = CliRunner()

    added, add = _invoke(
        runner,
        "add",
        "Ship CLI",
        "--assignee",
        "agent:allen:node:worker",
        "--item",
        "tests pass",
        "--depends",
        "M-allen-0000",
        "--acceptance",
        "all green",
    )
    assert added.exit_code == 0, added.output
    item_id = add["item"]["id"]
    assert item_id == "M-allen-0001"

    checked, _ = _invoke(runner, "check", item_id, "1")
    noted, _ = _invoke(runner, "note", item_id, "focused tests green")
    doing, _ = _invoke(runner, "status", item_id, "doing")
    done, _ = _invoke(runner, "done", item_id)
    shown, show = _invoke(runner, "show", item_id)
    listed, listing = _invoke(runner, "ls", "--status", "done", "--owner", "user:allen")

    assert [result.exit_code for result in (checked, noted, doing, done, shown, listed)] == [0] * 6
    assert show["item"]["status"] == "done"
    assert show["item"]["checklist"] == [{"text": "tests pass", "done": True}]
    assert any(
        line.endswith(
            "user:allen: focused tests green (unverified operator)"
        )
        for line in show["item"]["log"]
    )
    assert [item["id"] for item in listing["items"]] == [item_id]
    assert fake.writes == 5
    stored, _version = fake.app._require_orgfs_runtime().facade.read_text(
        fake.spaces["group-acme"], f"work/{item_id}.md"
    )
    assert "Ship CLI" in stored

    removed = runner.invoke(_cli_root_app, ["work", "--help"])
    assert removed.exit_code == 2
    assert "No such command 'work'" in removed.output


def test_repair_cli_uses_the_work_repair_ipc_method(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if method == "org.list":
            return {"orgs": [{"org": "group-acme", "spaceId": "space-group-acme"}]}
        calls.append((method, params))
        return {"item": {"id": params["itemId"]}}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: "/unused")
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "org",
            "work",
            "repair",
            "M-allen-0001",
            "--org",
            "group-acme",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "work.repair",
            {
                "spaceId": "space-group-acme",
                "itemId": "M-allen-0001",
                "actor": "user:hyprial-unit-owner",
            },
        )
    ]


def test_show_node_and_resolve_cli_use_the_conflict_ipc_surfaces(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if method == "org.list":
            return {"orgs": [{"org": "group-acme", "spaceId": "space-group-acme"}]}
        calls.append((method, params))
        return {"item": {"id": params["itemId"]}}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: "/unused")
    runner = CliRunner()

    shown = runner.invoke(
        _cli_root_app,
        [
            "org",
            "work",
            "show",
            "M-allen-0001",
            "--copy",
            "id:copy-a",
            "--org",
            "group-acme",
            "--json",
        ],
    )
    resolved = runner.invoke(
        _cli_root_app,
        [
            "org",
            "work",
            "resolve",
            "M-allen-0001",
            "--keep",
            "id:copy-a",
            "--org",
            "group-acme",
            "--json",
        ],
    )

    assert shown.exit_code == resolved.exit_code == 0
    assert calls == [
        (
            "work.show",
            {
                "spaceId": "space-group-acme",
                "itemId": "M-allen-0001",
                "node": "id:copy-a",
                "actor": "user:hyprial-unit-owner",
            },
        ),
        (
            "work.resolve",
            {
                "spaceId": "space-group-acme",
                "itemId": "M-allen-0001",
                "keep": "id:copy-a",
                "actor": "user:hyprial-unit-owner",
            },
        ),
    ]


def test_org_selection_is_optional_for_one_org_and_typed_for_ambiguity_or_unknown(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "allen"}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    runner = CliRunner()

    _backend(
        monkeypatch,
        tmp_path,
        own_daemon_application,
        orgs=("group-acme", "group-beta"),
    )
    ambiguous, body = _invoke(runner, "ls")
    assert ambiguous.exit_code == 1
    assert body["code"] == "WORK_ORG_REQUIRED"
    assert body["data"]["candidates"] == ["group-acme", "group-beta"]
    assert "group-acme" in body["error"] and "group-beta" in body["error"]

    unknown, body = _invoke(runner, "ls", "--org", "group-unknown")
    assert unknown.exit_code == 1
    assert body["code"] == "WORK_ORG_UNKNOWN"
    assert body["data"]["candidates"] == ["group-acme", "group-beta"]

    explicit, _ = _invoke(runner, "ls", "--org", "group-beta")
    assert explicit.exit_code == 0, explicit.output


def test_org_work_rejects_work_space_and_exposes_no_space_option(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "allen"}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    _backend(monkeypatch, tmp_path, own_daemon_application)
    runner = CliRunner()

    configured = runner.invoke(
        _cli_root_app, ["config", "set", "work.space", "space-bogus", "--json"]
    )
    listed, _ = _invoke(runner, "ls")

    assert configured.exit_code == 1, configured.output
    assert json.loads(configured.stdout)["code"] == "INVALID_CONFIGURATION"
    assert listed.exit_code == 0, listed.output
    assert "work" not in json.loads((home / "settings.json").read_text())

    commands = (
        ("add", "title"),
        ("check", "M-allen-0001", "1"),
        ("note", "M-allen-0001", "text"),
        ("status", "M-allen-0001", "doing"),
        ("done", "M-allen-0001"),
        ("show", "M-allen-0001"),
        ("ls",),
        ("link", "M-allen-0001", "wf-1234567890abcdef1234567890abcdef"),
        ("keywords", "M-allen-0001"),
        ("repair", "M-allen-0001"),
        ("resolve", "M-allen-0001"),
        ("collect",),
        ("finish",),
    )
    for command in commands:
        help_result = runner.invoke(_cli_root_app, ["org", "work", command[0], "--help"])
        assert help_result.exit_code == 0, help_result.output
        assert "--org" in help_result.output
        assert "--space" not in help_result.output


def test_workflow_missions_is_retired_without_an_alias(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    _configured(monkeypatch, tmp_path, own_daemon_application)

    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "missions", "check", "space-work", "--json"]
    )

    assert result.exit_code == 2
    assert "No such command 'missions'" in result.output


def test_collect_and_finish_round_trip_against_org_work_items(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    _configured(monkeypatch, tmp_path, own_daemon_application)
    calls: list[tuple[str, dict[str, Any]]] = []
    now_ms = 10**13
    item_text = (
        "## M-allen-0001 Login rollout\n\n"
        "```yaml\n"
        "id: M-allen-0001\nstatus: doing\nowner: user:allen\n"
        "assignee: null\nchecklist: []\ndepends: []\nacceptance: ''\n"
        "opened: 1\nclosed: null\nkeywords: [login]\npacs: []\n"
        "ownerActions: []\n"
        "```\n\n## Log\n"
    )

    def request(method: str, params: dict[str, Any] | None = None, **_kwargs: Any):
        payload = dict(params or {})
        calls.append((method, payload))
        if method == "org.list":
            return {"orgs": [{"org": "group-acme", "spaceId": "space-group-acme"}]}
        if method == "workflow.list":
            return {
                "runs": [
                    {
                        "graphId": "wf-round",
                        "name": "association-round",
                        "sender": "user:allen",
                        "state": "running",
                        "createdAtMs": now_ms,
                        "routineName": "associate",
                    },
                    {
                        "graphId": "wf-new",
                        "name": "fix-login",
                        "sender": "user:allen",
                        "state": "running",
                        "createdAtMs": now_ms,
                        "routineName": None,
                    },
                ]
            }
        if method == "orgfs.ls":
            assert payload == {"spaceId": "space-group-acme", "path": "work"}
            return {"nodes": [{"name": "M-allen-0001.md", "node_id": "n1"}]}
        if method == "orgfs.read":
            return {"text": item_text, "version": "v1"}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr("time.time", lambda: now_ms / 1000)
    runner = CliRunner()
    collected_result, collected = _invoke(
        runner,
        "collect",
        "--org",
        "group-acme",
        "--routine",
        "associate",
        "--graph",
        "wf-round",
    )
    assert collected_result.exit_code == 0, collected_result.output
    assert collected["spaceId"] == "space-group-acme"
    assert collected["graphs"][0]["candidates"][0]["id"] == "M-allen-0001"

    collected_file = tmp_path / "collected.json"
    replies_file = tmp_path / "replies.json"
    collected_file.write_text(json.dumps(collected), encoding="utf-8")
    replies_file.write_text(
        json.dumps(
            {"wf-new": {"answers": {"c0": {"type": "noul", "noul": 0.9}}}}
        ),
        encoding="utf-8",
    )
    finished_result, finished = _invoke(
        runner,
        "finish",
        "--org",
        "group-acme",
        "--collected",
        str(collected_file),
        "--replies",
        str(replies_file),
    )
    assert finished_result.exit_code == 0, finished_result.output
    assert finished["results"][0]["match"] == "M-allen-0001"
    assert [method for method, _ in calls].count("org.list") == 2


def test_ls_invalid_lists_only_invalid_items_while_plain_ls_keeps_valid_items(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    fake = _configured(monkeypatch, tmp_path, own_daemon_application)
    runner = CliRunner()
    added, body = _invoke(runner, "add", "Valid item")
    assert added.exit_code == 0, added.output
    valid_id = body["item"]["id"]
    fake.app._require_orgfs_runtime().facade.write_text(
        fake.spaces["group-acme"],
        "work/M-raw-0002.md",
        "## broken\n\n```yaml\nid: M-raw-0002\nstatus: bogus\n```\n",
        create_only=True,
    )

    invalid_result, invalid = _invoke(runner, "ls", "--invalid")
    plain_result, plain = _invoke(runner, "ls")

    assert invalid_result.exit_code == 0, invalid_result.output
    assert [item["id"] for item in invalid["items"]] == ["M-raw-0002"]
    assert invalid["items"][0]["invalid"] is True
    assert plain_result.exit_code == 0, plain_result.output
    assert valid_id in {item["id"] for item in plain["items"]}


def test_overview_show_reads_an_item_created_in_the_org_work_directory(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    _configured(monkeypatch, tmp_path, own_daemon_application)
    runner = CliRunner()
    graph_id = "wf-1234567890abcdef1234567890abcdef"
    added, body = _invoke(runner, "add", "Login rollout")
    assert added.exit_code == 0, added.output
    linked, _ = _invoke(runner, "link", body["item"]["id"], graph_id)
    assert linked.exit_code == 0, linked.output
    backend_request = _cli_support._daemon_request

    def request(method: str, params: dict[str, Any] | None = None, **kwargs: Any):
        if method == "ps":
            return {"daemon": {"nodeId": "node"}}
        if method == "workflow.list":
            return {
                "runs": [
                    {
                        "graphId": graph_id,
                        "name": "fix-login",
                        "sender": "user:allen",
                        "state": "running",
                        "createdAtMs": 10**13,
                    }
                ]
            }
        if method == "workflow.status":
            return {"nodes": [], "edges": [], "roster": {"nodes": []}}
        if method == "routine.list":
            return {"routines": []}
        return backend_request(method, params, **kwargs)

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = runner.invoke(
        _cli_root_app,
        ["workflow", "overview", "show", "--org", "group-acme", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [item["id"] for item in payload["missions"]] == [body["item"]["id"]]
    assert payload["runs"][0]["line"].endswith(f"|m:{body['item']['id']}")


def test_expect_version_conflict_retries_then_surfaces_typed_exhaustion(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    fake = _configured(monkeypatch, tmp_path, own_daemon_application)
    runner = CliRunner()
    added, add = _invoke(runner, "add", "Retry me")
    assert added.exit_code == 0, added.output
    item_id = add["item"]["id"]

    fake.stale_remaining = 1
    retried, _ = _invoke(runner, "note", item_id, "survived")
    assert retried.exit_code == 0, retried.output

    fake.stale_remaining = 3
    exhausted, body = _invoke(runner, "note", item_id, "never written")
    assert exhausted.exit_code == 1
    assert body["code"] == "WORK_ITEM_VERSION_CONFLICT"
    assert body["data"]["attempts"] == 3


def test_done_requires_authenticated_agent_owner_and_complete_checklist(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    fake = _configured(monkeypatch, tmp_path, own_daemon_application)
    runner = CliRunner()
    fake.register("agent:allen:node:worker", "session:one")
    fake.register("agent:allen:node:other", "session:two")
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:allen:node:worker")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session:one")
    added, add = _invoke(
        runner,
        "add",
        "Agent-owned",
        "--owner",
        "agent:allen:node:worker",
        "--item",
        "finish",
    )
    assert added.exit_code == 0, added.output
    item_id = add["item"]["id"]

    unticked, body = _invoke(runner, "done", item_id)
    assert unticked.exit_code == 1
    assert body["code"] == "WORK_ITEM_CHECKLIST_INCOMPLETE"

    checked, _ = _invoke(runner, "check", item_id, "1")
    assert checked.exit_code == 0, checked.output
    accepted, _ = _invoke(runner, "done", item_id)
    assert accepted.exit_code == 0, accepted.output

    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:allen:node:other")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session:two")
    reopened, _ = _invoke(runner, "status", item_id, "doing")
    assert reopened.exit_code == 0, reopened.output
    refused, body = _invoke(runner, "done", item_id)
    assert refused.exit_code == 1
    assert body["code"] == "WORK_ITEM_OWNER_REQUIRED"


def test_squire_association_commands_use_work_ipc_without_raw_writes(
    monkeypatch, tmp_path, own_daemon_application
) -> None:
    _configured(monkeypatch, tmp_path, own_daemon_application)
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if method == "org.list":
            return {"orgs": [{"org": "group-acme", "spaceId": "space-group-acme"}]}
        calls.append((method, params))
        return {
            "spaceId": "space-group-acme",
            "version": "v1",
            "item": {
                "id": "M-allen-0001",
                "pacs": ["wf-1234567890abcdef1234567890abcdef"],
                "keywords": ["login"],
                "log": [],
            },
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()
    linked, _ = _invoke(
        runner,
        "link",
        "M-allen-0001",
        "wf-1234567890abcdef1234567890abcdef",
    )
    tagged, _ = _invoke(
        runner,
        "keywords",
        "M-allen-0001",
        "--add",
        "login",
        "--remove",
        "stale",
    )

    assert linked.exit_code == 0, linked.output
    assert tagged.exit_code == 0, tagged.output
    assert [method for method, _params in calls] == ["work.link", "work.keywords"]
    assert not [method for method, _params in calls if method == "orgfs.write"]
