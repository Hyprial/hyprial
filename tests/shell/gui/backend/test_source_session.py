"""Owned-child ordering, cleanup and private startup handoff for source GUI."""

from __future__ import annotations

import json
import queue
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel import harden_private_path
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import lifecycle
from hyprial.shell.impl.gui.backend import source as backend_source
from hyprial.shell.impl.gui.backend.admission import SourceAdmission
from hyprial.shell.impl.gui.backend.config import host_argv
from hyprial.shell.impl.gui.errors import GuiError


REPOSITORY = Path(__file__).resolve().parents[4]
GRANT = "launch-grant-private-value"
MOCK_TOKEN = "mock-bearer-private-value"


class FakeProcess:
    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def _plan(tmp_path: Path, *, driver: str = "contract-mock") -> launcher.GuiLaunchPlan:
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    harden_private_path(tmp_path, mode=0o700)
    project = launcher.GuiProject(
        home=tmp_path / "gui",
        worktree=REPOSITORY,
        branch="test",
        node="/fake/node",
        npm="/fake/npm",
        node_version="v24",
        npm_version="11",
        scripts={"start": "node server.mjs", "build": "vite build"},
    )
    admission = SourceAdmission(
        driver=driver,
        owner="contract-mock" if driver == "contract-mock" else "source-owner",
        node="local" if driver == "contract-mock" else "source-node",
        principal=(
            "user:contract-mock"
            if driver == "contract-mock"
            else "user:source-owner"
        ),
        home=None if driver == "contract-mock" else tmp_path / "home",
        state_dir=tmp_path / "state",
        socket_path=None if driver == "contract-mock" else tmp_path / "state" / "daemon.sock",
    )
    return launcher.GuiLaunchPlan(
        mode="normal",
        project=project,
        host="127.0.0.1",
        port=4173,
        url="http://127.0.0.1:4173/",
        backend_endpoint=None,
        backend_owned=driver != "daemon",
        frontend_argv=("/fake/npm", "start"),
        env={"PATH": "/usr/bin", "HYPRIAL_OWNER": "must-not-reach-children"},
        open_browser=False,
        build_argv=("/fake/npm", "run", "build"),
        driver=driver,
        admission=admission,
        runtime_dir=tmp_path / "runtime",
    )


def _ready_line(plan: launcher.GuiLaunchPlan) -> str:
    return backend_source.HOST_READY_PREFIX + json.dumps(
        {
            "service": "hyprial-gui",
            "mode": "development" if plan.mode == "dev" else "production",
            "dataMode": "hosted",
            "driver": plan.driver,
            "backendConfigured": True,
            "session": "launch-required",
            "host": "127.0.0.1",
            "port": plan.port,
            "url": plan.url.rstrip("/"),
        }
    )


def _assets_ready_line(**overrides: Any) -> str:
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


