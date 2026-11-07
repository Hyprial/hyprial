"""Configuration and runtime-directory refusals for the HTTP entry."""

from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest

from hyprial.shell.impl.request_http.config import (
    ConfigFailure,
    RuntimeLock,
    load_config,
    prepare_runtime_dir,
    write_server_json,
)

OWNER = "h01-owner"
ACTOR = "agent:h01-owner:h01-node:operator"


def _config(tmp_path: Path, **overrides: object) -> Path:
    home = tmp_path / "home"
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    for directory in (home, state, runtime):
        directory.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "backend": {"kind": "local-daemon", "socket": str(state / "daemon.sock")},
        "principal": {"kind": "local-operator", "owner": OWNER, "actor": ACTOR},
        "listen": {"host": "127.0.0.1", "port": 0},
        "security": {
            "allowedOrigins": ["http://127.0.0.1:4173"],
            "allowedHosts": ["127.0.0.1", "localhost"],
        },
        "runtime": {"dir": str(runtime)},
    }
    payload.update(overrides)
    path = tmp_path / "request-http.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_minimal_config_fills_the_documented_defaults(tmp_path: Path) -> None:
    path = _config(tmp_path)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "backend": {"kind": "local-daemon", "socket": str(tmp_path / "state" / "daemon.sock")},
                "principal": {"kind": "local-operator", "owner": OWNER, "actor": ACTOR},
                "runtime": {"dir": str(tmp_path / "runtime")},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.allowed_origins == ()
    assert config.allowed_hosts == ("127.0.0.1", "localhost")
    assert config.budgets.max_request_bytes == 262144
    assert config.host == "127.0.0.1"
    assert config.port == 0


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"schemaVersion": 2}, "schemaVersion"),
        ({"backend": {"kind": "remote", "socket": "/tmp/sock"}}, "local-daemon"),
        ({"listen": {"host": "0.0.0.0", "port": 0}}, "listen.host"),
        ({"listen": {"host": "127.0.0.1", "port": 70000}}, "listen.port"),
        ({"security": {"allowedOrigins": ["*"], "allowedHosts": []}}, "wildcard"),
        ({"security": {"allowedOrigins": ["http://x/path"], "allowedHosts": ["127.0.0.1"]}}, "path"),
        (
            {"principal": {"kind": "local-operator", "owner": OWNER, "actor": "agent:other:node:a"}},
            "owner must equal",
        ),
        (
            {"principal": {"kind": "local-operator", "owner": OWNER, "actor": "operator"}},
            "canonical",
        ),
        ({"principal": {"kind": "agent", "owner": OWNER, "actor": ACTOR}}, "local-operator"),
        ({"backend": {"kind": "local-daemon", "socket": "relative.sock"}}, "absolute"),
    ],
)
def test_invalid_values_fail_closed(tmp_path: Path, overrides: dict, message: str) -> None:
    path = _config(tmp_path, **overrides)
    with pytest.raises(ConfigFailure, match=message):
        load_config(path)


def test_secret_keys_and_unknown_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigFailure, match="secret-bearing"):
        load_config(_config(tmp_path, clientToken="x"))
    with pytest.raises(ConfigFailure, match="unknown keys"):
        load_config(_config(tmp_path, unexpected=True))


def test_config_must_be_regular_private_and_not_a_symlink(tmp_path: Path) -> None:
    path = _config(tmp_path)
    path.chmod(0o666)
    with pytest.raises(ConfigFailure, match="group/world writable"):
        load_config(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ConfigFailure, match="symlink"):
        load_config(link)


def test_runtime_lock_is_single_instance_and_releases(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    prepare_runtime_dir(config)
    first = RuntimeLock(config.runtime_dir)
    first.acquire()
    second = RuntimeLock(config.runtime_dir)
    with pytest.raises(ConfigFailure, match="another request-http server"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_runtime_dir_requires_private_mode(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    prepare_runtime_dir(config)
    config.runtime_dir.chmod(0o755)
    with pytest.raises(ConfigFailure, match="0700"):
        prepare_runtime_dir(config)


def test_server_json_is_private_and_atomic(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path))
    prepare_runtime_dir(config)
    target = write_server_json(config.runtime_dir, {"schemaVersion": 1, "token": "x"})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["token"] == "x"
    assert not list(config.runtime_dir.glob(".server.json.*"))
