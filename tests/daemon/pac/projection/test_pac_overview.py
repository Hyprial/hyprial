"""PAC overview model and orgfs publish (Allen 2026-09-29: a live page on orgfs).

The model and publish run against faked daemon responses; the two-daemon
orgfs path is exercised by the multi-user scenario, not here.
"""

from __future__ import annotations

from tests.support.tailnet_daemon import bind_admitted
import base64

import json
import re
import shutil
import socket
import subprocess
import time
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.pac.views import overview
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.runtime._owned_applications import close_application
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
NODE = "hq-node"
CLI_DEV = "agent:owner@example.test:hq-node:hyprial-cli-developer"
SQUIRE = "agent:owner@example.test:hq-node:squire"


def _run(
    graph_id: str,
    name: str,
    *,
    sender: str = CLI_DEV,
    state: str = "completed",
    created: int = 1_000,
    closed: int | None = 2_000,
    routine: str | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "graphId": graph_id,
        "name": name,
        "sender": sender,
        "state": state,
        "createdAtMs": created,
        "closedAtMs": closed,
        "routineName": routine,
        "lastProgressAtMs": closed or created,
        "currentNode": None,
        "nodes": nodes or [],
        "edges": [],
        "roster": [],
        "graphReason": None,
    }


def _snapshot(
    *runs: dict[str, Any], routines: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {"observedAt": 9_000, "runs": list(runs), "routines": routines or []}


def test_routines_are_lines_of_their_own_under_their_owner() -> None:
    model = overview.build_model(
        _snapshot(
            _run("wf-1", "cli-dev-AZ-ready-wait"),
            _run(
                "wf-2",
                "routine-lark-flycheck",
                sender="user:owner",
                routine="lark-flycheck",
            ),
            routines=[
                {
                    "name": "lark-flycheck",
                    "owner": SQUIRE,
                    "mode": "source",
                    "nextDueMs": 5,
                    "inFlight": [],
                },
                {
                    "name": "never-ran",
                    "owner": SQUIRE,
                    "mode": "scheduled",
                    "inFlight": [],
                },
            ],
        ),
        node=NODE,
    )
    lines = {line["id"]: line for line in model["lines"]}
    flycheck = lines[f"{NODE}|{SQUIRE}|r:lark-flycheck"]
    assert flycheck["kind"] == "routine"
    assert flycheck["actor"] == f"{NODE}|{SQUIRE}"  # owned by the routine's owner
    assert flycheck["runs"] == []  # routine runs are ticks, not workflow cards
    assert flycheck["routines"][0]["runs"] == ["wf-2"]
    assert lines[f"{NODE}|{SQUIRE}|r:never-ran"]["routines"][0]["runs"] == []
    # An unrelated one-off graph goes to its actor's "other" line.
    assert lines[f"{NODE}|{CLI_DEV}|misc"]["runs"] == ["wf-1"]
    runs = {run["id"]: run for run in model["runs"]}
    assert runs["wf-2"]["line"] == f"{NODE}|{SQUIRE}|r:lark-flycheck"
    actors = {actor["id"]: actor for actor in model["actors"]}
    assert actors[f"{NODE}|{SQUIRE}"]["name"] == "squire"
    assert all(line["node"] == NODE for line in model["lines"])


def test_related_graphs_form_one_task_line_owned_by_the_actor_doing_most() -> None:
    hq = "agent:owner@example.test:hq-node:hq-adjutant"
    model = overview.build_model(
        _snapshot(
            _run("wf-a", "cli-dev-AZ-ready-wait", created=1),
            _run("wf-b", "review-970-ready-wait", sender=hq, created=2),
            _run("wf-c", "cli-dev-AZ2-ready-wait-fix", created=3),
            _run("wf-d", "orgfs-import-check", sender=hq, created=4),
            _run("wf-e", "operator-console", sender="user:owner", created=5),
        ),
        node=NODE,
    )
    task_lines = [line for line in model["lines"] if line["kind"] == "task"]
    assert len(task_lines) == 1
    line = task_lines[0]
    assert line["actor"] == f"{NODE}|{CLI_DEV}"  # two of the three graphs
    assert line["runs"] == ["wf-a", "wf-b", "wf-c"]
    assert line["title"] == "ready-wait"
    runs = {run["id"]: run for run in model["runs"]}
    # hq-adjutant's review stays on the line but keeps its own actor.
    assert runs["wf-b"]["line"] == line["id"]
    assert runs["wf-b"]["actor"] == f"{NODE}|{hq}"
    assert runs["wf-d"]["line"] == f"{NODE}|{hq}|misc"
    humans = [actor for actor in model["actors"] if actor["human"]]
    assert [actor["name"] for actor in humans] == ["user:owner"]


def test_links_are_inferred_from_names_and_skip_routine_runs() -> None:
    model = overview.build_model(
        _snapshot(
            _run("wf-a", "review-952-sidecar-expose", created=1),
            _run("wf-b", "rereview-952-persisted-exposure", created=2),
            _run("wf-c", "turn-hooks-recap", created=3),
            _run("wf-d", "turn-hooks-recap", created=4),
            _run("wf-r1", "routine-x", routine="x", created=5),
            _run("wf-r2", "routine-x", routine="x", created=6),
        ),
        node=NODE,
    )
    links = {(link["from"], link["to"], link["kind"]) for link in model["links"]}
    assert ("wf-a", "wf-b", "pr") in links
    assert ("wf-c", "wf-d", "rerun") in links
    assert not any(
        link["from"].startswith("wf-r") or link["to"].startswith("wf-r")
        for link in model["links"]
    )


def test_node_output_is_redacted_and_capped() -> None:
    secret = "ghp_" + "A" * 30
    node = {
        "nodeId": "implement",
        "owner": CLI_DEV,
        "kind": "task",
        "state": "done",
        "outputText": f"pushed with {secret} " + "x" * 2000,
        "reasonRef": "feat/x@abc",
    }
    model = overview.build_model(
        _snapshot(_run("wf-1", "feat-x", nodes=[node])), node=NODE
    )
    output = model["runs"][0]["nodes"][0]["output"]
    assert secret not in output
    assert "[已隐去]" in output
    assert len(output) <= overview.OUTPUT_TEXT_LIMIT


def test_content_key_ignores_build_time_but_not_state() -> None:
    routines = [
        {"name": "r", "owner": SQUIRE, "nextDueMs": 1, "inFlight": [{"runId": "wf-9"}]}
    ]
    first = overview.build_model(
        _snapshot(_run("wf-1", "a", state="running", closed=None), routines=routines),
        node=NODE,
    )
    later = dict(first, observedAt=123_456)
    later["lines"] = [
        {
            **line,
            "routines": [
                {**strip, "nextDue": 999, "inFlight": 0} for strip in line["routines"]
            ],
        }
        for line in first["lines"]
    ]
    assert overview.content_key(first) == overview.content_key(later)
    finished = overview.build_model(
        _snapshot(_run("wf-1", "a"), routines=routines), node=NODE
    )
    assert overview.content_key(first) != overview.content_key(finished)


def test_node_script_registers_the_node_and_cannot_close_its_script_tag() -> None:
    model = overview.build_model(_snapshot(_run("wf-1", "</script><b>x")), node=NODE)
    script = overview.node_script(model)
    assert script.startswith(
        '(window.PAC_NODES = window.PAC_NODES || {})["hq-node"] = '
    )
    assert "</script>" not in script
    payload = json.loads(script.split(" = ", 2)[2].rstrip(";\n").replace("<\\/", "</"))
    assert payload["runs"][0]["name"] == "</script><b>x"


def test_page_lists_every_node_file_once_in_order() -> None:
    page = overview.page_html(["b.js", "a.js", "b.js"])
    assert page.count('<script src="nodes/a.js"></script>') == 1
    assert page.index("nodes/a.js") < page.index("nodes/b.js")
    assert "<!--NODE_SCRIPTS-->" not in page
    assert page.startswith("<!doctype html>")
    # The work-item view asset is a sibling of the page, loaded the same way.
    assert page.count('<script src="work_items_view.js"></script>') == 1
    assert "<!--WORK_VIEW_SCRIPT-->" not in page


class _FakeSpace:
    """orgfs.* as the publish path uses it: import, ls, mkdir, export, checkout."""

    def __init__(
        self, *, peers: dict[str, str] | None = None, missing_dirs: bool = True
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, str] = {}
        self.dirs: set[str] = (
            set() if missing_dirs else {"pac-overview", "pac-overview/nodes"}
        )
        self.peers = peers or {}

    def __call__(self, method: str, params: Any) -> Any:
        params = dict(params)
        self.calls.append((method, params))
        if method == "orgfs.ls":
            if params["path"] not in self.dirs:
                raise _cli_get_services().CliError("ORGFS_NOT_FOUND", "no such path")
            prefix = params["path"] + "/"
            names = {
                path[len(prefix) :] for path in self.files if path.startswith(prefix)
            }
            nodes = [
                {"name": name, "contentState": "arrived"} for name in sorted(names)
            ]
            nodes += [
                {"name": name, "contentState": state}
                for name, state in self.peers.items()
            ]
            return {"nodes": nodes}
        if method == "orgfs.mkdir":
            self.dirs.add(params["path"])
            return {"path": params["path"]}
        if method == "orgfs.write":
            self.files[params["path"]] = base64.b64decode(params["contentB64"]).decode("utf-8")
            return {"path": params["path"]}
        if method == "orgfs.read":
            return {"ok": True}
        if method == "orgfs.checkout":
            return {"path": "/tmp/checkout", "enabled": True}
        raise AssertionError(method)


def _publish(
    space: _FakeSpace,
    model: dict[str, Any],
    tmp_path: Path,
    last: dict[str, str] | None = None,
) -> dict[str, Any]:
    return overview.publish(
        space,
        space_id="space-1",
        base="pac-overview",
        model=model,
        last=last or {},
    )


def test_first_publish_writes_node_file_and_page_then_idle_writes_nothing(
    tmp_path: Path,
) -> None:
    model = overview.build_model(_snapshot(_run("wf-1", "a")), node=NODE)
    space = _FakeSpace()
    first = _publish(space, model, tmp_path)
    assert first["written"] == [
        "pac-overview/work_items_view.js",
        "pac-overview/nodes/hq-node.js",
        "pac-overview/index.html",
    ]
    assert 'src="nodes/hq-node.js"' in space.files["pac-overview/index.html"]
    assert 'src="work_items_view.js"' in space.files["pac-overview/index.html"]
    assert {"pac-overview", "pac-overview/nodes"} <= space.dirs
    assert first["page"] == "/tmp/checkout/pac-overview/index.html"

    space.calls.clear()
    again = _publish(space, dict(model, observedAt=77), tmp_path, last=first["keys"])
    assert again["written"] == []
    assert not [call for call in space.calls if call[0] == "orgfs.write"]


def test_a_new_peer_rewrites_the_page_and_pending_peers_are_pulled(
    tmp_path: Path,
) -> None:
    model = overview.build_model(_snapshot(_run("wf-1", "a")), node=NODE)
    space = _FakeSpace()
    first = _publish(space, model, tmp_path)
    space.peers = {"other-node.js": "pending"}
    space.calls.clear()
    second = _publish(space, model, tmp_path, last=first["keys"])
    assert second["written"] == ["pac-overview/index.html"]
    assert 'src="nodes/other-node.js"' in space.files["pac-overview/index.html"]
    pulls = [params for method, params in space.calls if method == "orgfs.read"]
    assert [params["node"] for params in pulls] == [
        "pac-overview/nodes/other-node.js"
    ]
    assert ("orgfs.checkout", {"spaceId": "space-1", "enabled": True}) in space.calls


def test_publish_exchanges_ipc_bytes_across_two_daemon_runtimes(
    tmp_path: Path, own_daemon_application
) -> None:
    """m5: each daemon publishes bytes and renders both replicated node files."""

    def port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def eventually(action, *, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return action()
            except (AssertionError, DaemonRequestError, OrgFsError) as error:
                last_error = error
                time.sleep(0.05)
        if last_error is not None:
            raise last_error
        raise AssertionError("two-daemon overview did not converge")

    def application(name: str, owner: str) -> DaemonApplication:
        root = tmp_path / name
        return own_daemon_application(
            DaemonApplication(
                state_dir=root / "state",
                socket_path=root / "daemon.sock",
                node_id=name,
                hyprial_home=root / "home",
                owner=owner,
                **_deep_module_daemon_dependencies(),
            )
        )

    def request(app: DaemonApplication):
        def invoke(method, params):
            try:
                return app._handle_orgfs(method, dict(params))
            except DaemonRequestError as error:
                raise _cli_get_services().CliError(error.code, str(error)) from error

        return invoke

    endpoint = f"tcp/127.0.0.1:{port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner_app = application("node-owner", "owner@example.test")
    peer_app = application("node-peer", "peer@example.test")
    try:
        time.sleep(0.3)
        online = {"node-owner", "node-peer"}.__contains__
        owner_runtime = owner_app._require_orgfs_runtime()
        peer_runtime = peer_app._require_orgfs_runtime()
        # Cold loopback replicas stand in for already-admitted private peers.
        bind_admitted(owner_runtime, owner_transport, supplier_online=online)
        bind_admitted(
            peer_runtime, peer_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )
        space_id = owner_app._handle_orgfs("orgfs.create", {"name": "overview"})[
            "space_id"
        ]
        owner_app._handle_orgfs(
            "orgfs.invite",
            {"spaceId": space_id, "user": "user:peer@example.test", "mode": "rw"},
        )
        owner_runtime._meshes[space_id].announce()
        eventually(lambda: peer_runtime.facade.join(space_id))

        owner_model = overview.build_model(
            _snapshot(_run("wf-owner", "owner-work")), node="node-owner"
        )
        owner_first = overview.publish(
            request(owner_app),
            space_id=space_id,
            base="pac-overview",
            model=owner_model,
            last={},
        )

        def peer_sees_owner() -> bool:
            listing = peer_app._handle_orgfs(
                "orgfs.ls",
                {"spaceId": space_id, "path": "pac-overview/nodes"},
            )
            assert any(
                item["name"] == "node-owner.js" for item in listing["nodes"]
            )
            return True

        eventually(peer_sees_owner)
        peer_model = overview.build_model(
            _snapshot(_run("wf-peer", "peer-work")), node="node-peer"
        )
        overview.publish(
            request(peer_app),
            space_id=space_id,
            base="pac-overview",
            model=peer_model,
            last={},
        )

        def owner_sees_peer() -> bool:
            listing = owner_app._handle_orgfs(
                "orgfs.ls",
                {"spaceId": space_id, "path": "pac-overview/nodes"},
            )
            assert any(item["name"] == "node-peer.js" for item in listing["nodes"])
            return True

        eventually(owner_sees_peer)
        owner_second = overview.publish(
            request(owner_app),
            space_id=space_id,
            base="pac-overview",
            model=owner_model,
            last=owner_first["keys"],
        )
        assert owner_second["nodes"] == ["node-owner.js", "node-peer.js"]
        assert owner_second["written"] == ["pac-overview/index.html"]

        peer_script = owner_app._handle_orgfs(
            "orgfs.read",
            {
                "spaceId": space_id,
                "node": "pac-overview/nodes/node-peer.js",
                "waitSeconds": 3,
            },
        )
        page = owner_app._handle_orgfs(
            "orgfs.read",
            {
                "spaceId": space_id,
                "node": "pac-overview/index.html",
                "waitSeconds": 3,
            },
        )
        assert '"node-peer"' in base64.b64decode(peer_script["contentB64"]).decode()
        page_text = base64.b64decode(page["contentB64"]).decode()
        assert 'src="nodes/node-owner.js"' in page_text
        assert 'src="nodes/node-peer.js"' in page_text
    finally:
        close_application(peer_app)
        close_application(owner_app)
        peer_transport.close()
        owner_transport.close()


def test_overview_show_through_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "ps": {"daemon": {"nodeId": NODE}},
        "workflow.list": {
            "runs": [dict(_run("wf-1", "cli-dev-AZ-ready-wait"), createdAtMs=10**13)]
        },
        "workflow.status": {
            "nodes": [
                {
                    "nodeId": "implement",
                    "owner": CLI_DEV,
                    "kind": "task",
                    "state": "done",
                }
            ],
            "edges": [],
            "roster": {"nodes": [{"nodeId": "implement", "role": "execute"}]},
        },
        "workflow.node.inspect": {"node": {"outputText": "done", "flagSetAt": 5}},
        "routine.list": {"routines": []},
        "org.list": {"orgs": [{"org": "group-acme", "spaceId": "space-work"}]},
        "orgfs.ls": {"nodes": []},
    }
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake(method: str, params: Any = None, **_: Any) -> Any:
        seen.append((method, dict(params or {})))
        return responses[method]

    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    monkeypatch.setattr(
        "hyprial.shell.impl.cli.commands.workflow.overview._identity",
        lambda *a, **k: {"actor": "user:owner"},
    )
    result = CliRunner().invoke(_cli_root_app, ["workflow", "overview", "show", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["node"] == NODE
    assert payload["runs"][0]["nodes"][0]["output"] == "done"
    assert payload["runs"][0]["nodes"][0]["role"] == "execute"
    listed = next(params for method, params in seen if method == "workflow.list")
    assert listed["all"] is True


def test_page_leaves_out_node_file_names_a_writer_could_use_to_escape() -> None:
    hostile = [
        'x" onload="alert(1).js',
        "a/../b.js",
        "-x.js",
        'n"><script>alert(1)</script>.js',
    ]
    page = overview.page_html(["hq-node.js", *hostile])
    assert '<script src="nodes/hq-node.js"></script>' in page
    for name in hostile:
        assert name not in page


def test_publish_does_not_list_a_hostile_peer_file_on_the_page(tmp_path: Path) -> None:
    model = overview.build_model(_snapshot(_run("wf-1", "a")), node=NODE)
    space = _FakeSpace(peers={'evil" onerror="x.js': "arrived"})
    result = _publish(space, model, tmp_path)
    assert result["nodes"] == ["hq-node.js"]
    assert "evil" not in space.files["pac-overview/index.html"]


def test_publish_writes_the_work_item_view_asset_byte_identical(
    tmp_path: Path,
) -> None:
    model = overview.build_model(_snapshot(_run("wf-1", "a")), node=NODE)
    space = _FakeSpace()
    _publish(space, model, tmp_path)
    asset = space.files["pac-overview/work_items_view.js"]
    assert asset == overview.work_view_asset()
    assert "PAC_WORK_ITEMS" in asset


def test_publishing_the_same_space_to_another_path_writes_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    space = _FakeSpace()
    listed = {"runs": [dict(_run("wf-1", "a"), createdAtMs=10**13)]}

    def fake(method: str, params: Any = None, **_: Any) -> Any:
        if method.startswith("orgfs."):
            return space(method, params or {})
        return {
            "org.list": {"orgs": [{"org": "group-acme", "spaceId": "space-work"}]},
            "ps": {"daemon": {"nodeId": NODE}},
            "workflow.list": listed,
            "workflow.status": {"nodes": [], "edges": []},
            "routine.list": {"routines": []},
        }[method]

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: state)
    monkeypatch.setattr(
        "hyprial.shell.impl.cli.commands.workflow.overview._identity",
        lambda *a, **k: {"actor": "user:owner"},
    )

    def publish(path: str) -> list[str]:
        result = CliRunner().invoke(
            _cli_root_app,
            [
                "workflow",
                "overview",
                "publish",
                "space-1",
                "--path",
                path,
                "--org",
                "group-acme",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)["written"]

    assert publish("first") == [
        "first/work_items_view.js",
        "first/nodes/hq-node.js",
        "first/index.html",
    ]
    assert publish("second") == [
        "second/work_items_view.js",
        "second/nodes/hq-node.js",
        "second/index.html",
    ]
    assert publish("first") == []  # unchanged at that path


def test_every_graph_lands_in_exactly_one_line() -> None:
    hq = "agent:owner@example.test:hq-node:hq-adjutant"
    model = overview.build_model(
        _snapshot(
            _run("wf-a", "cli-dev-AZ-ready-wait", created=1),
            _run("wf-b", "review-970-ready-wait", sender=hq, created=2),
            _run("wf-c", "lonely-task", created=3),
            _run("wf-r", "routine-x", routine="x", created=4),
            _run("wf-u", "routine-unregistered", routine="gone", created=5),
            routines=[{"name": "x", "owner": SQUIRE}],
        ),
        node=NODE,
    )
    placed: list[str] = []
    for line in model["lines"]:
        placed += line["runs"]
        for strip in line["routines"]:
            placed += strip["runs"]
    assert sorted(placed) == sorted(run["id"] for run in model["runs"])
    by_line = {line["id"] for line in model["lines"]}
    assert all(run["line"] in by_line for run in model["runs"])


def test_page_normalises_line_kinds_from_other_members() -> None:
    page = overview.page_html(["a.js"])
    assert 'const LINE_KINDS = new Set(["mission", "task", "misc", "routine"]);' in page
    assert 'class="lane lane-${line.kind}' in page


def test_pr_numbers_reach_four_digits_but_years_are_not_prs() -> None:
    from hyprial.daemon.impl.pac.views.overview import _signature

    assert _signature("pac-overview-lanes-1009-abc123abc123")[1] == {"1009"}
    assert _signature("fix-996-login")[1] == {"996"}
    assert _signature("report-2026-09-29")[1] == set()


MISSION = {
    "id": "M-owner-0001",
    "title": "Login hints",
    "status": "active",
    "owner": "user:owner",
    "keywords": ["login"],
    "pacs": ["wf-a", "wf-r", "wf-elsewhere"],
    "path": "work/M-owner-0001.md",
    "version": "v9",
}


def _mission_model() -> dict[str, Any]:
    return overview.build_model(
        _snapshot(
            _run("wf-a", "cli-dev-AZ-ready-wait", created=1),
            _run("wf-b", "review-970-ready-wait", created=2),
            _run("wf-c", "cli-dev-AZ2-ready-wait-fix", created=3),
            _run("wf-r", "routine-x", routine="x", created=4),
            routines=[{"name": "x", "owner": SQUIRE}],
        ),
        node=NODE,
        missions=[MISSION],
    )


def test_listed_graphs_sit_on_their_mission_line_under_the_mission_owner() -> None:
    model = _mission_model()
    lines = {line["id"]: line for line in model["lines"]}
    mission_line = lines[f"{NODE}|user:owner|m:M-owner-0001"]
    assert mission_line["kind"] == "mission"
    assert mission_line["runs"] == ["wf-a"]
    assert mission_line["title"] == "Login hints"
    assert mission_line["mission"] == {"id": "M-owner-0001", "status": "active"}
    # The unlisted relatives are still inferred, without the listed graph.
    task = [line for line in model["lines"] if line["kind"] == "task"]
    assert [line["runs"] for line in task] == [["wf-b", "wf-c"]]
    # A routine run stays on its routine line even if a mission lists it.
    runs = {run["id"]: run for run in model["runs"]}
    assert runs["wf-r"]["line"].endswith("|r:x")
    placed = [g for line in model["lines"] for g in line["runs"]]
    placed += [g for line in model["lines"] for strip in line["routines"] for g in strip["runs"]]
    assert sorted(placed) == sorted(runs)


def test_only_five_mission_fields_reach_the_page() -> None:
    model = _mission_model()
    assert model["missions"] == [
        {
            "id": "M-owner-0001",
            "title": "Login hints",
            "status": "active",
            "owner": "user:owner",
            "pacs": ["wf-a"],
        }
    ]
    text = overview.node_script(model)
    assert "keywords" not in text and "work/M-owner-0001.md" in text
    assert "v9" not in text
    # The work-items page entry carries every linked graph, in or out of
    # the run window, while missions[] stays five fields and window-linked.
    assert model["workItems"][0]["pacs"] == ["wf-a", "wf-elsewhere", "wf-r"]
    assert model["workItems"][0]["pacsInWindow"] == ["wf-a", "wf-r"]
    assert "wf-elsewhere" in text


def test_without_missions_the_model_is_unchanged() -> None:
    snapshot = _snapshot(_run("wf-a", "cli-dev-AZ-ready-wait", created=1))
    plain = overview.build_model(snapshot, node=NODE)
    assert plain["missions"] == []
    assert overview.build_model(snapshot, node=NODE, missions=[]) == plain


def _cli_responses(extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "org.list": {"orgs": [{"org": "group-acme", "spaceId": "space-m"}]},
        "ps": {"daemon": {"nodeId": NODE}},
        "workflow.list": {"runs": [dict(_run("wf-a", "fix-login"), createdAtMs=10**13)]},
        "workflow.status": {"nodes": [], "edges": [], "roster": {"nodes": []}},
        "routine.list": {"routines": []},
        **extra,
    }


def _invoke_show(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> dict[str, Any]:
    def fake(method: str, params: Any = None, **_: Any) -> Any:
        value = responses[method]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    monkeypatch.setattr(
        "hyprial.shell.impl.cli.commands.workflow.overview._identity",
        lambda *a, **k: {"actor": "user:owner"},
    )
    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "overview", "show", "--org", "group-acme", "--json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_overview_show_groups_by_missions_read_from_the_space(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "## M-owner-0001 Login hints\n\n```yaml\nid: M-owner-0001\nstatus: active\n"
        "owner: user:owner\nkeywords: [login]\npacs:\n  - wf-a\n```\n"
    )
    payload = _invoke_show(
        monkeypatch,
        _cli_responses(
            {
                "orgfs.ls": {"nodes": [{"name": "M-owner-0001.md", "node_id": "n1"}]},
                "orgfs.read": {"text": text, "version": "v1"},
            }
        ),
    )
    assert payload["runs"][0]["line"] == f"{NODE}|user:owner|m:M-owner-0001"
    assert "missionsError" not in payload


def test_an_unreadable_work_directory_leaves_lines_inferred(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _invoke_show(
        monkeypatch, _cli_responses({"orgfs.ls": _cli_get_services().CliError("ORGFS_NOT_FOUND", "no missions dir")})
    )
    assert payload["runs"][0]["line"] == f"{NODE}|{CLI_DEV}|misc"
    assert "no missions dir" in payload["missionsError"]


def test_overview_flags_raw_invalid_work_items(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _invoke_show(
        monkeypatch,
        _cli_responses(
            {
                "orgfs.ls": {
                    "nodes": [{"name": "M-owner-0001.md", "node_id": "n1"}]
                },
                "orgfs.read": {
                    "text": "## broken\n\n```yaml\nid: M-owner-0001\nstatus: bogus\n```\n",
                    "version": "v1",
                },
            }
        ),
    )

    assert payload["invalidWorkItems"] == [
        {
            "path": "work/M-owner-0001.md",
            "reason": "status must be one of todo, doing, blocked, done, dropped",
        }
    ]


def test_overview_flags_unverified_operator_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "## M-owner-0001 Login hints\n\n```yaml\nid: M-owner-0001\nstatus: done\n"
        "owner: user:owner\nchecklist: []\nkeywords: [login]\npacs: [wf-a]\n"
        "ownerActions:\n  - {action: done, actor: 'user:owner', verified: false}\n```\n\n"
        "## Log\n- 2026-10-07T12:00:00+00:00 user:owner: done (unverified operator)\n"
    )
    payload = _invoke_show(
        monkeypatch,
        _cli_responses(
            {
                "orgfs.ls": {
                    "nodes": [{"name": "M-owner-0001.md", "node_id": "n1"}]
                },
                "orgfs.read": {"text": text, "version": "v1"},
            }
        ),
    )

    assert payload["missions"][0]["unverifiedOperator"] is True


# --- page behaviour (Allen 2026-09-30: explanations behind tips; idle agent lines fold) ---

_PAGE = resources.files('hyprial.daemon.impl.pac.views').joinpath("overview_page.html").read_text("utf-8")
_HOUR = 3600_000
_NOW = 100 * _HOUR


def _lane_activity_js() -> str:
    match = re.search(
        r"// ---- lane activity:.*?\n(.*?)// ---- end lane activity ----", _PAGE, re.S
    )
    assert match, "the page lost its lane-activity block"
    return match.group(1)


def _work_item_warnings_js() -> str:
    match = re.search(
        r"// ---- work item warnings:.*?\n(.*?)// ---- end work item warnings ----",
        _PAGE,
        re.S,
    )
    assert match, "the page lost its work-item warning renderer"
    return match.group(1)


def test_page_renders_unverified_and_invalid_work_item_fields() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    model = {
        "missions": [{"id": "M-owner-0001", "unverifiedOperator": True}],
        "invalidWorkItems": [
            {"path": "work/M-broken-0002.md", "reason": "owner action missing"}
        ],
    }
    script = (
        "const esc = s => String(s ?? '');\n"
        + _work_item_warnings_js()
        + f"process.stdout.write(workItemWarnings({json.dumps(model)}));\n"
    )
    rendered = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True
    ).stdout

    assert "M-owner-0001" in rendered
    assert "work/M-broken-0002.md" in rendered
    assert "owner action missing" in rendered


def test_page_qualifies_verified_owner_actions_as_lax() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    model = {
        "missions": [
            {
                "id": "M-owner-0001",
                "unverifiedOperator": False,
                "ownerActions": [
                    {
                        "action": "done",
                        "verified": True,
                        "verification": "verified (LAX same-uid)",
                    }
                ],
            }
        ],
        "invalidWorkItems": [],
    }
    script = (
        "const esc = s => String(s ?? '');\n"
        + _work_item_warnings_js()
        + f"process.stdout.write(workItemWarnings({json.dumps(model)}));\n"
    )
    rendered = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True
    ).stdout

    assert "verified (LAX same-uid)" in rendered
    assert "M-owner-0001" in rendered


def _has_tasks(runs: list[dict[str, Any]], routine_runs: list[dict[str, Any]]) -> bool:
    """Run the page's own lineHasTasks in node for one line at _NOW, 24h window."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    line = {
        "runs": [r["id"] for r in runs],
        "routines": [{"runs": [r["id"] for r in routine_runs]}] if routine_runs else [],
    }
    script = (
        _lane_activity_js()
        + f"const runs = new Map({json.dumps([[r['id'], r] for r in runs + routine_runs])});\n"
        + f"process.stdout.write(JSON.stringify(lineHasTasks({json.dumps(line)}, runs, {_NOW}, {24 * _HOUR})));\n"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _graph(
    gid: str, created_h: float, state: str, closed_h: float | None
) -> dict[str, Any]:
    closed = None if closed_h is None else int(closed_h * _HOUR)
    return {
        "id": gid,
        "created": int(created_h * _HOUR),
        "state": state,
        "closed": closed,
        "progress": closed,
    }


@pytest.mark.parametrize(
    ("runs", "routine_runs", "expected"),
    [
        ([], [], False),
        # Finished before the window opened: nothing to show, the line folds.
        ([_graph("old", 50, "completed", 60)], [], False),
        # Created after the chosen moment: not there yet.
        ([_graph("later", 101, "running", None)], [], False),
        ([_graph("live", 10, "running", None)], [], True),
        # Only finished graphs, but finished inside the window: still has tasks.
        ([_graph("done", 70, "completed", 90)], [], True),
        ([_graph("failed", 95, "failed", 96)], [], True),
        ([], [_graph("tick", 99, "completed", 99.1)], True),
    ],
    ids=[
        "empty",
        "closed-before-window",
        "not-yet-created",
        "running",
        "closed-in-window",
        "failed-in-window",
        "routine-tick",
    ],
)
def test_a_line_has_tasks_only_when_something_is_active_in_the_window(
    runs: list[dict[str, Any]], routine_runs: list[dict[str, Any]], expected: bool
) -> None:
    # Mutation check: replacing activeIn's last line with `return true;` makes
    # closed-before-window red; dropping the routine runs from lineHasTasks
    # makes routine-tick red.
    assert _has_tasks(runs, routine_runs) is expected


def test_idle_agent_lines_fold_behind_one_row_and_people_lines_do_not() -> None:
    assert (
        "const busy = g.info.human ? g.rows : g.rows.filter(row => hasTasks(row.line));"
        in _PAGE
    )
    assert (
        "const idle = g.info.human ? [] : g.rows.filter(row => !hasTasks(row.line));"
        in _PAGE
    )
    assert 'data-fold="${esc(g.key)}" aria-expanded="${open}"' in _PAGE
    assert '${open ? idle.map(row => lane(row.line, from, to)).join("") : ""}' in _PAGE


def test_explanations_start_hidden_behind_tip_buttons() -> None:
    assert '<div class="note" id="tip-note" data-tip-body="note" hidden></div>' in _PAGE
    assert 'parts.push(tip("note", "这一页怎么看"));' in _PAGE
    assert (
        'tipBody("beats", "每一格是一次运行，颜色是结果；点一格打开那次的 PAC 图。")'
        in _PAGE
    )
    # A tip button is not a breadcrumb: only data-go buttons navigate.
    assert 'e.target.closest("button[data-go]")' in _PAGE