def _dev_plan(tmp_path: Path) -> launcher.GuiLaunchPlan:
    base = _plan(tmp_path)
    return replace(
        base,
        mode="dev",
        frontend_argv=host_argv("127.0.0.1", 4173),
        assets_argv=("/fake/npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "0", "--strict-port"),
        build_argv=None,
        env={
            **base.env,
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
            "HYPRIAL_GUI_HOME": str(base.project.home),
            "HYPRIAL_GUI_PRINCIPAL": "user:inherited",
        },
    )


def _install_success_seams(
    monkeypatch: pytest.MonkeyPatch,
    plan: launcher.GuiLaunchPlan,
    *,
    assets_line: str | None = None,
    assets_exit: int | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    events: list[str] = []
    captured: dict[str, dict[str, Any]] = {}
    real_prepare = backend_source.prepare_runtime
    real_invocation = backend_source.mock_invocation
    real_handoff = backend_source.write_handoff
    real_publication = backend_source.write_launch_publication

    def prepare(active: launcher.GuiLaunchPlan):
        events.append("runtime")
        return real_prepare(active)

    def invocation(active: launcher.GuiLaunchPlan):
        events.append("mock-invocation")
        return real_invocation(active)

    def handoff(lease, url: str):
        events.append("handoff")
        return real_handoff(lease, url)

    def publication(lease):
        events.append("publication")
        return real_publication(lease)

    def spawn(
        active: launcher.GuiLaunchPlan,
        argv: tuple[str, ...],
        *,
        name: str,
        env,
        cwd: Path,
        log_path: Path,
        popen,
        mock_capture: bool = False,
        redactions: set[str] | None = None,
    ) -> lifecycle.GuiChild:
        events.append(f"spawn:{name}")
        lines: queue.Queue[Any] = queue.Queue()
        if name == "contract-mock":
            lines.put(("endpoint", "endpoint: http://127.0.0.1:4519/daemon/request/<method>\n"))
            lines.put(("token", MOCK_TOKEN))
        elif name == "assets":
            if assets_exit is None:
                lines.put(assets_line or _assets_ready_line())
        else:
            lines.put(_ready_line(active))
        captured[name] = {
            "argv": argv,
            "env": dict(env),
            "cwd": cwd,
            "log_path": log_path,
            "mock_capture": mock_capture,
            "redactions": set(redactions or ()),
        }
        return lifecycle.GuiChild(
            name=name,
            process=FakeProcess(
                {"contract-mock": 101, "assets": 103}.get(name, 102),
                assets_exit if name == "assets" else None,
            ),
            group=None,
            lines=lines,
        )

    monkeypatch.setattr(backend_source, "prepare_runtime", prepare)
    monkeypatch.setattr(backend_source, "mock_invocation", invocation)
    monkeypatch.setattr(backend_source, "write_handoff", handoff)
    monkeypatch.setattr(backend_source, "write_launch_publication", publication)
    monkeypatch.setattr(backend_source, "generate_launch_grant", lambda: GRANT)
    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)
    return events, captured


def _start(
    monkeypatch: pytest.MonkeyPatch,
    plan: launcher.GuiLaunchPlan,
    *,
    open_browser: bool,
    opener=None,
) -> tuple[launcher.GuiSession, list[str], dict[str, dict[str, Any]]]:
    events, captured = _install_success_seams(monkeypatch, plan)
    active_plan = replace(plan, open_browser=open_browser)

    def open_url(url: str):
        events.append("open")
        return opener(url) if opener is not None else True

    session = _run_start(active_plan, events, open_url)
    return session, events, captured


def _run_start(
    plan: launcher.GuiLaunchPlan,
    events: list[str],
    opener,
) -> launcher.GuiSession:

    def build(active, *, json_output: bool, runner) -> None:
        events.append("build")

    def wait_http(url: str, process, *, label: str, **kwargs) -> None:
        events.append(f"health:{label}")

    def terminate(child) -> None:
        if child is not None:
            events.append(f"terminate:{child.name}")

    return lifecycle.start_session(
        plan,
        opener=opener,
        run_build=build,
        wait_for_http=wait_http,
        terminate_child=terminate,
        sleep=lambda _: None,
        urlopen=lambda url, timeout: _Response(
            {"ok": True, "dataMode": "hosted", "assets": "installed", "assetsVerified": True}
        ),
        popen=lambda *args, **kwargs: pytest.fail("fake Popen must not be used"),
    )


def test_source_start_orders_owned_children_and_writes_one_usable_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    session, events, captured = _start(monkeypatch, plan, open_browser=False)
    try:
        assert events == [
            "runtime",
            "build",
            "mock-invocation",
            "spawn:contract-mock",
            "health:GUI backend",
            "spawn:frontend",
            "health:GUI frontend",
            "handoff",
            "publication",
        ]
        assert session.backend is not None
        assert session.plan.backend_endpoint == "http://127.0.0.1:4519"
        assert captured["contract-mock"]["mock_capture"] is True
        assert captured["contract-mock"]["cwd"] == REPOSITORY / "gui"
        assert captured["frontend"]["cwd"] == plan.project.home
        assert captured["frontend"]["env"]["HYPRIAL_GUI_LAUNCH_GRANT"] == GRANT
        assert captured["frontend"]["env"]["HYPRIAL_GUI_UPSTREAM_TOKEN"] == MOCK_TOKEN
        assert captured["frontend"]["env"]["HYPRIAL_GUI_BACKEND_DRIVER"] == "contract-mock"
        assert captured["frontend"]["env"]["HYPRIAL_GUI_HANDOFF_PATH"] == str(
            tmp_path / "runtime" / "launch.url"
        )
        assert captured["frontend"]["env"]["HYPRIAL_GUI_PUBLICATION_PATH"] == str(
            tmp_path / "runtime" / "launch.ready"
        )
        for child in captured.values():
            assert GRANT not in " ".join(child["argv"])
            assert MOCK_TOKEN not in " ".join(child["argv"])
            assert "HYPRIAL_OWNER" not in child["env"]

        assert session.handoff_path is not None
        assert session.handoff_path.stat().st_mode & 0o777 == 0o600
        launch = session.handoff_path.read_text(encoding="utf-8").strip()
        assert launch.startswith(plan.url)
        assert f"#grant={GRANT}" in launch

        payload = launcher.startup_payload(session)
        serialized = json.dumps(payload)
        assert payload["url"] == plan.url
        assert payload["handoff_path"] == str(session.handoff_path)
        assert "#grant" not in serialized
        assert GRANT not in serialized
        assert MOCK_TOKEN not in serialized
    finally:
        session.stop()

    assert "terminate:frontend" in events
    assert "terminate:contract-mock" in events
    assert not (tmp_path / "runtime" / "launch.url").exists()
    assert not (tmp_path / "runtime" / "launch.ready").exists()
    assert not (tmp_path / "runtime" / "host.log").exists()
    assert not (tmp_path / "runtime" / "mock.log").exists()


def test_open_browser_uses_fragment_but_startup_output_does_not(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    plan = _plan(tmp_path)
    session, events, _captured = _start(
        monkeypatch,
        plan,
        open_browser=True,
        opener=lambda url: seen.append(url) or True,
    )
    try:
        assert "open" in events
        assert events.index("publication") < events.index("open")
        assert session.handoff_path is None
        assert seen == [f"{plan.url}#grant={GRANT}"]
        assert "HYPRIAL_GUI_HANDOFF_PATH" not in _captured["frontend"]["env"]
        assert _captured["frontend"]["env"]["HYPRIAL_GUI_PUBLICATION_PATH"].endswith(
            "/launch.ready"
        )
        assert GRANT not in json.dumps(launcher.startup_payload(session))
    finally:
        session.stop()


def test_browser_open_failure_reclaims_both_owned_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = replace(_plan(tmp_path), open_browser=True)
    events, _captured = _install_success_seams(monkeypatch, plan)
    opened: list[str] = []

    def opener(url: str):
        events.append("open")
        opened.append(url)
        return False

    with pytest.raises(GuiError) as caught:
        _run_start(plan, events, opener)
    assert caught.value.code == "GUI_BROWSER_FAILED"
    assert opened == [f"{plan.url}#grant={GRANT}"]
    assert events[-2:] == ["terminate:frontend", "terminate:contract-mock"]
    assert not (tmp_path / "runtime" / "launch.url").exists()
    assert not (tmp_path / "runtime" / "launch.ready").exists()


def test_publication_failure_after_private_handoff_reclaims_owned_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    events, _captured = _install_success_seams(monkeypatch, plan)

    def fail_publication(lease):
        events.append("publication-failed")
        raise GuiError("GUI_RUNTIME_UNAVAILABLE", "publication failed")

    monkeypatch.setattr(backend_source, "write_launch_publication", fail_publication)

    with pytest.raises(GuiError) as caught:
        _run_start(plan, events, opener=lambda _url: True)

    assert caught.value.code == "GUI_RUNTIME_UNAVAILABLE"
    assert events[-3:] == [
        "publication-failed",
        "terminate:frontend",
        "terminate:contract-mock",
    ]
    assert not (tmp_path / "runtime" / "launch.url").exists()
    assert not (tmp_path / "runtime" / "launch.ready").exists()


def test_a_host_that_exits_before_its_ready_line_is_owned_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, driver="daemon")
    events: list[str] = []
    real_prepare = backend_source.prepare_runtime

    def prepare(active):
        events.append("runtime")
        return real_prepare(active)

    def spawn(active, argv, *, name, **kwargs):
        events.append(f"spawn:{name}")
        return lifecycle.GuiChild(name, FakeProcess(211, returncode=7), None, queue.Queue())

    def terminate(child):
        if child is not None:
            events.append(f"terminate:{child.name}")

    def wait_http(*args, **kwargs):
        events.append("health")

    def build(*args, **kwargs):
        events.append("build")

    monkeypatch.setattr(backend_source, "prepare_runtime", prepare)
    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)
    with pytest.raises(GuiError) as caught:
        lifecycle.start_session(
            plan,
            run_build=build,
            wait_for_http=wait_http,
            terminate_child=terminate,
            sleep=lambda _: None,
            popen=lambda *args, **kwargs: pytest.fail("fake Popen must not be used"),
        )

    assert caught.value.code == "GUI_START_FAILED"
    assert "GUI host exited before its readiness line" in str(caught.value)
    assert caught.value.data["exit_code"] == 7
    assert events == ["runtime", "build", "spawn:frontend", "terminate:frontend"]
    assert not (tmp_path / "runtime" / "request-http.json").exists()


