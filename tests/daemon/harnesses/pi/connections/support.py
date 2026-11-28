"""Real controlled-loader processes with private roots and synthetic credentials."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading

from hyprial.daemon.impl.harnesses.pi.loader import PI_CONTROLLED_LOADER
from .test_binding import binding


def sdk_root():
    override = os.environ.get("HYPRIAL_TEST_PI_PACKAGE_ROOT")
    if override:
        return Path(override)
    command = shutil.which("pi")
    if command:
        for parent in Path(command).resolve().parents:
            manifest = parent / "package.json"
            if manifest.is_file() and json.loads(manifest.read_text()).get("name") == "@earendil-works/pi-coding-agent":
                return parent
    raise RuntimeError("real Pi SDK required for connection acceptance")


def options(tmp_path, *, selected=None):
    selected = selected or binding()
    root = tmp_path.resolve()
    paths = {name: root / name for name in ["repo", "home", "projection", "native", "sessions"]}
    for path in paths.values():
        path.mkdir(parents=True)
    (paths["repo"] / ".git").mkdir()
    (paths["projection"] / "settings.json").write_text('{}')
    payload = {
        "piPackageRoot": str(sdk_root()), "cwd": str(paths["repo"]),
        "repositoryRoot": str(paths["repo"]), "homeRoot": str(paths["home"]),
        "projectionRoot": str(paths["projection"]), "nativeRoot": str(paths["native"]),
        "sessionRoot": str(paths["sessions"]), "sessionId": "fixture-session",
        "managedSessionRoot": str(paths["sessions"]), "agentHome": str(root),
        "projectTrusted": True, "requireExistingSession": False,
        "receiptPath": str(paths["sessions"] / ".hyprial-loader-receipts" / (sha256(b"fixture-session").hexdigest() + ".json")),
        "modelProvider": selected.provider_id, "model": selected.model,
        "modelBinding": selected.snapshot(), "modelDefinitions": selected.definitions(),
    }
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(paths["home"]),
           "PI_CODING_AGENT_DIR": str(paths["native"]), "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
           "HYPRIAL_PI_CONNECTION_KEY": selected.key, "NO_PROXY": "*", "no_proxy": "*"}
    return payload, env


def start(payload, env, command="run-rpc"):
    return subprocess.Popen([shutil.which("node"), str(PI_CONTROLLED_LOADER), command, json.dumps(payload)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, cwd=payload["cwd"])


@contextmanager
def running(payload, env):
    process = start(payload, env)
    rows = queue.Queue()
    def drain():
        for line in process.stdout:
            rows.put(json.loads(line))
        rows.put(None)
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        process.stdin.write('{"type":"get_state","id":"ready"}\n')
        process.stdin.flush()
        ready = rows.get(timeout=25)
        assert ready is not None, process.stderr.read()
        assert ready.get("success") is True, ready
        yield process, rows
    finally:
        if process.poll() is None:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        thread.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def refused(payload, env, code, *, command="run-rpc"):
    process = start(payload, env, command)
    stdout, stderr = process.communicate('{"type":"get_state","id":"ready"}\n', timeout=25)
    assert process.returncode != 0, (stdout, stderr)
    assert f"HYPRIAL_PI_START_FAILURE:{code}" in stderr, stderr
    assert "synthetic-pi-test-key" not in stdout + stderr


def reservation(payload):
    return Path(payload["sessionRoot"]) / ".hyprial-session-reservations" / sha256(payload["sessionId"].encode()).hexdigest()
