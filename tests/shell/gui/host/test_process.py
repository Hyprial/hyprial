"""The host as a child process: bind, ready line, signals and startup refusals."""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.shell.gui.host.support import GRANT, UPSTREAM_TOKEN

COMMIT = "b" * 40
MODULE = "hyprial.shell.impl.gui.host"


def asset_root(tmp_path: Path) -> Path:
    root = tmp_path / "gui"
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>Hyprial</title>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    return root


def child_env(**overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HYPRIAL_GUI_BACKEND_DRIVER": "daemon",
        "HYPRIAL_BACKEND_ENDPOINT": "http://127.0.0.1:9",
        "HYPRIAL_GUI_UPSTREAM_TOKEN": UPSTREAM_TOKEN,
        "HYPRIAL_GUI_LAUNCH_GRANT": GRANT,
    }
    env.update(overrides)
    return env


def spawn(root: Path, env: dict[str, str], *extra: str) -> subprocess.Popen[str]:
    argv = [sys.executable, "-m", MODULE, "serve", "--port", "0", "--root", str(root), *extra]
    return subprocess.Popen(
        argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def ready_line(child: subprocess.Popen[str]) -> dict:
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line.startswith("HYPRIAL_GUI_READY "), line
    return json.loads(line[len("HYPRIAL_GUI_READY ") :])


def stop(child: subprocess.Popen[str], sig: signal.Signals = signal.SIGTERM) -> tuple[int, str, str]:
    if child.poll() is None:
        child.send_signal(sig)
    out, err = child.communicate(timeout=20)
    return child.returncode, out, err


def refused(child: subprocess.Popen[str]) -> tuple[int, dict]:
    out, err = child.communicate(timeout=20)
    assert "HYPRIAL_GUI_READY" not in out
    return child.returncode, json.loads(err.strip().splitlines()[-1])


def test_the_child_prints_the_ready_line_only_after_it_is_listening(tmp_path: Path) -> None:
    child = spawn(asset_root(tmp_path), child_env())
    try:
        payload = ready_line(child)
        connection = http.client.HTTPConnection("127.0.0.1", payload["port"], timeout=10)
        connection.request("GET", "/__gui/healthz")
        response = connection.getresponse()
        health = json.loads(response.read())
        connection.close()
    finally:
        code, out, err = stop(child)
    assert payload == {
        "service": "hyprial-gui",
        "mode": "production",
        "dataMode": "hosted",
        "host": "127.0.0.1",
        "port": payload["port"],
        "url": f"http://127.0.0.1:{payload['port']}",
        "driver": "daemon",
        "backendConfigured": True,
        "session": "launch-required",
        "devTools": False,
        "apiDocs": False,
    }
    assert payload["port"] > 0
    assert response.status == 200 and health["dataMode"] == "hosted" and health["assets"] == "explicit-unverified"
    assert code == 0
    assert out.strip().splitlines()[-1] == "HYPRIAL_GUI_STOPPED {}"
    for text in (out, err):
        assert GRANT not in text and UPSTREAM_TOKEN not in text


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_drains_removes_launch_files_and_exits_zero(tmp_path: Path, sig: signal.Signals) -> None:
    handoff, publication = tmp_path / "launch.url", tmp_path / "launch.ready"
    for path in (handoff, publication):
        path.write_text("x\n")
        path.chmod(0o600)
    env = child_env(HYPRIAL_GUI_HANDOFF_PATH=str(handoff), HYPRIAL_GUI_PUBLICATION_PATH=str(publication))
    child = spawn(asset_root(tmp_path), env)
    try:
        ready_line(child)
        assert handoff.exists() and publication.exists()
    finally:
        code, out, _ = stop(child, sig)
    assert code == 0 and not handoff.exists() and not publication.exists()
    assert out.strip().splitlines()[-1] == "HYPRIAL_GUI_STOPPED {}"


def test_a_fixed_port_is_bound_as_asked(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    argv = [sys.executable, "-m", MODULE, "serve", "--port", str(port), "--root", str(asset_root(tmp_path))]
    child = subprocess.Popen(argv, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert ready_line(child)["port"] == port
    finally:
        assert stop(child)[0] == 0


def test_a_non_loopback_host_is_refused_with_exit_two(tmp_path: Path) -> None:
    for host in ("0.0.0.0", "192.0.2.1", "example.com"):
        code, report = refused(spawn(asset_root(tmp_path), child_env(), "--host", host))
        assert code == 2 and report["ok"] is False and report["code"] == "INVALID_ARGUMENT", host


def test_invalid_configuration_is_one_json_line_on_stderr_and_exit_two(tmp_path: Path) -> None:
    env = child_env()
    del env["HYPRIAL_BACKEND_ENDPOINT"]
    code, report = refused(spawn(asset_root(tmp_path), env))
    assert code == 2
    assert report == {
        "ok": False,
        "code": "INVALID_CONFIG",
        "message": "HYPRIAL_BACKEND_ENDPOINT is required for the GUI backend",
    }
    secret_env = child_env(HYPRIAL_GUI_LAUNCH_GRANT="short-secret-value")
    _, bad_grant = refused(spawn(asset_root(tmp_path), secret_env))
    assert "short-secret-value" not in json.dumps(bad_grant)


def test_an_unusable_asset_root_is_refused_with_exit_two(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, report = refused(spawn(empty, child_env()))
    assert code == 2 and report["code"] == "GUI_ASSETS_UNAVAILABLE"
    assert "index.html" in report["message"]


def test_a_port_that_is_taken_is_a_bind_failure_with_exit_four(tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        argv = [
            sys.executable, "-m", MODULE, "serve", "--port", str(taken.getsockname()[1]),
            "--root", str(asset_root(tmp_path)),
        ]
        child = subprocess.Popen(argv, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        code, report = refused(child)
    assert code == 4 and report["code"] == "GUI_BIND_FAILED"


def test_the_command_requires_serve_and_a_root(tmp_path: Path) -> None:
    for argv in ([sys.executable, "-m", MODULE], [sys.executable, "-m", MODULE, "serve"]):
        done = subprocess.run(argv, env=child_env(), capture_output=True, text=True, timeout=20)
        assert done.returncode == 2
    assert "HYPRIAL_GUI_READY" not in done.stdout


@pytest.mark.parametrize("name", ["--help"])
def test_help_names_the_serve_command_and_prints_no_secret(name: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", MODULE, "serve", name], env=child_env(), capture_output=True, text=True, timeout=20
    )
    assert done.returncode == 0 and "--root" in done.stdout
    assert GRANT not in done.stdout + done.stderr


def test_an_absolute_form_request_target_is_routed_by_its_path(tmp_path: Path) -> None:
    child = spawn(asset_root(tmp_path), child_env())
    try:
        port = ready_line(child)["port"]
        with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
            connection.sendall(
                f"GET http://127.0.0.1:{port}/__gui/healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            raw = b""
            while chunk := connection.recv(4096):
                raw += chunk
    finally:
        stop(child)
    assert raw.startswith(b"HTTP/1.1 200")
    assert b'"dataMode":"hosted"' in raw


def docs_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "swagger"
    directory.mkdir(parents=True)
    (directory / "swagger-ui-bundle.js").write_text("window.SwaggerUIBundle=1;")
    (directory / "swagger-ui.css").write_text(".swagger-ui{}")
    return directory


def fetch(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


def test_developer_tools_are_an_argument_and_the_ready_line_reports_them(tmp_path: Path) -> None:
    root = asset_root(tmp_path)
    (root / "dev-tools" / "docs").mkdir(parents=True)
    (root / "dev-tools" / "tools-a.js").write_text("export const tools = 1;")
    (root / "dev-tools" / "docs" / "swagger-ui-bundle.js").write_text("window.SwaggerUIBundle=1;")
    (root / "dev-tools" / "docs" / "swagger-ui.css").write_text(".swagger-ui{}")
    off = spawn(root, child_env())
    try:
        payload = ready_line(off)
        assert payload["devTools"] is False and payload["apiDocs"] is False
        assert fetch(payload["port"], "/dev-tools/tools-a.js")[0] == 404
        assert fetch(payload["port"], "/docs-assets/swagger-ui.css")[0] == 404
        assert fetch(payload["port"], "/docs")[1] == b"<!doctype html><title>Hyprial</title>"
        assert json.loads(fetch(payload["port"], "/__gui/healthz")[1])["devTools"] is False
    finally:
        assert stop(off)[0] == 0
    on = spawn(root, child_env(), "--dev-tools")
    try:
        payload = ready_line(on)
        assert payload["devTools"] is True and payload["apiDocs"] is True
        assert fetch(payload["port"], "/dev-tools/tools-a.js") == (200, b"export const tools = 1;")
        assert fetch(payload["port"], "/docs-assets/swagger-ui.css") == (200, b".swagger-ui{}")
        assert b"swagger-ui" in fetch(payload["port"], "/docs")[1]
        assert json.loads(fetch(payload["port"], "/__gui/healthz")[1])["devTools"] is True
    finally:
        code, out, err = stop(on)
    assert code == 0
    for text in (out, err):
        assert GRANT not in text and UPSTREAM_TOKEN not in text


def test_an_environment_variable_cannot_turn_developer_tools_on(tmp_path: Path) -> None:
    env = child_env(HYPRIAL_GUI_DEV_TOOLS="1", HYPRIAL_DEV_TOOLS="1", HYPRIAL_REQUEST_HTTP_DEV_SWAGGER="1")
    child = spawn(asset_root(tmp_path), env)
    try:
        payload = ready_line(child)
        assert payload["devTools"] is False
        assert fetch(payload["port"], "/docs-assets/hyprial-docs.js")[0] == 404
    finally:
        assert stop(child)[0] == 0


def test_an_explicit_docs_directory_is_used_or_the_start_is_refused(tmp_path: Path) -> None:
    root = asset_root(tmp_path)
    child = spawn(root, child_env(), "--dev-tools", "--docs-assets", str(docs_directory(tmp_path)))
    try:
        payload = ready_line(child)
        assert payload["apiDocs"] is True
        assert fetch(payload["port"], "/docs-assets/swagger-ui-bundle.js") == (200, b"window.SwaggerUIBundle=1;")
    finally:
        assert stop(child)[0] == 0
    missing = tmp_path / "absent"
    code, report = refused(spawn(root, child_env(), "--dev-tools", "--docs-assets", str(missing)))
    assert (code, report["code"]) == (2, "GUI_ASSETS_UNAVAILABLE")
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "swagger-ui.css").write_text(".swagger-ui{}")
    code, report = refused(spawn(root, child_env(), "--dev-tools", "--docs-assets", str(incomplete)))
    assert (code, report["code"]) == (2, "GUI_ASSETS_UNAVAILABLE")
    code, report = refused(spawn(root, child_env(), "--docs-assets", str(docs_directory(tmp_path / "second"))))
    assert (code, report["code"]) == (2, "INVALID_ARGUMENT")


def test_developer_tools_without_docs_files_start_without_the_docs_page(tmp_path: Path) -> None:
    child = spawn(asset_root(tmp_path), child_env(), "--dev-tools")
    try:
        payload = ready_line(child)
        assert payload["devTools"] is True and payload["apiDocs"] is False
        assert fetch(payload["port"], "/docs")[1] == b"<!doctype html><title>Hyprial</title>"
        assert fetch(payload["port"], "/docs-assets/hyprial-docs.js")[0] == 404
    finally:
        assert stop(child)[0] == 0
