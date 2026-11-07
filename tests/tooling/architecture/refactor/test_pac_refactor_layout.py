"""PAC integration layout/port regressions that cannot open a transport."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[4]
PAC = ROOT / "src/hyprial/daemon/impl/pac"


def test_request_port_error_keeps_presentation_exception_contract():
    from hyprial.kernel import RequestPortError

    class PresentationError(RequestPortError):
        pass

    data = {"path": "missing"}
    error = PresentationError("ORGFS_PATH_NOT_FOUND", "not found", data)
    assert isinstance(error, Exception)
    assert not isinstance(error, RuntimeError)
    assert str(error) == "not found"
    assert error.code == "ORGFS_PATH_NOT_FOUND"
    assert error.data is data
    with pytest.raises(RequestPortError) as captured:
        raise error
    assert captured.value is error


def test_leaf_code_is_grouped_by_real_responsibility():
    expected = {
        "contracts": {"workflow.py", "bindings.py", "restore.py", "principals.py", "roster.py", "delivery.py"},
        "storage": {"store.py", "journal.py", "legacy.py", "legacy_barrier.sql", "assignments.py"},
        "graphs": {"edits.py", "reactor.py", "events.py", "subscription.py", "projection.py"},
        "actors": {"daemon.py", "gc.py", "assignments.py", "reconcile.py"},
        "workflows": {"graphs.py", "outputs.py"},
        "views": {"context.py", "missions.py", "overview.py", "overview_page.html"},
    }
    for name, files in expected.items():
        directory = PAC / name
        assert directory.is_dir(), f"missing semantic responsibility: {name}"
        assert files <= {path.name for path in directory.iterdir() if path.is_file()}
    for old in ("store", "graph", "reactor", "subscription", "context", "workflow_schema", "projection", "delivery_guard", "legacy_workflows", "overview", "journal", "stream", "agent_task", "remote_binding", "resolve", "restore_facts", "missions", "workflow_graph", "workflow_output", "workflow_roster"):
        assert not (PAC / old).exists(), f"obsolete same-named leaf package: {old}"


def test_owned_modules_meet_limits_and_have_no_upward_imports():
    owned = [PAC, ROOT / "src/hyprial/daemon/impl/dispatch", ROOT / "src/hyprial/biz/impl/routine"]
    for base in owned:
        for directory in [base, *[path for path in base.rglob("*") if path.is_dir() and "__pycache__" not in path.parts]]:
            files = [path for path in directory.iterdir() if path.is_file()]
            code = [path for path in files if path.suffix in {".py", ".html"}]
            if code:
                assert len(files) <= 7, directory
            for path in code:
                assert len(path.read_bytes().splitlines()) <= 1000, path
                if path.suffix != ".py" or "/biz/" in path.as_posix():
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        assert not node.module.startswith(("hyprial.cli", "hyprial.shell", "hyprial.biz")), (path, node.lineno, node.module)


def test_owned_cross_domain_consumers_use_public_faces():
    for relative in ("daemon/impl/pac", "daemon/impl/dispatch", "biz/impl/routine"):
        origin = relative.split("/", 1)[0]
        for path in (ROOT / "src/hyprial" / relative).rglob("*.py"):
            if path.name == "cli.py" or "__pycache__" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("hyprial."):
                    domain = node.module.split(".")[1]
                    if domain != origin:
                        assert ".impl" not in node.module, (path, node.lineno, node.module)


def test_overview_uses_shared_request_error_without_cli_import():
    path = PAC / "views/overview.py"
    source = path.read_text()
    assert "from hyprial.kernel import RequestPortError" in source
    assert "except RequestPortError:" in source
    assert "from hyprial.cli import CliError" not in source


def test_overview_request_port_recovers_missing_directory_only():
    from hyprial.daemon.impl.pac.views.overview import _ensure_dir
    from hyprial.kernel import RequestPortError

    calls = []

    def missing(method, parameters):
        calls.append((method, parameters))
        if method == "orgfs.ls":
            raise RequestPortError("ORGFS_PATH_NOT_FOUND", "missing")

    _ensure_dir(missing, "space", "overview")
    assert [method for method, _ in calls] == ["orgfs.ls", "orgfs.mkdir"]
    assert calls[0][1] == calls[1][1] == {"spaceId": "space", "path": "overview"}

    def unrelated(method, parameters):
        raise ValueError("not a request error")

    with pytest.raises(ValueError, match="not a request error"):
        _ensure_dir(unrelated, "space", "overview")


def test_overview_resource_loader_targets_its_real_package():
    source = (PAC / "views/overview.py").read_text()
    assert "resources.files('hyprial.daemon.impl.pac.views')" in source
    from importlib import resources
    page = resources.files("hyprial.daemon.impl.pac.views").joinpath("overview_page.html")
    assert page.read_bytes() == (PAC / "views/overview_page.html").read_bytes()


@pytest.mark.parametrize("unsafe", [
    {"listen": ("tcp/0.0.0.0:7447",)},
    {"connect": ("tcp/example.invalid:7447",)},
    {"gossip_scouting": True},
    {"multicast_scouting": True},
])
def test_startup_guard_rejects_network_before_native_open(unsafe):
    source = (ROOT / 'tests/daemon/pac/graphs/test_pac_graph_db_created_on_startup.py').read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_assert_loopback_transport")
    namespace = {"ZenohConfig": object}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "startup-guard", "exec"), namespace)
    configuration = {"listen": (), "connect": (), "gossip_scouting": False, "multicast_scouting": False}
    configuration.update(unsafe)
    with pytest.raises(AssertionError):
        namespace["_assert_loopback_transport"](SimpleNamespace(**configuration))
    configuration.update(listen=("tcp/127.0.0.1:7447", "tcp/[::1]:7447"), connect=(),
                         gossip_scouting=False, multicast_scouting=False)
    namespace["_assert_loopback_transport"](SimpleNamespace(**configuration))