class _FakeClock:
    """A fake monotonic clock whose sleeps advance time without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _SilentLines:
    """A captured output stream that never delivers a line, without a real wait."""

    def get(self, timeout: float | None = None) -> Any:
        raise queue.Empty


def test_a_host_that_never_announces_times_out_at_the_host_budget_and_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, driver="daemon")
    events: list[str] = []
    clock = _FakeClock()
    waits: list[float] = []

    def spawn(active, argv, *, name, **kwargs):
        events.append(f"spawn:{name}")
        return lifecycle.GuiChild(name, FakeProcess(214), None, _SilentLines())

    def terminate(child):
        if child is not None:
            events.append(f"terminate:{child.name}")

    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)
    with pytest.raises(GuiError) as caught:
        lifecycle.start_session(
            plan,
            run_build=lambda *args, **kwargs: None,
            wait_for_http=lambda *args, **kwargs: events.append("health"),
            terminate_child=terminate,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            on_backend_wait=lambda: waits.append(clock.now),
            popen=lambda *args, **kwargs: pytest.fail("fake Popen must not be used"),
        )

    assert caught.value.code == "GUI_READY_TIMEOUT"
    assert 60.0 <= clock.now < 60.5
    assert len(waits) == 1 and 5.0 <= waits[0] < 5.5
    assert events == ["spawn:frontend", "terminate:frontend"]
    assert not (tmp_path / "runtime" / "request-http.json").exists()
    assert not (tmp_path / "runtime" / "server.json").exists()


def test_a_failed_start_keeps_stage_times_and_the_hosts_redacted_log_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, driver="daemon")
    clock = _FakeClock()
    log = tmp_path / "runtime" / "host.log"

    def spawn(active, argv, *, name, **kwargs):
        log.write_text("x" * 9000 + "\nimport stalled\n", encoding="utf-8")
        child = lifecycle.GuiChild(name, FakeProcess(214), None, _SilentLines())
        child.redactions.add("stalled")
        return child

    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)
    with pytest.raises(GuiError) as caught:
        lifecycle.start_session(
            plan,
            run_build=lambda *args, **kwargs: None,
            wait_for_http=lambda *args, **kwargs: None,
            terminate_child=lambda child: None,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            popen=lambda *args, **kwargs: pytest.fail("fake Popen must not be used"),
        )

    data = caught.value.data
    assert caught.value.code == "GUI_READY_TIMEOUT"
    assert 60.0 <= data["stages_seconds"]["failed"] < 60.5
    child = data["children"]["frontend"]
    assert child["pid"] == 214 and child["exit_code"] is None
    assert len(child["log_tail"]) <= lifecycle.process.FAILURE_LOG_TAIL_BYTES
    assert child["log_tail"].endswith("import <redacted>\n")
    assert "request-http" not in data["children"]


def test_contract_mock_eof_is_a_controlled_start_failure(tmp_path: Path) -> None:
    lines: queue.Queue[Any] = queue.Queue()
    lines.put(lifecycle._EOF)
    child = lifecycle.GuiChild("contract-mock", FakeProcess(212), None, lines=lines)

    with pytest.raises(GuiError) as caught:
        lifecycle._wait_for_mock(
            _plan(tmp_path),
            child,
            sleep=lambda _: None,
            monotonic=lambda: 0.0,
        )

    assert caught.value.code == "GUI_START_FAILED"
    assert "closed its private output early" in str(caught.value)


def test_backend_and_frontend_exit_are_reported_by_session_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    session, _events, _captured = _start(monkeypatch, plan, open_browser=False)
    try:
        assert session.backend is not None
        session.frontend.process.returncode = 0
        assert session.wait(sleep=lambda _: None) == 0

        session.frontend.process.returncode = None
        session.backend.process.returncode = 2
        with pytest.raises(GuiError) as caught:
            session.wait(sleep=lambda _: None)
        assert caught.value.code == "GUI_BACKEND_EXITED"
        assert caught.value.data["backend_exit_code"] == 2
    finally:
        session.stop()


class _Response:
    status = 200

    def __init__(self, body: Any = None) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_backend_health_request_carries_private_bearer_header_only() -> None:
    seen: list[Any] = []

    def urlopen(request, *, timeout):
        seen.append(request)
        return _Response()

    opened = lifecycle._authorized_urlopen(urlopen, "backend-private-token")
    response = opened("http://127.0.0.1:4519/healthz", timeout=1.0)
    assert response.status == 200
    assert seen[0].full_url == "http://127.0.0.1:4519/healthz"
    assert seen[0].get_header("Authorization") == "Bearer backend-private-token"


def _installed_plan(tmp_path: Path, *, open_browser: bool) -> launcher.GuiLaunchPlan:
    assets = tmp_path / "wheel-gui"
    assets.mkdir()
    base = _plan(tmp_path, driver="daemon")
    assert base.admission is not None
    base.admission.home.mkdir()
    project = replace(
        base.project,
        home=assets,
        worktree=assets,
        node="",
        npm="",
        scripts={},
        installed=True,
    )
    return replace(
        base,
        project=project,
        build_argv=None,
        open_browser=open_browser,
        frontend_argv=(
            sys.executable,
            "-m",
            "hyprial.shell.impl.gui.host",
            "serve",
            "--root",
            str(assets),
            "--host",
            "127.0.0.1",
            "--port",
            "4173",
        ),
    )


def _start_installed(
    monkeypatch: pytest.MonkeyPatch, plan: launcher.GuiLaunchPlan, opened: list[str]
) -> tuple[launcher.GuiSession, list[str], dict[str, dict[str, Any]]]:
    events, captured = _install_success_seams(monkeypatch, plan)

    def spawn(active, argv, *, name, env, cwd, log_path, popen, mock_capture=False, redactions=None):
        events.append(f"spawn:{name}")
        lines: queue.Queue[Any] = queue.Queue()
        if name == "frontend":
            lines.put(_ready_line(active))
        captured[name] = {
            "argv": argv,
            "env": dict(env),
            "cwd": cwd,
            "log_path": log_path,
            "redactions": set(redactions or ()),
        }
        return lifecycle.GuiChild(name, FakeProcess(os.getpid()), None, lines)

    monkeypatch.setattr(lifecycle.process, "_spawn_captured", spawn)

    def open_url(url: str) -> bool:
        events.append("open")
        opened.append(url)
        return True

    session = _run_start(plan, events, open_url)
    return session, events, captured


def test_installed_session_starts_only_the_python_host_with_no_open_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _installed_plan(tmp_path, open_browser=False)
    opened: list[str] = []
    session, events, captured = _start_installed(monkeypatch, plan, opened)
    try:
        assert events == [
            "runtime",
            "spawn:frontend",
            "health:GUI frontend",
            "handoff",
            "publication",
        ]
        assert set(captured) == {"frontend"}
        frontend = captured["frontend"]
        assert frontend["argv"] == (*plan.frontend_argv, *backend_source.daemon_host_arguments(plan))
        assert frontend["argv"][-8:] == (
            "--home", str(plan.admission.home),
            "--state-dir", str(plan.admission.state_dir),
            "--socket", str(plan.admission.socket_path),
            "--node", "source-node",
        )
        assert frontend["cwd"] == plan.project.home
        assert frontend["log_path"].name == "host.log"
        env = frontend["env"]
        assert env["HYPRIAL_GUI_BACKEND_DRIVER"] == "daemon"
        assert env["HYPRIAL_NODE_ID"] == "source-node"
        assert env["HYPRIAL_HOME"] == str(plan.admission.home)
        assert env["HARNESS_STATE_DIR"] == str(plan.admission.state_dir)
        assert env["HYPRIAL_GUI_LAUNCH_GRANT"] == GRANT
        assert env["HYPRIAL_GUI_HANDOFF_PATH"] == str(tmp_path / "runtime" / "launch.url")
        assert env["HYPRIAL_GUI_PUBLICATION_PATH"] == str(tmp_path / "runtime" / "launch.ready")
        for name in (
            "HYPRIAL_BACKEND_ENDPOINT",
            "HYPRIAL_GUI_UPSTREAM_TOKEN",
            "HYPRIAL_GUI_PRINCIPAL",
            "HYPRIAL_OWNER",
        ):
            assert name not in env, name
        assert GRANT not in " ".join(frontend["argv"])
        assert frontend["redactions"] == {GRANT}
        assert opened == []
        assert session.handoff_path is not None
        launch = session.handoff_path.read_text(encoding="utf-8").strip()
        assert launch == f"{plan.url}#grant={GRANT}"
        payload = launcher.startup_payload(session)
        assert payload["backend_pid"] is None and payload["backend_endpoint"] == ""
        assert payload["backend_driver"] == "daemon" and payload["frontend_pid"] == os.getpid()
        assert GRANT not in json.dumps(payload)
        rendered = launcher.render_startup(payload)
        assert "backend  : in the host process" in rendered and "owned by CLI" not in rendered
        assert session.backend is None
        assert not (tmp_path / "runtime" / "server.json").exists()
        assert not (tmp_path / "runtime" / "frontend-authority.json").exists()
    finally:
        session.stop()
    assert not (tmp_path / "runtime" / "launch.url").exists()
    assert not (tmp_path / "runtime" / "host.log").exists()


def test_installed_session_opens_only_the_grant_url_never_the_bare_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _installed_plan(tmp_path, open_browser=True)
    opened: list[str] = []
    session, events, captured = _start_installed(monkeypatch, plan, opened)
    try:
        assert opened == [f"{plan.url}#grant={GRANT}"]
        assert events.index("publication") < events.index("open")
        assert "HYPRIAL_GUI_HANDOFF_PATH" not in captured["frontend"]["env"]
        assert session.handoff_path is None
    finally:
        session.stop()


def test_build_failure_spawns_no_child_and_releases_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    events, _captured = _install_success_seams(monkeypatch, plan)

    def build(active, *, json_output: bool, runner) -> None:
        events.append("build")
        raise GuiError("GUI_BUILD_FAILED", "the GUI build exited with status 2")

    with pytest.raises(GuiError) as caught:
        lifecycle.start_session(
            replace(plan, open_browser=False),
            run_build=build,
            wait_for_http=lambda *a, **k: events.append("health"),
            terminate_child=lambda child: None,
            sleep=lambda _: None,
            popen=lambda *args, **kwargs: pytest.fail("fake Popen must not be used"),
        )

    assert caught.value.code == "GUI_BUILD_FAILED"
    assert events == ["runtime", "build"]


def test_dev_start_runs_vite_then_the_host_relaying_to_it_and_stops_host_vite_backend_in_that_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _dev_plan(tmp_path)
    session, events, captured = _start(monkeypatch, plan, open_browser=False)
    try:
        assert events == [
            "runtime",
            "spawn:assets",
            "mock-invocation",
            "spawn:contract-mock",
            "health:GUI backend",
            "spawn:frontend",
            "health:GUI frontend",
            "handoff",
            "publication",
        ]
        assert captured["assets"]["argv"] == plan.assets_argv
        assert captured["assets"]["cwd"] == plan.project.home
        assert not [name for name in captured["assets"]["env"] if name.startswith("HYPRIAL_GUI_")]
        assert "HYPRIAL_OWNER" not in captured["assets"]["env"]
        assert captured["frontend"]["argv"] == (*plan.frontend_argv, "--live-upstream", "http://127.0.0.1:5173")
        assert captured["frontend"]["env"]["HYPRIAL_GUI_UPSTREAM_TOKEN"] == MOCK_TOKEN
        assert captured["frontend"]["env"]["HYPRIAL_GUI_LAUNCH_GRANT"] == GRANT
        for argv in (captured["assets"]["argv"], captured["frontend"]["argv"]):
            assert GRANT not in " ".join(argv) and MOCK_TOKEN not in " ".join(argv)
        assert session.assets is not None and session.assets.name == "assets"
    finally:
        session.stop()

    stops = [event for event in events if event.startswith("terminate:")]
    assert stops == ["terminate:frontend", "terminate:assets", "terminate:contract-mock"]
    assert not (tmp_path / "runtime" / "assets.log").exists()


def test_a_normal_start_has_no_assets_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session, events, captured = _start(monkeypatch, _plan(tmp_path), open_browser=False)
    try:
        assert session.assets is None and "assets" not in captured
        assert "spawn:assets" not in events
    finally:
        session.stop()


def test_a_dev_server_that_names_a_driver_is_refused_and_everything_started_is_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _dev_plan(tmp_path)
    events, _captured = _install_success_seams(monkeypatch, plan, assets_line=_assets_ready_line(driver="daemon"))

    with pytest.raises(GuiError) as caught:
        _run_start(replace(plan, open_browser=False), events, None)

    assert caught.value.code == "GUI_BACKEND_CONTRACT"
    assert "spawn:frontend" not in events
    assert events[-2:] == ["terminate:assets", "terminate:contract-mock"]
    assert not (tmp_path / "runtime" / "assets.log").exists()


def test_a_dev_server_that_exits_before_its_ready_line_fails_the_start_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _dev_plan(tmp_path)
    events, _captured = _install_success_seams(monkeypatch, plan, assets_exit=1)

    with pytest.raises(GuiError) as caught:
        _run_start(replace(plan, open_browser=False), events, None)

    assert caught.value.code == "GUI_START_FAILED"
    assert "GUI development server exited before its readiness line" in str(caught.value)
    assert caught.value.data["exit_code"] == 1
    assert caught.value.data["children"]["assets"]["exit_code"] == 1
    assert "assets_spawned" in caught.value.data["stages_seconds"]
    assert "spawn:frontend" not in events
    assert events[-2:] == ["terminate:assets", "terminate:contract-mock"]


def test_session_wait_reports_the_dev_server_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session, _events, _captured = _start(monkeypatch, _dev_plan(tmp_path), open_browser=False)
    try:
        assert session.assets is not None
        session.assets.process.returncode = 3
        with pytest.raises(GuiError) as caught:
            session.wait(sleep=lambda _: None)
        assert caught.value.code == "GUI_ASSETS_EXITED"
        assert caught.value.data["assets_exit_code"] == 3
    finally:
        session.stop()
