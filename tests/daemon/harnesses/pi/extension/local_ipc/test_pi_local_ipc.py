"""The shared Node local-IPC module against the frozen endpoint contract.

These tests drive the shipped ``pi_local_ipc.ts`` / ``pi_daemon_ipc.ts``
under plain ``node`` (>= 22.6 strips types natively):

* every endpoint-file sample in ``contract/wire/local-ipc-endpoint-samples.json``
  resolves or rejects exactly as the contract says, with ``platform`` forced
  to ``win32`` so non-Windows machines exercise the Windows branch;
* non-win32 platforms return the path untouched without reading anything;
* ``pi_daemon_ipc.callDaemon`` with ``process.platform`` forced to ``win32``
  connects to the pipe NAME the endpoint file carries, not to the endpoint
  file path itself.  On POSIX a ``\\\\.\\pipe\\hyprial-<hex>`` argument is one
  cwd-relative path component, so the test binds a real unix socket under
  that literal name and a successful whoami round trip proves the target.

Skipped when node is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.rpc_client import PI_HARNESS_BRIDGE_EXTENSION

PI_LOCAL_IPC = PI_HARNESS_BRIDGE_EXTENSION.with_name("pi_local_ipc.ts")
PI_DAEMON_IPC = PI_HARNESS_BRIDGE_EXTENSION.with_name("pi_daemon_ipc.ts")
CONTRACT = (
    Path(__file__).resolve().parents[6] / "contract" / "wire"
)
SAMPLES = json.loads(
    (CONTRACT / "local-ipc-endpoint-samples.json").read_text(encoding="utf-8")
)

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")

ACTOR = "agent:op:node-1:pi-worker"
SESSION_REF = "sess-local-ipc-1"
PIPE_SUFFIX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
PIPE_NAME = "\\\\.\\pipe\\hyprial-" + PIPE_SUFFIX

RESOLVE_DRIVER = """
import { resolveLocalIpcEndpoint } from "./pi_local_ipc.ts";

const [endpointPath, platform] = process.argv.slice(2);
try {
  process.stdout.write(
    JSON.stringify({ ok: true, endpoint: resolveLocalIpcEndpoint(endpointPath, platform) }) + "\\n",
  );
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error) }) + "\\n");
}
"""

CALL_DRIVER = """
Object.defineProperty(process, "platform", { value: "win32" });
const { callDaemon } = await import("./pi_daemon_ipc.ts");
try {
  const result = await callDaemon(
    "identity.whoami",
    { actor: process.env.HYPRIAL_WORKER_ACTOR, sessionRef: process.env.HYPRIAL_WORKER_SESSION_REF },
    false,
  );
  process.stdout.write(JSON.stringify({ ok: true, result }) + "\\n");
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error) }) + "\\n");
}
"""


def _stage(workdir: Path, driver_name: str, driver_source: str) -> None:
    for source in (PI_LOCAL_IPC, PI_DAEMON_IPC):
        shutil.copy(source, workdir / source.name)
    (workdir / driver_name).write_text(driver_source, encoding="utf-8")


def _run_node(workdir: Path, driver: str, *args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    assert NODE is not None
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        **(env or {}),
    }
    completed = subprocess.run(
        [NODE, driver, *args],
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip())


@pytest.mark.parametrize("case", SAMPLES["cases"], ids=lambda case: case["id"])
def test_win32_samples_resolve_or_reject(case: dict[str, object], tmp_path: Path) -> None:
    _stage(tmp_path, "resolve_driver.ts", RESOLVE_DRIVER)
    endpoint_file = tmp_path / "endpoint"
    endpoint_file.write_text(str(case["file"]), encoding="utf-8")
    payload = _run_node(tmp_path, "resolve_driver.ts", str(endpoint_file), "win32")
    if case["expect"] == "resolve":
        assert payload["ok"] is True, payload
        assert payload["endpoint"] == case["endpoint"]
    else:
        assert payload["ok"] is False, payload
        assert "invalid local Windows IPC endpoint" in str(payload["error"])


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_non_win32_returns_the_path_without_reading_it(
    platform: str, tmp_path: Path
) -> None:
    _stage(tmp_path, "resolve_driver.ts", RESOLVE_DRIVER)
    missing = tmp_path / "no-such-endpoint"
    payload = _run_node(tmp_path, "resolve_driver.ts", str(missing), platform)
    assert payload == {"ok": True, "endpoint": str(missing)}


class _PipeNameDaemon:
    """A version-1 IPC responder bound under the literal pipe-name path."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.frames: list[dict[str, object]] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            with connection:
                line = b""
                while b"\n" not in line:
                    chunk = connection.recv(64 * 1024)
                    if not chunk:
                        return
                    line += chunk
                frame = json.loads(line.partition(b"\n")[0])
                self.frames.append(frame)
                connection.sendall(
                    json.dumps(
                        {
                            "version": 1,
                            "id": frame.get("id"),
                            "result": {
                                "ok": True,
                                "actor": (frame.get("params") or {}).get("actor"),
                                "sessionRegistered": True,
                            },
                        },
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )

    def close(self) -> None:
        if self._server.fileno() >= 0:
            try:
                self._server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._server.close()
        self._thread.join(timeout=1)
        self.socket_path.unlink(missing_ok=True)


def _win32_call_rig(endpoint_document: str) -> tuple[Path, _PipeNameDaemon]:
    workdir = Path(tempfile.mkdtemp(prefix="pi-w32-", dir="/tmp"))
    state_dir = workdir / "state"
    state_dir.mkdir()
    (state_dir / "daemon.sock").write_text(endpoint_document, encoding="utf-8")
    _stage(workdir, "call_driver.ts", CALL_DRIVER)
    daemon = _PipeNameDaemon(workdir / PIPE_NAME)
    return workdir, daemon


def test_win32_call_daemon_connects_to_the_pipe_name() -> None:
    workdir, daemon = _win32_call_rig(
        json.dumps({"transport": "windows-pipe-v1", "pipe": PIPE_NAME})
    )
    try:
        payload = _run_node(
            workdir,
            "call_driver.ts",
            env={
                "HYPRIAL_WORKER_ACTOR": ACTOR,
                "HYPRIAL_WORKER_SESSION_REF": SESSION_REF,
                "HARNESS_STATE_DIR": str(workdir / "state"),
            },
        )
        assert payload["ok"] is True, payload
        result = payload["result"]
        assert isinstance(result, dict)
        assert result["actor"] == ACTOR
        assert result["sessionRegistered"] is True
        (frame,) = daemon.frames
        assert frame["method"] == "identity.whoami"
        assert frame["params"]["actor"] == ACTOR
    finally:
        daemon.close()
        shutil.rmtree(workdir, ignore_errors=True)


def test_win32_call_daemon_rejects_an_invalid_endpoint_file() -> None:
    workdir, daemon = _win32_call_rig(
        json.dumps({"transport": "unix-socket", "pipe": PIPE_NAME})
    )
    try:
        payload = _run_node(
            workdir,
            "call_driver.ts",
            env={
                "HYPRIAL_WORKER_ACTOR": ACTOR,
                "HYPRIAL_WORKER_SESSION_REF": SESSION_REF,
                "HARNESS_STATE_DIR": str(workdir / "state"),
            },
        )
        assert payload["ok"] is False, payload
        assert "invalid local Windows IPC endpoint" in str(payload["error"])
        assert daemon.frames == []
    finally:
        daemon.close()
        shutil.rmtree(workdir, ignore_errors=True)
