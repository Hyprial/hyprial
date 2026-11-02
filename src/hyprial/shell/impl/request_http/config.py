"""Configuration, runtime directory, single-instance lock and capability file."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any
from urllib.parse import urlsplit

from hyprial.kernel import canonical_agent_uri, parse_agent_uri

CONFIG_KEYS = frozenset(
    {"schemaVersion", "backend", "principal", "listen", "security", "budgets", "runtime"}
)
BUDGET_KEYS = frozenset(
    {
        "requestTimeoutSeconds",
        "headerTimeoutSeconds",
        "idleTimeoutSeconds",
        "maxRequestBytes",
        "maxResponseBytes",
        "maxConcurrentRequests",
        "shutdownDrainSeconds",
    }
)
BUDGET_RANGES = {
    "requestTimeoutSeconds": (1, 120),
    "headerTimeoutSeconds": (1, 60),
    "idleTimeoutSeconds": (1, 300),
    "maxRequestBytes": (1, 16 * 1024 * 1024),
    "maxResponseBytes": (1, 64 * 1024 * 1024),
    "maxConcurrentRequests": (1, 64),
    "shutdownDrainSeconds": (1, 30),
}
DEFAULT_BUDGETS = {
    "requestTimeoutSeconds": 15,
    "headerTimeoutSeconds": 5,
    "idleTimeoutSeconds": 30,
    "maxRequestBytes": 262144,
    "maxResponseBytes": 8388608,
    "maxConcurrentRequests": 8,
    "shutdownDrainSeconds": 5,
}
_SECRET_WORDS = ("token", "secret", "password")


class ConfigFailure(Exception):
    """A startup refusal carrying its process exit code and stable code."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        self.code = code
        self.message = str(message)
        self.exit_code = exit_code
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class Budgets:
    request_timeout: float
    header_timeout: float
    idle_timeout: float
    max_request_bytes: int
    max_response_bytes: int
    max_concurrent: int
    shutdown_drain: float


@dataclass(frozen=True, slots=True)
class RequestHttpConfig:
    socket_path: Path
    owner: str
    actor: str
    host: str
    port: int
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    budgets: Budgets
    runtime_dir: Path
    source: Path


