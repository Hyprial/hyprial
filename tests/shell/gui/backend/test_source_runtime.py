"""Private runtime and child contracts for initialized source GUI sessions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile

import pytest

from hyprial.kernel import harden_private_path
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import source as backend_source
from hyprial.shell.impl.gui.backend.admission import SourceAdmission
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.worktree import AutoGuiWorktree, DependencyInstall


REPOSITORY = Path(__file__).resolve().parents[4]
OWNER = "runtime-owner"
NODE = "runtime-node"
PRINCIPAL = f"user:{OWNER}"
SECRET_VALUE = "mock-private-bearer"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    harden_private_path(path, mode=0o700)
    return path


def _admission(
    tmp_path: Path, driver: str = "daemon", *, external_state: bool = False
) -> SourceAdmission:
    if driver == "contract-mock":
        return SourceAdmission(
            driver=driver,
            owner="contract-mock",
            node="local",
            principal="user:contract-mock",
            home=None,
            state_dir=tmp_path / "state",
            socket_path=None,
        )
    home = _private_dir(tmp_path / "home")
    state = tmp_path / "external-state" if external_state else home / "state"
    return SourceAdmission(
        driver=driver,
        owner=OWNER,
        node=NODE,
        principal=PRINCIPAL,
        home=home,
        state_dir=state,
        socket_path=state / "daemon.sock",
    )


def _plan(
    tmp_path: Path,
    *,
    driver: str = "daemon",
    mode: str = "normal",
    external_state: bool = False,
) -> launcher.GuiLaunchPlan:
    admission = _admission(tmp_path, driver, external_state=external_state)
    runtime_name = "daemon" if driver == "daemon" else "contract-mock"
    project = launcher.GuiProject(
        home=tmp_path / "gui",
        worktree=REPOSITORY,
        branch="test",
        node="/fake/node",
        npm="/fake/npm",
        node_version="v24",
        npm_version="11",
        scripts={"start": "node server.mjs", "dev": "vite", "build": "vite build"},
    )
    return launcher.GuiLaunchPlan(
        mode=mode,
        project=project,
        host="127.0.0.1",
        port=4173,
        url="http://127.0.0.1:4173/",
        backend_endpoint=None,
        backend_owned=True,
        frontend_argv=("/fake/npm", "start"),
        env={
            "PATH": "/usr/bin",
            "HYPRIAL_HOME": str(tmp_path / "home"),
            "HARNESS_STATE_DIR": str(admission.state_dir),
            "HYPRIAL_OWNER": OWNER,
            "HYPRIAL_GUI_UPSTREAM_TOKEN": "inherited-token",
            "HYPRIAL_GUI_LAUNCH_GRANT": "inherited-grant",
            "HYPRIAL_GUI_HOME": str(tmp_path / "gui"),
            "HYPRIAL_GUI_LAUNCH_TTL_MS": "1000",
            "HARNESS_SOCKET_PATH": str(tmp_path / "wrong.sock"),
        },
        open_browser=False,
        driver=driver,
        admission=admission,
        runtime_dir=admission.runtime_root / runtime_name,
    )


def test_runtime_is_private_and_clears_only_stale_session_files(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    runtime = plan.runtime_dir
    assert runtime is not None
    runtime.mkdir(mode=0o700, parents=True)
    for name in (
        "launch.url",
        "launch.ready",
        "request-http.json",
        "server.json",
        "server.lock",
        "frontend-authority.json",
        "host.log",
        "assets.log",
        "mock.log",
    ):
        (runtime / name).write_text("stale", encoding="utf-8")

    lease = backend_source.prepare_runtime(plan)
    try:
        assert runtime.stat().st_mode & 0o777 == 0o700
        assert (runtime / "supervisor.lock").stat().st_mode & 0o077 == 0
        for name in (
            "launch.url",
            "launch.ready",
            "request-http.json",
            "server.json",
            "server.lock",
            "frontend-authority.json",
            "host.log",
            "assets.log",
            "mock.log",
        ):
            assert not (runtime / name).exists()
    finally:
        lease.close()


def test_runtime_has_no_authority_file(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    lease = backend_source.prepare_runtime(plan)
    try:
        assert plan.runtime_dir is not None
        assert sorted(entry.name for entry in plan.runtime_dir.iterdir()) == ["supervisor.lock"]
    finally:
        lease.close()


def test_concurrent_supervisor_is_rejected_then_released(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    first = backend_source.prepare_runtime(plan)
    try:
        with pytest.raises(GuiError) as caught:
            backend_source.prepare_runtime(plan)
        assert caught.value.code == "GUI_RUNTIME_BUSY"
    finally:
        first.close()

    second = backend_source.prepare_runtime(plan)
    second.close()


def test_contract_mock_resolves_the_source_root_behind_an_auto_worktree(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    entry = source / "gui" / "contract-mock" / "serve.ts"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    openapi = source / "contract" / "daemon-request" / "openapi.json"
    openapi.parent.mkdir(parents=True)
    openapi.write_text("{}", encoding="utf-8")
    auto_home = source / ".worktrees" / "gui-dev-auto" / "gui"
    project = replace(
        _plan(tmp_path, driver="contract-mock").project,
        home=auto_home,
        worktree=auto_home.parent,
    )
    auto = AutoGuiWorktree(
        source=source,
        path=auto_home.parent,
        branch="work/gui-dev-auto",
        base="a" * 40,
        home=auto_home,
        created=False,
        install=DependencyInstall(command=(), cwd=auto_home, ran=False),
    )
    plan = replace(
        _plan(tmp_path, driver="contract-mock", mode="dev"),
        project=project,
        auto_worktree=auto,
    )

    argv, cwd, _environment = backend_source.mock_invocation(plan)

    assert argv[:2] == (plan.project.node, str(entry))
    assert cwd == source / "gui"


def test_contract_mock_missing_source_names_the_selected_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plan = replace(
        _plan(tmp_path, driver="contract-mock"),
        project=replace(_plan(tmp_path, driver="contract-mock").project, worktree=source),
    )

    with pytest.raises(GuiError) as caught:
        backend_source.mock_invocation(plan)

    assert caught.value.code == "GUI_MOCK_UNAVAILABLE"
    assert caught.value.data == {"worktree": str(source)}


def test_contract_mock_needs_the_openapi_document_beside_the_entry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    entry = source / "gui" / "contract-mock" / "serve.ts"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    base = _plan(tmp_path, driver="contract-mock")
    plan = replace(base, project=replace(base.project, worktree=source))

    with pytest.raises(GuiError) as caught:
        backend_source.mock_invocation(plan)

    assert caught.value.code == "GUI_MOCK_UNAVAILABLE"
    assert caught.value.data == {"worktree": str(source)}


def _fallback_plan(tmp_path: Path) -> launcher.GuiLaunchPlan:
    """A plan whose runtime is the root outside any home (no state directory known)."""
    base = _plan(tmp_path)
    return replace(
        base,
        admission=replace(base.admission, state_dir=None),
        runtime_dir=tmp_path / "shared-tmp" / "hyprial-gui-test" / "daemon",
    )


def test_runtime_rejects_a_symlink_inside_the_home(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    runtime = plan.runtime_dir
    assert runtime is not None
    runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    runtime.symlink_to(target, target_is_directory=True)

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"


def test_runtime_inside_the_home_does_not_repeat_the_mode_fence(tmp_path: Path) -> None:
    """The home root is the fence; its runtime subdirectory is not re-checked."""
    plan = _plan(tmp_path)
    assert backend_source._runtime_inside_home(plan) is True
    runtime = plan.runtime_dir
    assert runtime is not None
    runtime.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)

    lease = backend_source.prepare_runtime(plan)
    lease.close()
    assert runtime.stat().st_mode & 0o777 == 0o755


def test_symlinked_state_dir_inside_home_cannot_escape_the_home_boundary(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    assert plan.admission is not None and plan.admission.state_dir is not None
    outside = tmp_path / "shared-state"
    outside.mkdir(mode=0o755)
    outside.chmod(0o755)
    plan.admission.state_dir.symlink_to(outside, target_is_directory=True)

    assert backend_source._runtime_inside_home(plan) is False
    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert not (outside / "gui").exists()


@pytest.mark.parametrize(
    ("driver", "external_state"),
    [("daemon", True), ("contract-mock", False)],
)
def test_external_or_mock_state_dir_is_created_private_outside_the_home(
    tmp_path: Path, driver: str, external_state: bool
) -> None:
    plan = _plan(tmp_path, driver=driver, external_state=external_state)
    assert plan.admission is not None and plan.admission.state_dir is not None
    assert plan.runtime_dir is not None
    assert backend_source._runtime_inside_home(plan) is False

    lease = backend_source.prepare_runtime(plan)
    lease.close()

    for fenced in (plan.admission.state_dir, plan.runtime_dir.parent, plan.runtime_dir):
        assert fenced.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("driver", "external_state"),
    [("daemon", True), ("contract-mock", False)],
)
@pytest.mark.parametrize("which", ["state", "gui", "runtime"])
def test_external_or_mock_state_dir_rejects_unsafe_permissions(
    tmp_path: Path, driver: str, external_state: bool, which: str
) -> None:
    plan = _plan(tmp_path, driver=driver, external_state=external_state)
    assert plan.admission is not None and plan.admission.state_dir is not None
    runtime = plan.runtime_dir
    assert runtime is not None
    state = plan.admission.state_dir
    for path in (state, runtime.parent, runtime):
        _private_dir(path)
    targets = {"state": state, "gui": runtime.parent, "runtime": runtime}
    unsafe = targets[which]
    unsafe.chmod(0o755)

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert caught.value.data == {"path": str(unsafe), "reason": "unsafe-mode"}
    assert not (runtime / "supervisor.lock").exists()


@pytest.mark.parametrize(
    ("driver", "external_state"),
    [("daemon", True), ("contract-mock", False)],
)
@pytest.mark.parametrize("which", ["state", "gui", "runtime"])
def test_external_or_mock_state_dir_rejects_symlinks(
    tmp_path: Path, driver: str, external_state: bool, which: str
) -> None:
    plan = _plan(tmp_path, driver=driver, external_state=external_state)
    assert plan.admission is not None and plan.admission.state_dir is not None
    runtime = plan.runtime_dir
    assert runtime is not None
    state = plan.admission.state_dir
    target = _private_dir(tmp_path / "elsewhere")
    if which == "state":
        state.symlink_to(target, target_is_directory=True)
    elif which == "gui":
        _private_dir(state)
        runtime.parent.symlink_to(target, target_is_directory=True)
    else:
        _private_dir(state)
        _private_dir(runtime.parent)
        runtime.symlink_to(target, target_is_directory=True)

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert not (target / "supervisor.lock").exists()


@pytest.mark.parametrize("driver", ["daemon", "contract-mock"])
def test_external_state_rejects_a_symlinked_ancestor(
    tmp_path: Path, driver: str
) -> None:
    plan = _plan(tmp_path, driver=driver, external_state=driver == "daemon")
    outside = _private_dir(tmp_path / "outside")
    linked = tmp_path / "linked-parent"
    linked.symlink_to(outside, target_is_directory=True)
    state = linked / "state"
    runtime = state / ("daemon" if driver == "daemon" else "contract-mock")
    admission = replace(plan.admission, state_dir=state)
    plan = replace(plan, admission=admission, runtime_dir=runtime)

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert not (outside / "state").exists()


@pytest.mark.parametrize(
    ("driver", "external_state"),
    [("daemon", True), ("contract-mock", False)],
)
def test_external_or_mock_state_dir_rejects_reparse_and_acl_violations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, driver: str, external_state: bool
) -> None:
    plan = _plan(tmp_path, driver=driver, external_state=external_state)
    assert plan.admission is not None and plan.admission.state_dir is not None
    state = _private_dir(plan.admission.state_dir)

    monkeypatch.setattr(backend_source, "private_tree_violation", lambda p, _: "unsafe-acl")
    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert caught.value.data == {"path": str(state), "reason": "unsafe-acl"}

    monkeypatch.undo()
    monkeypatch.setattr(backend_source, "path_is_reparse", lambda _info: True)
    with pytest.raises(GuiError) as caught_reparse:
        backend_source.prepare_runtime(plan)
    assert caught_reparse.value.code == "GUI_RUNTIME_UNAVAILABLE"


def test_fallback_runtime_outside_the_home_is_created_private(tmp_path: Path) -> None:
    plan = _fallback_plan(tmp_path)
    runtime = plan.runtime_dir
    assert runtime is not None
    runtime.parent.parent.mkdir()

    lease = backend_source.prepare_runtime(plan)
    lease.close()
    assert runtime.parent.stat().st_mode & 0o777 == 0o700
    assert runtime.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("which", ["root", "runtime"])
def test_fallback_runtime_outside_the_home_keeps_the_hard_gate(
    tmp_path: Path, which: str
) -> None:
    plan = _fallback_plan(tmp_path)
    runtime = plan.runtime_dir
    assert runtime is not None
    runtime.parent.parent.mkdir()
    runtime.parent.mkdir(mode=0o700)
    runtime.parent.chmod(0o700)
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    unsafe = runtime.parent if which == "root" else runtime
    unsafe.chmod(0o755)

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert caught.value.data == {"path": str(unsafe), "reason": "unsafe-mode"}
    assert not (runtime / "supervisor.lock").exists()


def test_fallback_runtime_root_must_not_be_a_symlink(tmp_path: Path) -> None:
    plan = _fallback_plan(tmp_path)
    runtime = plan.runtime_dir
    assert runtime is not None
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    runtime.parent.parent.mkdir()
    runtime.parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert not (target / "daemon").exists()


def test_fallback_tempdir_symlink_is_rejected_without_creating_outside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    temp_link = tmp_path / "tmp-link"
    temp_link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(temp_link))
    monkeypatch.setenv("TEMP", str(temp_link))
    monkeypatch.setenv("TMP", str(temp_link))
    monkeypatch.setattr(tempfile, "tempdir", None)

    base = _plan(tmp_path)
    admission = replace(base.admission, state_dir=None)
    plan = replace(
        base,
        admission=admission,
        runtime_dir=admission.runtime_root / "daemon",
    )

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert not list(outside.iterdir())


def test_external_first_ancestor_reparse_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path, external_state=True)
    assert plan.admission is not None and plan.admission.state_dir is not None
    state = plan.admission.state_dir
    first = state / "first"
    first.mkdir(mode=0o700, parents=True)
    first_info = first.lstat()
    monkeypatch.setattr(
        backend_source,
        "path_is_reparse",
        lambda info: info.st_ino == first_info.st_ino,
    )
    plan = replace(
        plan,
        admission=replace(plan.admission, state_dir=first / "state"),
        runtime_dir=first / "state" / "daemon",
    )

    with pytest.raises(GuiError) as caught:
        backend_source.prepare_runtime(plan)
    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert not (first / "state").exists()


@pytest.mark.parametrize("phase", ["bootstrap", "serving"])
def test_daemon_host_environment_names_the_node_and_no_credential_or_principal(
    tmp_path: Path, phase: str
) -> None:
    base = _plan(tmp_path)
    admission = base.admission
    if phase == "bootstrap":
        admission = replace(admission, owner=None, principal=None, phase="bootstrap")
    plan = replace(
        base,
        admission=admission,
        env={**base.env, "HYPRIAL_GUI_PRINCIPAL": PRINCIPAL},
    )
    assert "HYPRIAL_NODE_ID" not in plan.env

    environment = backend_source.host_environment(plan, None, "grant-1", handoff_path=None)

    assert environment["HYPRIAL_GUI_BACKEND_DRIVER"] == "daemon"
    assert environment["HYPRIAL_NODE_ID"] == NODE
    assert environment["HYPRIAL_HOME"] == str(tmp_path / "home")
    assert environment["HARNESS_STATE_DIR"] == str(tmp_path / "home" / "state")
    assert environment["HYPRIAL_GUI_LAUNCH_GRANT"] == "grant-1"
    for name in (
        "HYPRIAL_GUI_PRINCIPAL",
        "HYPRIAL_GUI_HOME",
        "HYPRIAL_GUI_UPSTREAM_TOKEN",
        "HYPRIAL_BACKEND_ENDPOINT",
        "HYPRIAL_OWNER",
        "HARNESS_SOCKET_PATH",
    ):
        assert name not in environment
    assert "inherited-token" not in str(environment)


def test_the_daemon_host_arguments_are_the_admitted_coordinates_without_a_secret(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)

    assert backend_source.daemon_host_arguments(plan) == (
        "--home",
        str(tmp_path / "home"),
        "--state-dir",
        str(tmp_path / "home" / "state"),
        "--socket",
        str(tmp_path / "home" / "state" / "daemon.sock"),
        "--node",
        NODE,
    )


def test_mock_invocation_and_bearer_redaction_keep_the_secret_private(tmp_path: Path) -> None:
    plan = _plan(tmp_path, driver="contract-mock")
    argv, cwd, environment = backend_source.mock_invocation(plan)
    assert cwd == REPOSITORY / "gui"
    assert argv[:4] == (
        plan.project.node,
        str(REPOSITORY / "gui" / "contract-mock" / "serve.ts"),
        "--scenario",
        "success",
    )
    assert "token" not in " ".join(argv).lower()
    assert not any(name.startswith(("HYPRIAL_", "HARNESS_")) for name in environment)
    assert "contract/daemon-request" not in environment.get("PYTHONPATH", "")

    endpoint = "http://127.0.0.1:4519/daemon/request/<method>"
    secret, redacted, kind = backend_source.mock_line(f"token: Bearer {SECRET_VALUE}\n")
    assert secret == SECRET_VALUE
    assert redacted == "token: Bearer <redacted>"
    assert kind == "token"
    parsed_secret, parsed_line, parsed_kind = backend_source.mock_line(f"endpoint: {endpoint}\n")
    assert parsed_secret is None
    assert parsed_line == f"endpoint: {endpoint}\n"
    assert parsed_kind == "endpoint"

    authority = backend_source.mock_authority("http://127.0.0.1:4519", SECRET_VALUE, plan)
    assert authority.health_url == "http://127.0.0.1:4519/healthz"
    with pytest.raises(GuiError):
        backend_source.mock_authority("http://example.invalid:4519", SECRET_VALUE, plan)


def test_child_environments_do_not_inherit_borrowed_identity_or_new_secrets(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, driver="contract-mock")
    authority = backend_source.BackendAuthority(
        driver="contract-mock",
        endpoint="http://127.0.0.1:4519",
        token="new-upstream-token",
        principal="user:contract-mock",
        owner="contract-mock",
        node="local",
    )
    node = backend_source.host_environment(
        plan,
        authority,
        "new-launch-grant",
        publication_path=tmp_path / "runtime" / "launch.ready",
        handoff_path=tmp_path / "runtime" / "launch.url",
    )
    assert node["HYPRIAL_GUI_BACKEND_DRIVER"] == "contract-mock"
    assert node["HYPRIAL_GUI_PRINCIPAL"] == "user:contract-mock"
    assert node["HYPRIAL_BACKEND_ENDPOINT"] == authority.endpoint
    assert node["HYPRIAL_GUI_UPSTREAM_TOKEN"] == authority.token
    assert node["HYPRIAL_GUI_LAUNCH_GRANT"] == "new-launch-grant"
    assert node["HYPRIAL_GUI_HANDOFF_PATH"].endswith("/launch.url")
    assert node["HYPRIAL_GUI_PUBLICATION_PATH"].endswith("/launch.ready")
    assert "HYPRIAL_OWNER" not in node
    assert "HARNESS_SOCKET_PATH" not in node

    build = backend_source.build_environment(plan.env)
    serialized = json.dumps(build)
    for private in (
        "inherited-token",
        "inherited-grant",
        "new-upstream-token",
        "new-launch-grant",
        "HYPRIAL_GUI_HOME",
        "HARNESS_SOCKET_PATH",
    ):
        assert private not in serialized
    assert build["PATH"] == "/usr/bin"


def test_no_open_handoff_is_exclusive_private_and_removed(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    lease = backend_source.prepare_runtime(plan)
    url = backend_source.launch_url(plan.url, "private-grant")
    try:
        path = backend_source.write_handoff(lease, url)
        assert path == lease.directory / "launch.url"
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text(encoding="utf-8") == url + "\n"
        assert lease.directory.stat().st_mode & 0o777 == 0o700
        with pytest.raises(GuiError) as caught:
            backend_source.write_handoff(lease, url)
        assert caught.value.code == "GUI_HANDOFF_CONFLICT"
        backend_source.remove_handoff(lease)
        assert not path.exists()
    finally:
        lease.close()


def test_launch_publication_is_exclusive_private_and_secret_free(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    lease = backend_source.prepare_runtime(plan)
    try:
        path = backend_source.write_launch_publication(lease)
        assert path == lease.directory / "launch.ready"
        assert path.stat().st_mode & 0o777 == 0o600
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document == {
            "schemaVersion": 1,
            "service": "hyprial-gui",
            "event": "launch-published",
        }
        with pytest.raises(GuiError) as caught:
            backend_source.write_launch_publication(lease)
        assert caught.value.code == "GUI_PUBLICATION_CONFLICT"
    finally:
        lease.close()


def _ready_payload(plan: launcher.GuiLaunchPlan) -> dict:
    return {
        "service": "hyprial-gui",
        "mode": "production" if plan.mode == "normal" else "development",
        "dataMode": "hosted",
        "driver": plan.driver,
        "backendConfigured": True,
        "session": "launch-required",
        "host": "127.0.0.1",
        "port": plan.port,
        "url": plan.url.rstrip("/"),
    }


def test_node_ready_confirms_exact_driver_mode_and_origin(tmp_path: Path) -> None:
    plan = _plan(tmp_path, driver="contract-mock")
    payload = _ready_payload(plan)
    line = backend_source.HOST_READY_PREFIX + json.dumps(payload)
    assert backend_source.parse_host_ready(line, plan) == payload

    for field, value in (
        ("driver", "daemon"),
        ("host", "example.invalid"),
        ("port", plan.port + 1),
        ("dataMode", "fixture"),
    ):
        changed = dict(payload)
        changed[field] = value
        with pytest.raises(GuiError) as caught:
            backend_source.parse_host_ready(
                backend_source.HOST_READY_PREFIX + json.dumps(changed),
                plan,
            )
        assert caught.value.code == "GUI_BACKEND_CONTRACT"


def test_the_node_host_names_are_gone() -> None:
    for name in ("node_environment", "parse_node_ready", "node_log_path", "NODE_READY_PREFIX"):
        assert not hasattr(backend_source, name), name


def test_the_dev_server_environment_holds_no_gui_variable_of_any_kind(tmp_path: Path) -> None:
    base = _plan(tmp_path)
    plan = replace(
        base,
        env={
            **base.env,
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
            "HYPRIAL_GUI_UPSTREAM_TOKEN": "t",
            "HYPRIAL_GUI_LAUNCH_GRANT": "g",
            "HYPRIAL_GUI_PRINCIPAL": PRINCIPAL,
            "HYPRIAL_GUI_HOME": "/somewhere",
            "HARNESS_SOCKET_PATH": "/run/daemon.sock",
            "HYPRIAL_OWNER": "o",
            "HYPRIAL_BACKEND_ENDPOINT": "http://127.0.0.1:1",
            "PATH": "/usr/bin",
        },
        build_env=None,
    )

    environment = backend_source.assets_environment(plan)

    assert environment["PATH"] == "/usr/bin"
    assert not [name for name in environment if name.startswith("HYPRIAL_GUI_")]
    for name in ("HARNESS_SOCKET_PATH", "HYPRIAL_OWNER", "HYPRIAL_BACKEND_ENDPOINT"):
        assert name not in environment


def _assets_line(**overrides: object) -> str:
    payload = {
        "service": "hyprial-gui",
        "mode": "development",
        "dataMode": "unavailable",
        "host": "127.0.0.1",
        "port": 5173,
        "url": "http://127.0.0.1:5173",
        "driver": None,
        "backendConfigured": False,
        "session": "unavailable",
    }
    payload.update(overrides)
    return backend_source.HOST_READY_PREFIX + json.dumps(payload)


def test_the_dev_server_ready_line_yields_its_loopback_origin() -> None:
    assert backend_source.parse_assets_ready(_assets_line()) == "http://127.0.0.1:5173"
    assert backend_source.parse_assets_ready(_assets_line(host="::1", port=4000)) == "http://[::1]:4000"


@pytest.mark.parametrize(
    "line",
    [
        _assets_line(driver="daemon"),
        _assets_line(mode="production"),
        _assets_line(service="other"),
        _assets_line(host="0.0.0.0"),
        _assets_line(host="evil.example"),
        _assets_line(port=0),
        _assets_line(port=70000),
        _assets_line(port="5173"),
        _assets_line(port=True),
        backend_source.HOST_READY_PREFIX + "[]",
        backend_source.HOST_READY_PREFIX + "not json",
        "something else",
    ],
)
def test_a_dev_server_ready_line_that_is_not_a_driverless_loopback_development_server_is_refused(line: str) -> None:
    with pytest.raises(GuiError):
        backend_source.parse_assets_ready(line)


def test_a_custody_file_left_by_an_older_build_is_removed_with_the_other_runtime_files(tmp_path):
    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o700)
    stale = directory / "logout-custody.json"
    stale.write_text("{}", encoding="utf-8")
    keep = directory / "unrelated.txt"
    keep.write_text("keep", encoding="utf-8")

    backend_source._remove_stale_files(directory)

    assert not stale.exists()
    assert keep.exists()
