"""The disposable container owns its GUI, ingress, and handoff; ``hyprial gui`` ensures the daemon.

The runtime root is a short TMPDIR-backed directory, never ``tmp_path``: the
entrypoint appends ``/state/daemon.sock``, and the impact-sharded CI basetemp
(``...-shard-Nof4/popen-gwK/...``) pushed the combined path past the kernel's
AF_UNIX ``sun_path`` limit. The fake daemon then died with ``OSError: AF_UNIX
path too long``, the entrypoint exited before readiness, and the failure read
as an empty line. An early exit now reports the captured stderr instead.

Decision record (Z-7 A3): the entrypoint no longer runs ``daemon run`` or waits
for the socket itself; ``hyprial gui`` ensures the daemon, so the fake launcher
binds the socket from ``gui`` and any ``daemon`` invocation by the script fails.
"""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.request
import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.windows_suite.host import pid_alive


DOCKER_FILES = Path(__file__).resolve().parents[3] / "scripts/build/docker"


@pytest.fixture
def runtime_root() -> Iterator[Path]:
    """A short AF_UNIX root (pytest's basetemp can exceed the path limit)."""

    path = Path(tempfile.mkdtemp(prefix="hyprial-gui-", dir=hyprial_test_tmp_root()))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_container_entrypoint_does_not_start_its_own_daemon() -> None:
    script = (DOCKER_FILES / "gui-debug-container-entrypoint.sh").read_text(encoding="utf-8")
    assert "daemon run" not in script
    assert "daemon.sock" not in script


@pytest.mark.parametrize("mode", ["normal", "dev"])
def test_container_entrypoint_starts_gui_which_ensures_the_daemon_and_reclaims_it(
    tmp_path: Path, runtime_root: Path, mode: str
) -> None:
    source = tmp_path / "source"
    bin_dir = source / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    scripts = source / "scripts/build/docker"
    scripts.mkdir(parents=True)
    shutil.copy2(DOCKER_FILES / "gui-debug-proxy.py", scripts / "gui-debug-proxy.py")
    cli = bin_dir / "hyprial"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, socket, sys, time\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from pathlib import Path\n"
        "runtime = Path(os.environ['HYPRIAL_GUI_RUNTIME_ROOT'])\n"
        "if sys.argv[1] == 'daemon':\n"
        "  sys.exit(97)\n"
        "if sys.argv[1] == 'gui':\n"
        "  assert os.environ['HYPRIAL_GUI_BACKEND_DRIVER'] == 'daemon'\n"
        "  sock = socket.socket(socket.AF_UNIX)\n"
        "  sock.bind(os.environ['HARNESS_STATE_DIR'] + '/daemon.sock')\n"
        "  sock.listen()\n"
        "  class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "      self.send_response(200); self.end_headers(); self.wfile.write(b'GUI-READY')\n"
        "    def log_message(self, *args): pass\n"
        "  port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "  server = ThreadingHTTPServer(('127.0.0.1', port), Handler)\n"
        "  handoff = runtime / 'private.url'\n"
        "  handoff.write_text(f'http://127.0.0.1:{port}/#grant=secret\\n')\n"
        "  (runtime / 'gui.pid').write_text(str(os.getpid()))\n"
        "  print(json.dumps({'ok': True, 'handoff_path': str(handoff)}), flush=True)\n"
        "  signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "  server.serve_forever()\n"
    )
    cli.chmod(0o755)
    runtime = runtime_root
    ingress_port = free_port()
    app_port = free_port()
    env = dict(os.environ)
    env.update(
        HYPRIAL_GUI_SOURCE_ROOT=str(source),
        HYPRIAL_GUI_RUNTIME_ROOT=str(runtime),
        HYPRIAL_GUI_INGRESS_PORT=str(ingress_port),
        HYPRIAL_GUI_APP_PORT=str(app_port),
        HYPRIAL_GUI_PUBLIC_PORT="4382",
    )
    process = subprocess.Popen(
        ("bash", str(DOCKER_FILES / "gui-debug-container-entrypoint.sh"), mode),
        cwd=source,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 15)
        assert ready, "container entrypoint did not become ready"
        line = process.stdout.readline()
        if not line:
            process.wait(timeout=5)
            assert process.stderr is not None
            raise AssertionError(
                "container entrypoint exited before readiness: "
                f"rc={process.returncode} stderr={process.stderr.read()!r}"
            )
        assert "GUI_CONTAINER_READY" in line
        assert "secret" not in line
        handoff = runtime / "public-launch.url"
        assert handoff.read_text() == "http://127.0.0.1:4382/#grant=secret\n"
        assert handoff.stat().st_mode & 0o777 == 0o600
        request = urllib.request.Request(
            f"http://127.0.0.1:{ingress_port}/",
            headers={"Host": "127.0.0.1:4382"},
        )
        assert urllib.request.urlopen(request, timeout=2).read() == b"GUI-READY"
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
    for name in ("gui.pid",):
        pid = int((runtime / name).read_text())
        if not pid_alive(pid):
            continue
        raise AssertionError(f"{name} remained alive after entrypoint exit")
