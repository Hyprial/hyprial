"""Process lifecycle, capability file and startup refusals."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer, StartupRefusal

OWNER = "h01-owner"
NODE = "h01-node"
ACTOR = f"user:{OWNER}"

from tests.integration.request_http.conftest import (  # noqa: E402
    HttpProcess,
    _clean_env,
    signal_group,
    write_config,
)


def _start_invalid(isolation, payload: dict) -> subprocess.CompletedProcess[str]:
    config = write_config(isolation, payload)
    return subprocess.run(
        [sys.executable, "-m", "hyprial.shell.impl.request_http", "serve", "--config", str(config)],
        cwd=isolation.root,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_capability_file_is_private_and_the_endpoint_is_loopback(http_server: HttpProcess) -> None:
    path = http_server.isolation.runtime / "server.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["endpoint"].startswith("http://127.0.0.1:")
    assert document["pid"] == http_server.process.pid
    assert len(document["token"]) == 43
    assert document["httpContractVersion"] == 1
    assert document["backend"]["owner"] == OWNER
    assert document["principal"] == {
        "kind": "local-operator",
        "owner": OWNER,
        "actor": ACTOR,
    }
    assert "token" not in http_server.log_path.read_text(encoding="utf-8")


def test_second_instance_exits_4_without_touching_the_first(http_server: HttpProcess) -> None:
    config = http_server.isolation.root / "request-http.json"
    second = subprocess.run(
        [sys.executable, "-m", "hyprial.shell.impl.request_http", "serve", "--config", str(config)],
        cwd=http_server.isolation.root,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second.returncode == 4
    assert "another request-http server" in second.stderr
    document = json.loads((http_server.isolation.runtime / "server.json").read_text(encoding="utf-8"))
    assert document["pid"] == http_server.process.pid
    status, _, _ = http_server.request("GET", "/readyz")
    assert status == 200


def test_sigterm_drains_and_removes_the_capability_file(isolation, daemon) -> None:
    config = write_config(isolation)
    process = subprocess.Popen(
        [sys.executable, "-m", "hyprial.shell.impl.request_http", "serve", "--config", str(config)],
        cwd=isolation.root,
        env=_clean_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    server_json = isolation.runtime / "server.json"
    deadline = time.monotonic() + 30
    while not server_json.exists() and time.monotonic() < deadline:
        assert process.poll() is None
        time.sleep(0.05)
    assert server_json.exists()
    os.killpg(os.getpgid(process.pid), 15)
    process.wait(timeout=30)
    assert process.returncode == 0
    assert not server_json.exists()


def test_invalid_config_exits_2_without_publishing(isolation, daemon) -> None:
    payload = {
        "schemaVersion": 1,
        "backend": {"kind": "local-daemon", "socket": str(isolation.socket_path)},
        "principal": {"kind": "local-operator", "owner": OWNER, "actor": ACTOR},
        "listen": {"host": "0.0.0.0", "port": 0},
        "runtime": {"dir": str(isolation.runtime)},
    }
    result = _start_invalid(isolation, payload)
    assert result.returncode == 2
    assert "CONFIG_INVALID" in result.stderr
    assert not (isolation.runtime / "server.json").exists()


def test_unreachable_backend_exits_3(isolation) -> None:
    payload = {
        "schemaVersion": 1,
        "backend": {"kind": "local-daemon", "socket": str(isolation.state / "missing.sock")},
        "principal": {"kind": "local-operator", "owner": OWNER, "actor": ACTOR},
        "runtime": {"dir": str(isolation.runtime)},
    }
    result = _start_invalid(isolation, payload)
    assert result.returncode == 3
    assert "BACKEND_UNAVAILABLE" in result.stderr


def test_actor_from_another_owner_exits_2(isolation, daemon) -> None:
    payload = {
        "schemaVersion": 1,
        "backend": {"kind": "local-daemon", "socket": str(isolation.socket_path)},
        "principal": {"kind": "local-operator", "owner": OWNER, "actor": "user:other-owner"},
        "runtime": {"dir": str(isolation.runtime)},
    }
    result = _start_invalid(isolation, payload)
    assert result.returncode == 2
    assert "CONFIG_INVALID" in result.stderr


def test_declaration_mismatch_refuses_before_binding(tmp_path: Path) -> None:
    config = RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"),
        owner=OWNER,
        actor=ACTOR,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=tmp_path / "runtime",
        source=tmp_path / "config.json",
    )
    (tmp_path / "runtime").mkdir(mode=0o700)
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = "available" if entry["httpExposed"] else "disabled"
        item["callable"] = item["state"] == "available"
        item["reason"] = None
        entries.append(item)
    entries[0]["owner"] = "daemon.other"

    def request_once(method: str, params: dict, **_kwargs):
        if method == "ping":
            return {
                "version": __version__,
                "nodeId": NODE,
                "owner": OWNER,
                "epoch": "epoch-1",
                "phase": "serving",
            }
        return {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": "epoch-1",
            "packageVersion": __version__,
            "methods": entries,
        }

    server = RequestHttpServer(config, request_once=request_once)
    with pytest.raises(StartupRefusal, match="declarations differ"):
        server.start()
    assert not (tmp_path / "runtime" / "server.json").exists()


def _local_gui_config(isolation, *, owner: str | None, actor: str | None) -> Path:
    document: dict = {
        "schemaVersion": 1,
        "backend": {"kind": "local-daemon", "socket": str(isolation.socket_path)},
        "principal": {"kind": "local-gui", "owner": owner, "actor": actor},
        "listen": {"host": "127.0.0.1", "port": 0},
        "security": {
            "allowedOrigins": ["http://127.0.0.1:4173"],
            "allowedHosts": ["127.0.0.1", "localhost"],
        },
        "runtime": {"dir": str(isolation.runtime)},
        "home": str(isolation.home),
        "stateDir": str(isolation.state),
    }
    return write_config(isolation, document)


def test_a_local_gui_config_is_refused_by_the_standalone_entry_and_hosts_nothing(isolation) -> None:
    """The standalone entry serves no local-gui config, whatever the home holds.

    It exits 2 with ``CONFIG_INVALID``: it publishes no ``server.json``, writes no
    ``bootstrap.json`` and leaves the state locks free for a real daemon.
    """

    from hyprial.daemon import DaemonStateOwnershipFence

    (isolation.home / "settings.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    isolation.env.pop("HYPRIAL_OWNER", None)
    isolation.env.pop("HYPRIAL_USER_ID", None)
    config = _local_gui_config(isolation, owner=None, actor=None)

    process = subprocess.Popen(
        [sys.executable, "-m", "hyprial.shell.impl.request_http", "serve", "--config", str(config)],
        cwd=isolation.root,
        env=isolation.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        signal_group(process, hard=True)
        process.communicate(timeout=30)
        raise
    finally:
        if process.poll() is None:
            signal_group(process, hard=True)
            process.wait(timeout=10)

    assert process.returncode == 2
    assert "CONFIG_INVALID" in stderr
    assert not (isolation.runtime / "server.json").exists()
    assert not (isolation.state / "bootstrap.json").exists()
    DaemonStateOwnershipFence.acquire(isolation.state, timeout=0).close()