def _reject_secret_keys(value: Any, where: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in _SECRET_WORDS):
                raise ConfigFailure("CONFIG_INVALID", f"{where}: secret-bearing key {key!r} is not allowed")
            _reject_secret_keys(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_keys(item, f"{where}[{index}]")



def _object(
    value: Any, keys: frozenset[str], where: str, *, required: frozenset[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigFailure("CONFIG_INVALID", f"{where} must be an object")
    unknown = set(value) - keys
    missing = (keys if required is None else required) - set(value)
    if unknown:
        raise ConfigFailure("CONFIG_INVALID", f"{where}: unknown keys {sorted(unknown)}")
    if missing:
        raise ConfigFailure("CONFIG_INVALID", f"{where}: missing keys {sorted(missing)}")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigFailure("CONFIG_INVALID", f"{where} must be a non-empty string")
    return value


def _absolute(value: Any, where: str) -> Path:
    text = _string(value, where)
    path = Path(text)
    if not path.is_absolute():
        raise ConfigFailure("CONFIG_INVALID", f"{where} must be an absolute path")
    return path


def _origin(value: Any, where: str) -> str:
    text = _string(value, where)
    if text in {"*", "null"}:
        raise ConfigFailure("CONFIG_INVALID", f"{where} cannot be a wildcard origin")
    try:
        parts = urlsplit(text)
    except ValueError as error:
        raise ConfigFailure("CONFIG_INVALID", f"{where} is not a valid origin") from error
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigFailure("CONFIG_INVALID", f"{where} must be an http(s) origin")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigFailure("CONFIG_INVALID", f"{where} must not carry a path, query or credentials")
    return text


def _host(value: Any, where: str) -> str:
    text = _string(value, where)
    if any(char in text for char in "/*:") or text.strip() != text:
        raise ConfigFailure("CONFIG_INVALID", f"{where} must be a bare hostname")
    return text


def _actor(value: Any, owner: str) -> str:
    text = _string(value, "principal.actor")
    parsed = parse_agent_uri(text)
    if parsed is None:
        raise ConfigFailure(
            "CONFIG_INVALID",
            "principal.actor must be a canonical agent:<owner>:<node>:<actor> URI",
        )
    if canonical_agent_uri(*parsed) != text:
        raise ConfigFailure("CONFIG_INVALID", "principal.actor is not canonical")
    if parsed[0] != owner:
        raise ConfigFailure(
            "CONFIG_INVALID", "principal.actor owner must equal principal.owner"
        )
    return text


def _budgets(value: Any) -> Budgets:
    data = _object(value, BUDGET_KEYS, "budgets")
    resolved: dict[str, int] = {}
    for name, (low, high) in BUDGET_RANGES.items():
        item = data[name]
        if type(item) is not int or not low <= item <= high:
            raise ConfigFailure(
                "CONFIG_INVALID", f"budgets.{name} must be an integer {low}..{high}"
            )
        resolved[name] = item
    return Budgets(
        request_timeout=float(resolved["requestTimeoutSeconds"]),
        header_timeout=float(resolved["headerTimeoutSeconds"]),
        idle_timeout=float(resolved["idleTimeoutSeconds"]),
        max_request_bytes=resolved["maxRequestBytes"],
        max_response_bytes=resolved["maxResponseBytes"],
        max_concurrent=resolved["maxConcurrentRequests"],
        shutdown_drain=float(resolved["shutdownDrainSeconds"]),
    )


def load_config(path: Path) -> RequestHttpConfig:
    """Parse and fully validate one explicit configuration file."""

    try:
        info = path.lstat()
    except OSError as error:
        raise ConfigFailure("CONFIG_INVALID", f"cannot stat config {path}: {error.strerror}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigFailure("CONFIG_INVALID", "config must be a regular file, not a symlink")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigFailure("CONFIG_INVALID", "config must not be group/world writable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigFailure("CONFIG_INVALID", f"cannot read config: {error}") from error

    _reject_secret_keys(payload, "config")
    root = _object(
        payload,
        CONFIG_KEYS,
        "config",
        required=frozenset({"schemaVersion", "backend", "principal", "runtime"}),
    )
    if root["schemaVersion"] != 1:
        raise ConfigFailure("CONFIG_INVALID", "config.schemaVersion must be 1")

    backend = _object(root["backend"], frozenset({"kind", "socket"}), "backend")
    if backend["kind"] != "local-daemon":
        raise ConfigFailure("CONFIG_INVALID", "backend.kind must be local-daemon")
    socket_path = _absolute(backend["socket"], "backend.socket")

    principal = _object(root["principal"], frozenset({"kind", "owner", "actor"}), "principal")
    if principal["kind"] != "local-operator":
        raise ConfigFailure("UNSUPPORTED_PRINCIPAL", "principal.kind must be local-operator", exit_code=3)
    owner = _string(principal["owner"], "principal.owner")
    if ":" in owner:
        raise ConfigFailure("CONFIG_INVALID", "principal.owner must not contain ':'")
    actor = _actor(principal["actor"], owner)

    listen = _object(
        root.get("listen", {"host": "127.0.0.1", "port": 0}),
        frozenset({"host", "port"}),
        "listen",
    )
    if listen["host"] != "127.0.0.1":
        raise ConfigFailure("CONFIG_INVALID", "listen.host must be 127.0.0.1")
    port = listen["port"]
    if type(port) is not int or not 0 <= port <= 65535:
        raise ConfigFailure("CONFIG_INVALID", "listen.port must be an integer 0..65535")

    security = _object(
        root.get(
            "security",
            {"allowedOrigins": [], "allowedHosts": ["127.0.0.1", "localhost"]},
        ),
        frozenset({"allowedOrigins", "allowedHosts"}),
        "security",
    )
    origins = security["allowedOrigins"]
    hosts = security["allowedHosts"]
    if not isinstance(origins, list):
        raise ConfigFailure("CONFIG_INVALID", "security.allowedOrigins must be a list")
    allowed_origins = tuple(
        _origin(item, f"security.allowedOrigins[{index}]")
        for index, item in enumerate(origins)
    )
    if not isinstance(hosts, list) or not hosts:
        raise ConfigFailure("CONFIG_INVALID", "security.allowedHosts must be a non-empty list")
    if len(set(allowed_origins)) != len(allowed_origins):
        raise ConfigFailure("CONFIG_INVALID", "security.allowedOrigins must be unique")
    allowed_hosts = tuple(
        _host(item, f"security.allowedHosts[{index}]")
        for index, item in enumerate(hosts)
    )
    if len(set(allowed_hosts)) != len(allowed_hosts):
        raise ConfigFailure("CONFIG_INVALID", "security.allowedHosts must be unique")

    budgets = _budgets(root.get("budgets", dict(DEFAULT_BUDGETS)))
    runtime = _object(root["runtime"], frozenset({"dir"}), "runtime")
    runtime_dir = _absolute(runtime["dir"], "runtime.dir")

    return RequestHttpConfig(
        socket_path=socket_path,
        owner=owner,
        actor=actor,
        host="127.0.0.1",
        port=port,
        allowed_origins=allowed_origins,
        allowed_hosts=allowed_hosts,
        budgets=budgets,
        runtime_dir=runtime_dir,
        source=path,
    )


def prepare_runtime_dir(config: RequestHttpConfig) -> None:
    """Create/validate the private runtime directory (0700, current uid)."""

    directory = config.runtime_dir
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
    except OSError as error:
        raise ConfigFailure(
            "RUNTIME_UNAVAILABLE", f"cannot prepare runtime dir: {error.strerror}", exit_code=4
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigFailure("RUNTIME_UNAVAILABLE", "runtime.dir must be a real directory", exit_code=4)
    if info.st_uid != os.getuid():
        raise ConfigFailure("RUNTIME_UNAVAILABLE", "runtime.dir must be owned by the current uid", exit_code=4)
    if info.st_mode & 0o077:
        raise ConfigFailure("RUNTIME_UNAVAILABLE", "runtime.dir must be mode 0700", exit_code=4)


class RuntimeLock:
    """One server per runtime directory; never steals a live listener."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "server.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as error:
            raise ConfigFailure(
                "RUNTIME_UNAVAILABLE", f"cannot open server lock: {error.strerror}", exit_code=4
            ) from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise ConfigFailure(
                    "RUNTIME_UNAVAILABLE",
                    "another request-http server owns this runtime dir",
                    exit_code=4,
                ) from error
            raise ConfigFailure(
                "RUNTIME_UNAVAILABLE", f"cannot lock server.lock: {error.strerror}", exit_code=4
            ) from error
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def server_json_path(directory: Path) -> Path:
    return directory / "server.json"


def write_server_json(directory: Path, document: dict[str, Any]) -> Path:
    """Atomically publish the 0600 capability file."""

    target = server_json_path(directory)
    temporary = directory / f".server.json.{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return target


def remove_server_json(directory: Path) -> None:
    try:
        server_json_path(directory).unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "Budgets",
    "ConfigFailure",
    "RequestHttpConfig",
    "RuntimeLock",
    "load_config",
    "prepare_runtime_dir",
    "remove_server_json",
    "server_json_path",
    "write_server_json",
]
