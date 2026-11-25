"""Regression coverage for launcher seams retained across the GUI extraction."""

from __future__ import annotations

import io
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import urllib.request

import pytest

from hyprial.kernel import harden_private_path
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend.admission import SourceAdmission
from hyprial.shell.impl.gui.backend.lifecycle import _wait_for_http


class FakeProcess:
    def __init__(self, argv, *, pid: int, returncode: int | None = None, stdout=None):
        self.argv = list(argv)
        self.pid = pid
        self.returncode = returncode
        self.stdout = stdout

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_direct_session_stop_uses_launcher_terminate_child_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[str] = []
    monkeypatch.setattr(
        launcher, "_terminate_child", lambda child: terminated.append(child.name)
    )
    session = launcher.GuiSession(
        plan=SimpleNamespace(backend_owned=True),
        frontend=launcher.GuiChild(
            "frontend",
            FakeProcess(("npm", "start"), pid=501),
            None,
        ),
        backend=launcher.GuiChild(
            "backend",
            FakeProcess(("npm", "run", "stub"), pid=502),
            None,
        ),
    )

    session.stop()

    assert terminated == ["frontend", "backend"]


def test_direct_session_stop_reclaims_the_dev_server_between_the_host_and_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[str] = []
    monkeypatch.setattr(
        launcher, "_terminate_child", lambda child: terminated.append(child.name)
    )
    session = launcher.GuiSession(
        plan=SimpleNamespace(backend_owned=True),
        frontend=launcher.GuiChild("frontend", FakeProcess(("host",), pid=501), None),
        backend=launcher.GuiChild("backend", FakeProcess(("request-http",), pid=502), None),
        assets=launcher.GuiChild("assets", FakeProcess(("npm", "run", "dev"), pid=503), None),
    )

    session.stop()

    assert terminated == ["frontend", "assets", "backend"]


def test_source_start_failure_reclaims_only_owned_children_and_redacts_mock_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}
    terminated: list[str] = []
    secret = "private-contract-mock-bearer"
    harden_private_path(tmp_path, mode=0o700)
    repository = Path(__file__).resolve().parents[4]
    project = launcher.GuiProject(
        home=tmp_path / "gui",
        worktree=repository,
        branch="test",
        node="/fake/node",
        npm="/fake/npm",
        node_version="v24",
        npm_version="11",
        scripts={"start": "node server.mjs"},
    )
    admission = SourceAdmission(
        driver="contract-mock",
        owner="contract-mock",
        node="local",
        principal="user:contract-mock",
        home=None,
        state_dir=tmp_path / "state",
        socket_path=None,
    )
    plan = launcher.GuiLaunchPlan(
        mode="normal",
        project=project,
        host="127.0.0.1",
        port=4173,
        url="http://127.0.0.1:4173/",
        backend_endpoint=None,
        backend_owned=True,
        frontend_argv=("/fake/npm", "start"),
        env={},
        open_browser=False,
        driver="contract-mock",
        admission=admission,
        runtime_dir=tmp_path / "runtime",
    )
    real_cleanup = launcher._lifecycle._cleanup_runtime_files

    def cleanup(lease):
        if lease is not None:
            log = lease.directory / "mock.log"
            observed["log"] = log.read_text(encoding="utf-8")
            observed["directory_mode"] = lease.directory.stat().st_mode & 0o777
        real_cleanup(lease)

    monkeypatch.setattr(launcher, "_wait_for_http", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher._lifecycle.process, "_cleanup_runtime_files", cleanup)
    monkeypatch.setattr(
        launcher, "_terminate_child", lambda child: terminated.append(child.name)
    )

    def spawn(argv, **kwargs):
        if argv == plan.frontend_argv:
            return FakeProcess(argv, pid=602, stdout=io.StringIO("frontend closed\n"))
        return FakeProcess(
            argv,
            pid=601,
            stdout=io.StringIO(
                "endpoint: http://127.0.0.1:4519/daemon/request/<method>\n"
                f"token: Bearer {secret}\n"
            ),
        )

    with pytest.raises(launcher.GuiError) as caught:
        launcher.start_session(plan, popen=spawn, sleep=lambda _: None)

    assert caught.value.code == "GUI_START_FAILED"
    assert terminated == ["frontend", "contract-mock"]
    assert observed["directory_mode"] == 0o700
    assert secret not in str(observed["log"])
    assert "token: Bearer <redacted>" in str(observed["log"])
    assert not (tmp_path / "runtime" / "launch.url").exists()


@contextmanager
def _readiness_endpoint(delay: float, status: int = 200):
    requests: list[float] = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(time.monotonic())
            release.wait(delay)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/readyz", requests
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _probe_http(url: str, timeout: float) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    _wait_for_http(
        url,
        SimpleNamespace(poll=lambda: None),
        label="GUI backend",
        timeout=timeout,
        urlopen=opener.open,
        sleep=time.sleep,
        monotonic=time.monotonic,
    )


@pytest.mark.parametrize("delay", [0.0, 1.2])
def test_readiness_accepts_one_response_within_startup_budget(delay: float) -> None:
    with _readiness_endpoint(delay) as (url, requests):
        started = time.monotonic()
        _probe_http(url, 2.0)
        elapsed = time.monotonic() - started
        assert len(requests) == 1
        if delay == 0:
            assert elapsed < 1.0
        else:
            assert 1.0 < elapsed < 2.0


def test_readiness_slow_response_cannot_exceed_startup_budget() -> None:
    with _readiness_endpoint(0.5) as (url, requests):
        started = time.monotonic()
        with pytest.raises(launcher.GuiError) as caught:
            _probe_http(url, 0.1)
        elapsed = time.monotonic() - started
        assert caught.value.code == "GUI_READY_TIMEOUT"
        assert len(requests) == 1
        assert elapsed < 0.4


def test_readiness_refusal_is_retried_and_remains_a_failure() -> None:
    with _readiness_endpoint(0, 503) as (url, requests):
        started = time.monotonic()
        with pytest.raises(launcher.GuiError) as caught:
            _probe_http(url, 0.8)
        elapsed = time.monotonic() - started
        assert caught.value.code == "GUI_READY_TIMEOUT"
        assert len(requests) >= 2
        assert elapsed < 1.2
