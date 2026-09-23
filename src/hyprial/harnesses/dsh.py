"""DeepSeek Harness (DSH) HTTP-session adapter.

DSH already owns the model process, conversation state, and tool runtime.  This
adapter turns its unary HTTP API into HYPRIAL's small ``TurnClient`` seam: create or
resume one DSH session, queue prompts, observe ``turn/end``, and cancel the
active turn on interrupt.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import json
import multiprocessing
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.parse import urlparse
from uuid import uuid4

import yaml

from hyprial._dsh_resolver import resolve_hostname
from hyprial.daemon.desired_state import HarnessLaunchSpec

from .owned_process import OwnedProcessGroup
from .streaming import (
    StreamingTurnProcess,
    TurnClientFactory,
    resolve_turn_timeout_seconds,
)
from .worker_channel import WorkerChannel
from .model_provider import dsh_provider_id, validate_model_selection

# The single DSH HTTP request timeout.  It is also the banner read budget:
# the banner is how the OS-assigned port comes back and is one request's worth
# of waiting, not a second policy.
DSH_REQUEST_TIMEOUT_SECONDS = 15.0

# Declared start budget for the launcher's readiness wait.  ``wait_ready``
# resolves only after ``DshApiClient.__aenter__`` returns, whose readiness path
# is a sequence of request-bounded steps: the banner read, ``host.describe``,
# ``agentPreset.copy`` (worker channel), ``session.create``/``session.history``,
# ``session.models`` and ``session.selectModel``.  Six sequential
# ``DSH_REQUEST_TIMEOUT_SECONDS`` waits plus a margin is therefore the real
# composition; ``actor_runtime`` exposes stop/drain/shutdown timeouts and
# restart budgets but no request-level deadline primitive (the same reason
# codex's ``APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT`` is declared here).
# Registered in ``tests/supervision_exemptions.json``.
DSH_STARTUP_TIMEOUT_SECONDS_DEFAULT = DSH_REQUEST_TIMEOUT_SECONDS * 6 + 15.0

# Granularity of the banner wait: a poll slice, not a budget.
DSH_BANNER_POLL_SECONDS = 0.1

# Bounded wait for a closed or rejected generation's process group to be
# reaped.  Part of the start/stop handshake, so it is registered in
# ``tests/supervision_exemptions.json`` alongside the declared start budget.
DSH_STARTUP_REAP_SECONDS = 2.0

_DSH_WEB_BANNER = re.compile(r"^dsh web: http://127\.0\.0\.1:(\d+)\s*$")
_MCP_PLUGIN_PACKAGE = "@deepseek-ai/dsh-mcp-client"
_DSH_IO_TAIL_BYTES = 64 * 1024
#: An unterminated line cannot buffer without bound; ``readline(size)`` caps it.
_DSH_MAX_LINE_BYTES = 64 * 1024
_DSH_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
# Old substring set (kept verbatim) UNION the boundary-limited ``key``/``auth``
# set.  The union is deliberate: the substring set catches names like
# ``DEEPSEEKAPIKEY`` / ``ACCESSTOKEN`` / ``BEARERTOKEN`` / ``API-KEY`` /
# ``DB_PASSWORDS``, the boundary set catches ``MY_KEY`` / ``SERVICE_AUTH``.
_SECRET_ENV_NAME = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|passwd|credential"
    r"|(?:^|_)(?:key|auth)(?:$|_))"
)


def _environment_secrets(environment: Mapping[str, str]) -> tuple[str, ...]:
    """The env values that must never appear in output, longest first."""

    values = {
        value
        for name, value in environment.items()
        if value and len(value) >= 8 and _SECRET_ENV_NAME.search(name)
    }
    return tuple(sorted(values, key=len, reverse=True))


def dsh_worker_home(state_dir: Path, name: str) -> Path:
    """The fixed private ``DSH_HOME`` for one managed dsh worker.

    ``DSH_HOME`` holds the session ``storages/``, the copied agent preset,
    and any user patch, so it must never be shared between workers.  The
    daemon already owns the only state root (``worker_channel.state_dir`` /
    ``DaemonApplication.state_dir``); this names the per-worker directory
    under it.  The name is sanitized because a DSH profile directory is a
    path component.
    """

    safe = _DSH_UNSAFE_NAME.sub("-", name)
    if not safe or safe in {".", ".."}:
        raise ValueError("DSH worker name cannot form a directory component")
    return Path(state_dir) / "dsh" / safe / "home"


def _validate_client_arguments(spec: HarnessLaunchSpec) -> None:
    """Parse the DSH client's own budgets before any child exists.

    A malformed ``--poll-interval`` / ``--turn-timeout`` (or malformed
    ``HYPRIAL_TURN_TIMEOUT_SECONDS``) is deterministic: it must fail once at
    construction instead of being re-parsed on every retry while the pump
    spawns a fresh child each time.
    """

    _positive_float(
        _option_value(spec.args, "--poll-interval"),
        default=0.25,
        label="--poll-interval",
    )
    _positive_float(
        _option_value(spec.args, "--turn-timeout"),
        default=resolve_turn_timeout_seconds(
            spec.turn_timeout_seconds,
            default=MANAGED_TURN_TIMEOUT_SECONDS,
        ),
        label="--turn-timeout",
    )


def _iter_pipe_lines(stream: Any) -> Iterator[bytes]:
    """Bounded line reader for a child pipe.

    ``iter(stream.readline, b"")`` waits for a newline and buffers the whole
    line first, so one newline-free megabyte would sit in memory; the sized
    ``readline`` returns partial chunks instead.
    """

    while True:
        line = stream.readline(_DSH_MAX_LINE_BYTES)
        if not line:
            return
        yield line


def _close_stream(stream: Any) -> None:
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _close_stream_async(stream: Any) -> None:
    """Close a pipe without waiting on a drain thread's read lock.

    A drain thread blocked in ``readline`` holds the buffered reader's lock,
    so a synchronous ``close()`` waits until the last writer closes the pipe.
    A grandchild that outlived the child outside its process group can hold
    that write end far longer than any stop budget (measured 29.3s against a
    30s surviving grandchild), so close on a daemon thread: stop stays
    bounded and the fd is released when the write end actually closes.
    """

    threading.Thread(target=_close_stream, args=(stream,), daemon=True).start()


def _worker_web_patch() -> str:
    """The managed user patch that turns off the Web GUI surface context.

    A patch replaces the targeted row's whole ``config``, so all three
    ``web-runtime`` keys are restated.  ``printUrl`` must stay true: the
    banner it prints is the only machine-readable port channel.
    """

    rows = [
        {
            "id": "web-runtime",
            "config": {
                "printUrl": True,
                "surfaceContext": False,
                "trustedHosts": [],
            },
        }
    ]
    return yaml.safe_dump(rows, allow_unicode=True, default_flow_style=False, sort_keys=False)


def prepare_worker_home(home: Path) -> None:
    """Create the worker home and pin its managed Web-profile patch."""

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    patch = home / "profiles" / "web" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = _worker_web_patch()
    try:
        if patch.read_text(encoding="utf-8") == content:
            return
    except (OSError, UnicodeError):
        pass
    temporary = patch.with_name(f".{patch.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(patch)
    finally:
        temporary.unlink(missing_ok=True)

# Retired wall-clock cap (#277: no timeout kills a turn).  The value is
# still resolved so the pre-existing ``--turn-timeout`` argument and the
# persisted ``turnTimeoutSeconds`` field keep parsing (old launch specs
# and callers must not crash), but nothing enforces it.
MANAGED_TURN_TIMEOUT_SECONDS = 3600.0


class DshApiError(RuntimeError):
    """DSH transport or RPC envelope failure."""


class DshApi(Protocol):
    async def call(self, method: str, payload: dict[str, object]) -> object: ...


class _ResolverRequest:
    """One hostname lookup behind a process boundary that can be terminated."""

    def __init__(self, host: str, port: int) -> None:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        self._receiver = receiver
        self._process = context.Process(
            target=resolve_hostname,
            args=(sender, host, port),
            name="hyprial-dsh-resolver",
            daemon=True,
        )
        self._lock = threading.Lock()
        self._closed = False
        self._process.start()
        sender.close()

    def result(self, timeout: float) -> tuple[tuple[Any, ...], ...]:
        try:
            if not self._receiver.poll(timeout):
                raise DshApiError("DSH hostname resolution timed out")
            message = self._receiver.recv()
        except (EOFError, OSError) as error:
            raise DshApiError("DSH hostname resolution was cancelled") from error
        if not isinstance(message, tuple) or not message:
            raise DshApiError("DSH hostname resolver returned an invalid result")
        if message[0] is not True:
            kind = message[1] if len(message) > 1 else "resolver error"
            detail = message[2] if len(message) > 2 else "unknown failure"
            raise DshApiError(f"DSH hostname resolution failed: {kind}: {detail}")
        addresses = message[1] if len(message) > 1 else None
        if not isinstance(addresses, list) or not addresses:
            raise DshApiError("DSH hostname resolved to no addresses")
        return tuple(tuple(address) for address in addresses)

    def cancel(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(0.5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(0.5)
        self._receiver.close()
        if not self._process.is_alive():
            self._process.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        self._process.join(0.1)
        if self._process.is_alive():
            self.cancel()
            return
        with self._lock:
            self._closed = True
        self._receiver.close()
        self._process.close()


def _start_resolver(host: str, port: int) -> _ResolverRequest:
    return _ResolverRequest(host, port)


class DshHttpApi:
    """Dependency-free client for DSH's ``POST /api/<method>`` envelope.

    Each request owns one connection.  The registry is the local cancellation
    fence used by the daemon's bounded shutdown path: cancelling the asyncio
    task returned by ``to_thread`` cannot stop a thread blocked in socket I/O,
    while shutting down the registered socket does.  A permanently closed
    instance rejects calls started by stale teardown callbacks; a new managed
    DSH process gets a new instance.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = DSH_REQUEST_TIMEOUT_SECONDS,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._redact = redact
        self._lock = threading.Lock()
        self._active: set[http.client.HTTPConnection] = set()
        self._connecting: set[socket.socket] = set()
        self._resolvers: set[_ResolverRequest] = set()
        self._resolved: dict[tuple[str, int], tuple[tuple[Any, ...], ...]] = {}
        self._cancel_generation = 0
        self._closed = False

    async def call(self, method: str, payload: dict[str, object]) -> object:
        return await asyncio.to_thread(self._call_sync, method, payload)

    def _call_sync(self, method: str, payload: dict[str, object]) -> object:
        rpc_id = str(uuid4())
        body = json.dumps(
            {
                "type": "client-request",
                "rpcId": rpc_id,
                "method": method,
                "payload": payload,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        endpoint = urlparse(self.endpoint)
        connection_type: type[http.client.HTTPConnection]
        if endpoint.scheme == "http":
            connection_type = http.client.HTTPConnection
        elif endpoint.scheme == "https":
            connection_type = http.client.HTTPSConnection
        else:
            raise DshApiError(f"unsupported DSH endpoint scheme: {endpoint.scheme!r}")
        if endpoint.hostname is None:
            raise DshApiError("DSH endpoint has no hostname")
        port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        connection = connection_type(
            endpoint.hostname,
            endpoint.port,
            timeout=self.timeout_seconds,
        )
        owned_sockets: list[socket.socket] = []
        with self._lock:
            if self._closed:
                raise DshApiError("DSH HTTP transport is closed")
            cancel_generation = self._cancel_generation
            self._active.add(connection)
        path = f"{endpoint.path.rstrip('/')}/api/{method}"
        try:
            addresses = self._resolve_addresses(
                endpoint.hostname, port, cancel_generation
            )

            def create_connection(
                _address: object,
                timeout: object = self.timeout_seconds,
                source_address: tuple[str, int] | None = None,
            ) -> socket.socket:
                effective_timeout = (
                    self.timeout_seconds
                    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT
                    else float(timeout)
                )
                return self._connect_addresses(
                    addresses,
                    timeout=effective_timeout,
                    source_address=source_address,
                    cancel_generation=cancel_generation,
                    owned_sockets=owned_sockets,
                )

            connection._create_connection = create_connection  # type: ignore[attr-defined]
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response_body = response.read()
            if not 200 <= response.status < 300:
                detail = response_body.decode("utf-8", errors="replace")[:1000]
                if self._redact is not None:
                    detail = self._redact(detail)
                raise DshApiError(
                    f"DSH {method} returned HTTP {response.status}: {detail}"
                )
            envelope = json.loads(response_body.decode("utf-8"))
        except DshApiError:
            raise
        except (
            OSError,
            TimeoutError,
            json.JSONDecodeError,
            http.client.HTTPException,
        ) as error:
            if isinstance(error, OSError):
                with self._lock:
                    self._resolved.pop((endpoint.hostname, port), None)
            raise DshApiError(
                self._transport_error_message(method, error, cancel_generation)
            ) from error
        finally:
            with self._lock:
                self._active.discard(connection)
                for owned_socket in owned_sockets:
                    self._connecting.discard(owned_socket)
            connection.close()
            for owned_socket in owned_sockets:
                owned_socket.close()

        if not isinstance(envelope, dict) or envelope.get("rpcId") != rpc_id:
            raise DshApiError(f"DSH {method} returned an invalid RPC envelope")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise DshApiError(f"DSH {method} response has no result")
        if result.get("ok") is not True:
            detail = result.get("error", result)
            rendered = str(detail)
            if self._redact is not None:
                rendered = self._redact(rendered)
            raise DshApiError(f"DSH {method} failed: {rendered}")
        return result.get("value")

    def _transport_error_message(
        self, method: str, error: BaseException, cancel_generation: int
    ) -> str:
        """Describe one failed call while keeping our own shutdown fence visible.

        ``close``/``cancel_active`` shut the socket down under a thread parked
        in ``http.client`` I/O.  Depending on where that thread sits, the fence
        surfaces either as a socket ``OSError`` or as an ``http.client`` state
        error (``ResponseNotReady``/``CannotSendRequest``/``IncompleteRead`` and
        the rest of ``HTTPException``).  Both are this fence winning a race
        rather than a remote failure, so a fenced call says so; every other
        failure keeps its own message, and the original exception is still
        chained as ``__cause__`` either way.
        """

        with self._lock:
            closed = self._closed
            cancelled = cancel_generation != self._cancel_generation
        if closed or cancelled:
            reason = "closed" if closed else "cancelled"
            return (
                f"DSH {method} aborted: HTTP transport was {reason} "
                f"during the call ({error})"
            )
        return f"DSH {method} failed: {error}"

    def cancel_active(self) -> None:
        """Abort currently blocked local I/O while allowing later calls."""

        with self._lock:
            self._cancel_generation += 1
            active = tuple(self._active)
            connecting = tuple(self._connecting)
            resolvers = tuple(self._resolvers)
        for resolver in resolvers:
            resolver.cancel()
        for connection in active:
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        for connecting_socket in connecting:
            try:
                connecting_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connecting_socket.close()

    def close(self) -> None:
        """Fence future calls and abort every request owned by this process."""

        with self._lock:
            self._closed = True
        self.cancel_active()

    def stopped(self) -> bool:
        with self._lock:
            return not self._active and not self._connecting and not self._resolvers

    def _resolve_addresses(
        self, host: str, port: int, cancel_generation: int
    ) -> tuple[tuple[Any, ...], ...]:
        numeric_host = host.removeprefix("[").removesuffix("]")
        try:
            address = ipaddress.ip_address(numeric_host)
        except ValueError:
            address = None
        if address is not None:
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            sockaddr: tuple[Any, ...] = (
                (str(address), port, 0, 0)
                if address.version == 6
                else (str(address), port)
            )
            return ((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr),)

        key = (host, port)
        with self._lock:
            cached = self._resolved.get(key)
            if cached is not None:
                return cached
            if self._closed or cancel_generation != self._cancel_generation:
                raise DshApiError("DSH HTTP transport was cancelled")
        resolver = _start_resolver(host, port)
        with self._lock:
            if self._closed or cancel_generation != self._cancel_generation:
                resolver.cancel()
                raise DshApiError("DSH HTTP transport was cancelled")
            self._resolvers.add(resolver)
        try:
            addresses = resolver.result(self.timeout_seconds)
        finally:
            with self._lock:
                self._resolvers.discard(resolver)
            resolver.close()
        with self._lock:
            if self._closed or cancel_generation != self._cancel_generation:
                raise DshApiError("DSH HTTP transport was cancelled")
            self._resolved[key] = addresses
        return addresses

    def _connect_addresses(
        self,
        addresses: tuple[tuple[Any, ...], ...],
        *,
        timeout: float,
        source_address: tuple[str, int] | None,
        cancel_generation: int,
        owned_sockets: list[socket.socket],
    ) -> socket.socket:
        last_error: OSError | None = None
        for family, socket_type, protocol, _canonname, sockaddr in addresses:
            candidate = socket.socket(family, socket_type, protocol)
            candidate.settimeout(timeout)
            if source_address is not None:
                candidate.bind(source_address)
            with self._lock:
                if self._closed or cancel_generation != self._cancel_generation:
                    candidate.close()
                    raise DshApiError("DSH HTTP transport was cancelled")
                self._connecting.add(candidate)
                owned_sockets.append(candidate)
            try:
                candidate.connect(sockaddr)
                return candidate
            except OSError as error:
                last_error = error
                with self._lock:
                    self._connecting.discard(candidate)
                candidate.close()
        if last_error is not None:
            raise last_error
        raise DshApiError("DSH hostname resolved to no usable addresses")


@dataclass(frozen=True, slots=True)
class DshWorkerPreset:
    """One DSH agent preset carrying exactly one worker's MCP identity."""

    preset_id: str
    server_name: str
    path: Path


class DshWorkerPresetManager:
    """Install a worker-scoped MCP client into rc.6's user preset roster.

    DSH's MCP bridge is a Cordis plugin, and rc.6 mounts agent presets once per
    process while scoping their tools to sessions that select that preset.  A
    global Web-profile MCP row would therefore leak one worker's identity into
    every DSH session.  This manager instead copies the requested base preset,
    appends a uniquely-namespaced MCP row, and selects the copy only for the
    managed session.
    """

    def __init__(
        self,
        api: DshApi,
        channel: WorkerChannel,
        *,
        dsh_home: Path,
        base_preset: str,
    ) -> None:
        self.api = api
        self.channel = channel
        self.dsh_home = dsh_home
        self.base_preset = base_preset

    async def install(self) -> DshWorkerPreset:
        digest = hashlib.sha256(
            f"{self.channel.actor}\0{self.channel.session_ref}".encode()
        ).hexdigest()[:16]
        preset_id = f"hyprial-{digest}"
        server_name = f"hyprial-{digest}"
        await self.api.call(
            "agentPreset.copy",
            {
                "from": self.base_preset,
                "agentPreset": preset_id,
                "name": f"HYPRIAL worker {digest}",
            },
        )
        path = self.dsh_home / ".agent-presets" / preset_id / "agent.cordis.yml"
        try:
            self._append_mcp_row(path, server_name)
        except Exception:
            # The copy has not been selected by a session yet, so cleanup is
            # safe and prevents a broken preset occupying the deterministic id.
            try:
                await self.api.call("agentPreset.remove", {"agentPreset": preset_id})
            except Exception:
                pass
            raise
        return DshWorkerPreset(preset_id, server_name, path)

    def _append_mcp_row(self, path: Path, server_name: str) -> None:
        roster = (self.dsh_home / ".agent-presets").resolve()
        resolved_parent = path.parent.resolve()
        if roster not in resolved_parent.parents or path.is_symlink():
            raise DshApiError("DSH worker preset path escaped the user preset root")
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as error:
            raise DshApiError(
                f"DSH did not create the copied agent preset at {path}"
            ) from error

        server = self.channel.mcp_server
        command = server.get("command")
        args = server.get("args")
        env = server.get("env")
        if not isinstance(command, str) or not isinstance(args, list) or not isinstance(
            env, dict
        ):
            raise DshApiError("worker channel produced an invalid MCP stdio config")
        row = {
            "id": f"hyprial-worker-{server_name.removeprefix('hyprial-')}",
            "name": _MCP_PLUGIN_PACKAGE,
            "config": {
                "serverName": server_name,
                "transport": "stdio",
                "command": command,
                "args": args,
                "env": env,
                "failOnStartupError": True,
            },
        }
        block = yaml.safe_dump(
            [row], allow_unicode=True, default_flow_style=False, sort_keys=False
        )
        marker = "# Managed by hyprial: per-worker MCP identity; do not reuse.\n"
        content = original.rstrip() + "\n\n" + marker + block
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class _DshSession:
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class _DshTurnOutcome:
    result: str
    is_error: bool = False


def _option_value(args: tuple[str, ...], option: str) -> str | None:
    for index, value in enumerate(args):
        if value == option and index + 1 < len(args):
            return args[index + 1]
        prefix = f"{option}="
        if value.startswith(prefix):
            return value[len(prefix) :]
    return None


def _positive_float(value: str | None, *, default: float, label: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a number") from error
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _events(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise DshApiError("DSH session.history returned an invalid value")
    result: list[dict[str, Any]] = []
    for item in value["events"]:
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        if isinstance(event, dict):
            result.append(event)
    return result


def _assistant_text(event: dict[str, Any]) -> str | None:
    if event.get("type") != "assistant/message":
        return None
    data = event.get("data")
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    chunks = [
        item.get("text")
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    text = "\n".join(chunks).strip()
    return text or None


class DshApiClient:
    """One persistent DSH conversation exposed as a streaming turn client."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        api: DshApi | None = None,
        session: _DshSession | None = None,
        poll_interval_seconds: float | None = None,
        turn_timeout_seconds: float | None = None,
        worker_channel: WorkerChannel | None = None,
        dsh_home: Path | None = None,
    ) -> None:
        self.spec = spec
        # The transport always belongs to the managed process generation that
        # spawned this DSH; only that generation knows the OS-assigned port.
        self.api = api or DshHttpApi("http://127.0.0.1:0")
        self._session = session or _DshSession(_option_value(spec.args, "--session-id"))
        self.agent_preset = _option_value(spec.args, "--agent-preset") or "standard"
        self.poll_interval_seconds = poll_interval_seconds or _positive_float(
            _option_value(spec.args, "--poll-interval"),
            default=0.25,
            label="--poll-interval",
        )
        self.turn_timeout_seconds = turn_timeout_seconds or _positive_float(
            _option_value(spec.args, "--turn-timeout"),
            default=resolve_turn_timeout_seconds(
                spec.turn_timeout_seconds,
                default=MANAGED_TURN_TIMEOUT_SECONDS,
            ),
            label="--turn-timeout",
        )
        self.running = False
        self.exit_error: str | None = None
        self._baseline_seq = 0
        self._turn_pending = False
        self.worker_channel = worker_channel
        self.worker_preset: DshWorkerPreset | None = None
        self.dsh_home = Path(dsh_home) if dsh_home is not None else None

    @property
    def session_id(self) -> str | None:
        return self._session.session_id

    async def __aenter__(self) -> Self:
        validate_model_selection(
            self.spec.harness, self.spec.model_provider, self.spec.model
        )
        # Readiness has two parts: the banner proves the OS port is bound and
        # the Loader settled, this call proves /api is registered behind the
        # trust fence.  A banner alone is human-facing text, not a contract.
        await self.api.call("host.describe", {})
        if self.worker_channel is not None:
            if self.dsh_home is None:
                raise DshApiError(
                    "per-worker DSH MCP injection requires the worker's DSH_HOME"
                )
            if self._session.session_id is not None:
                raise DshApiError(
                    "a fresh worker MCP identity cannot resume an existing DSH "
                    "session; omit --session-id"
                )
            self.worker_preset = await DshWorkerPresetManager(
                self.api,
                self.worker_channel,
                dsh_home=self.dsh_home,
                base_preset=self.agent_preset,
            ).install()
        if self._session.session_id is None:
            payload: dict[str, object] = {
                "agentPreset": (
                    self.worker_preset.preset_id
                    if self.worker_preset is not None
                    else self.agent_preset
                )
            }
            if self.spec.cwd is not None:
                payload["cwd"] = self.spec.cwd
            value = await self.api.call("session.create", payload)
            if not isinstance(value, dict) or not isinstance(
                value.get("sessionId"), str
            ):
                raise DshApiError("DSH session.create returned no sessionId")
            self._session.session_id = value["sessionId"]
        else:
            # Fail at startup, rather than after the first delivery, when a
            # requested resume target is missing or belongs to another host.
            await self.api.call(
                "session.history",
                {"sessionId": self._session.session_id, "maxMessages": 1},
            )
        if self.spec.model_provider is not None or self.spec.model is not None:
            session_id = self._require_session()
            available = await self.api.call("session.models", {"sessionId": session_id})
            current = available.get("current") if isinstance(available, dict) else None
            if not isinstance(current, dict):
                raise DshApiError("DSH session.models returned no current selection")
            provider = self.spec.model_provider or current.get("provider")
            model = self.spec.model or current.get("model")
            if not isinstance(provider, str) or not provider:
                raise DshApiError("DSH session.models returned no current provider")
            provider = dsh_provider_id(provider)
            if not isinstance(model, str) or not model:
                raise DshApiError("DSH session.models returned no current model")
            value = await self.api.call(
                "session.selectModel",
                {"sessionId": session_id, "provider": provider, "model": model},
            )
            selected = value.get("selected") if isinstance(value, dict) else None
            if not isinstance(selected, dict) or (
                selected.get("provider"), selected.get("model")
            ) != (provider, model):
                raise DshApiError(
                    "DSH session.selectModel did not preserve the requested selection"
                )
        self.running = True
        self.exit_error = None
        return self

    async def __aexit__(self, *args: object) -> bool:
        self.running = False
        if self._turn_pending:
            try:
                await self.interrupt()
            except DshApiError:
                pass
        return False

    async def query(self, prompt: str) -> None:
        session_id = self._require_session()
        history = await self.api.call(
            "session.history", {"sessionId": session_id, "maxMessages": 1}
        )
        self._baseline_seq = max(
            (event.get("seq", 0) for event in _events(history)), default=0
        )
        value = await self.api.call(
            "session.prompt",
            {
                "sessionId": session_id,
                "mode": "queue",
                "content": [{"type": "text", "text": prompt}],
            },
        )
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshApiError("DSH session.prompt was not accepted")
        self._turn_pending = True

    async def receive_response(self) -> AsyncIterator[_DshTurnOutcome]:
        session_id = self._require_session()
        last_text = ""
        while True:
            history = await self.api.call(
                "session.history", {"sessionId": session_id, "maxMessages": 200}
            )
            for event in _events(history):
                seq = event.get("seq")
                if not isinstance(seq, int) or seq <= self._baseline_seq:
                    continue
                text = _assistant_text(event)
                if text is not None:
                    last_text = text
                if event.get("type") == "turn/end":
                    self._turn_pending = False
                    data = event.get("data")
                    reason = data.get("reason") if isinstance(data, dict) else None
                    kind = reason.get("kind") if isinstance(reason, dict) else None
                    if kind == "completed":
                        yield _DshTurnOutcome(last_text)
                    else:
                        yield _DshTurnOutcome(
                            f"DSH turn ended with reason {kind or 'unknown'}", True
                        )
                    return
            # No wall-clock deadline (#277): the turn ends when DSH reports
            # turn/end, or on an explicit interrupt.  Stall reporting waits
            # for a truthful DSH activity source (the current fixed
            # ``_baseline_seq`` re-reads old events every poll and would
            # fake a heartbeat).
            await asyncio.sleep(self.poll_interval_seconds)

    async def interrupt(self) -> None:
        if not self._turn_pending:
            return
        await self.api.call("session.cancel", {"sessionId": self._require_session()})

    def force_stop(self) -> None:
        cancel_active = getattr(self.api, "cancel_active", None)
        if callable(cancel_active):
            cancel_active()

    def force_stopped(self) -> bool:
        stopped = getattr(self.api, "stopped", None)
        return bool(stopped()) if callable(stopped) else True

    def _require_session(self) -> str:
        if self._session.session_id is None:
            raise DshApiError("DSH session is not initialized")
        return self._session.session_id


@dataclass(frozen=True, slots=True)
class _DshGeneration:
    """One spawned ``dsh`` process and the transport bound to its port."""

    process: subprocess.Popen[bytes]
    group: OwnedProcessGroup
    api: DshHttpApi
    endpoint: str
    argv: tuple[str, ...]
    #: This generation's own output buffers: a late line from a replaced child
    #: must never land in the next generation's tail or ``exit_error``.
    stdout_tail: bytearray
    stderr_tail: bytearray


class DshHarnessProcess(StreamingTurnProcess):
    """Daemon-managed DSH session driven through the shared turn pump.

    Each (re)connect spawns one private ``dsh --profile web --host 127.0.0.1
    --port 0`` in its own session (process group), reads the OS-assigned port
    out of the child's banner, and hands that endpoint to a fresh
    ``DshHttpApi``.  Ownership mirrors the codex app-server: an
    ``OwnedProcessGroup`` fenced by PID and birth identity, so stop/``hyprial
    down`` signals only a generation this daemon actually started.
    """

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: TurnClientFactory | None = None,
        worker_channel: WorkerChannel | None = None,
        env: Mapping[str, str] | None = None,
        state_dir: Path | None = None,
        dsh_home: Path | None = None,
    ) -> None:
        if spec.harness != "dsh" or not spec.headless:
            raise ValueError("DSH API process requires a headless dsh spec")
        self.spec = spec
        self._session = _DshSession(_option_value(spec.args, "--session-id"))
        self.worker_channel = worker_channel
        if dsh_home is not None:
            self.dsh_home: Path | None = Path(dsh_home)
        elif worker_channel is not None:
            self.dsh_home = dsh_worker_home(worker_channel.state_dir, spec.name)
        elif state_dir is not None:
            self.dsh_home = dsh_worker_home(Path(state_dir), spec.name)
        else:
            # Only reachable for injected client factories (tests) and direct
            # embedding; a real spawn fails loudly below instead of guessing a
            # shared home.
            self.dsh_home = None
        # Deterministic configuration errors fail once, before the pump (and
        # therefore before any child) exists; they must not be rediscovered on
        # every retry.  An injected client factory owns its own arguments.
        if client_factory is None:
            if self.dsh_home is None:
                raise ValueError(
                    "managed DSH requires a worker channel or a state directory"
                )
            _validate_client_arguments(spec)
            # The same deterministic checks ``DshApiClient.__aenter__`` makes;
            # re-running them there is fine, but they must not be discovered
            # only after the first child already exists (that would respawn
            # one process per retry in the reconnect window).
            validate_model_selection(
                spec.harness, spec.model_provider, spec.model
            )
            if worker_channel is not None and self._session.session_id is not None:
                raise DshApiError(
                    "a fresh worker MCP identity cannot resume an existing DSH "
                    "session; omit --session-id"
                )
            if shutil.which("dsh") is None:
                raise DshApiError("dsh not on PATH")
        self._base_env: dict[str, str] | None = (
            dict(env) if env is not None else None
        )
        self._dsh_lock = threading.Lock()
        self._io_log_lock = threading.Lock()
        self._generation: _DshGeneration | None = None
        self._exit_error: str | None = None
        self._secret_values: tuple[str, ...] = ()
        self._stdout_tail = bytearray()
        self._stderr_tail = bytearray()
        self._io_log_path = (
            self.dsh_home.parent / "io.log" if self.dsh_home is not None else None
        )
        factory = client_factory or self._connect_generation
        super().__init__(
            harness="dsh",
            label="DSH API",
            client_factory=factory,
            thread_name=f"hyprial-dsh-{spec.name}",
            reconnect_delay_max_seconds=1.0,
            force_stop=self._force_stop_client,
            force_stopped=self._force_stopped_client,
        )

    @property
    def session_ref(self) -> str | None:
        return self._session.session_id

    @property
    def endpoint(self) -> str | None:
        """The current generation's real endpoint, or ``None`` before spawn."""

        generation = self._generation
        return generation.endpoint if generation is not None else None

    @property
    def pid(self) -> int | None:
        """The live DSH child PID of the current generation, if any."""

        generation = self._generation
        if generation is None:
            return None
        process = generation.process
        return process.pid if process.poll() is None else None

    @property
    def argv(self) -> tuple[str, ...] | None:
        generation = self._generation
        return generation.argv if generation is not None else None

    @property
    def exit_error(self) -> str | None:
        """The current or most recent child's exit, with its status code."""

        return self._exit_error

    def io_log(self) -> bytes:
        """The bounded tail of the child's drained stdout/stderr."""

        if self._io_log_path is None:
            return b""
        try:
            return self._io_log_path.read_bytes()
        except OSError:
            return b""

    def stderr_tail(self) -> bytes:
        return bytes(self._stderr_tail)

    def _connect_generation(self) -> DshApiClient:
        self._terminate_generation()
        if self.dsh_home is None:
            raise DshApiError(
                "managed DSH requires a worker channel or a state directory"
            )
        binary = shutil.which("dsh")
        if binary is None:
            raise DshApiError("dsh not on PATH")
        prepare_worker_home(self.dsh_home)
        # A partial env is an overlay on the daemon's own environment, never a
        # replacement: DSH needs PATH (and whatever else the daemon has).
        environment = {**os.environ, **(self._base_env or {})}
        environment.pop("DSH_HOME", None)
        environment["DSH_HOME"] = str(self.dsh_home)
        self._secret_values = _environment_secrets(environment)
        argv = (
            binary,
            "--profile",
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        )
        process = subprocess.Popen(
            argv,
            cwd=str(self.dsh_home),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        group = OwnedProcessGroup(label="DSH")
        # Every failure after spawn -- identity registration, the banner and its
        # drain setup, the client's own argument parsing -- must reap this
        # fresh process group.  Otherwise the pump's next retry leaves the
        # previous child running and every generation leaks one DSH.
        try:
            group.register(process.pid)
            endpoint, stdout_tail, stderr_tail = self._read_banner(process, group)
            api = DshHttpApi(
                endpoint,
                timeout_seconds=DSH_REQUEST_TIMEOUT_SECONDS,
                redact=self._redact_text,
            )
            generation = _DshGeneration(
                process, group, api, endpoint, argv, stdout_tail, stderr_tail
            )
            client = DshApiClient(
                self.spec,
                api=api,
                session=self._session,
                worker_channel=self.worker_channel,
                dsh_home=self.dsh_home,
            )
            with self._dsh_lock:
                self._exit_error = None
                self._generation = generation
            threading.Thread(
                target=self._watch_generation,
                args=(generation, client),
                name=f"hyprial-dsh-exit-{self.spec.name}",
                daemon=True,
            ).start()
            return client
        except BaseException:
            group.force_close()
            self._reap(process)
            raise

    def _watch_generation(
        self, generation: _DshGeneration, client: DshApiClient
    ) -> None:
        """Publish the child's real exit status to the client and this process."""

        returncode = generation.process.wait()
        if self._generation is not generation:
            return
        detail = bytes(generation.stderr_tail).decode(
            "utf-8", errors="replace"
        ).strip()
        message = f"DSH process exited with status {returncode}"
        if detail:
            message = f"{message}: {detail[-500:]}"
        self._exit_error = message
        client.exit_error = message
        client.running = False

    def _read_banner(
        self, process: subprocess.Popen[bytes], group: OwnedProcessGroup
    ) -> tuple[str, bytearray, bytearray]:
        """Read the first ``dsh web:`` banner, then keep draining both pipes.

        Returns the endpoint plus this generation's own output buffers.
        """

        stdout = process.stdout
        stderr = process.stderr
        assert stdout is not None and stderr is not None
        stdout_tail = bytearray()
        stderr_tail = bytearray()
        with self._dsh_lock:
            self._stdout_tail = stdout_tail
            self._stderr_tail = stderr_tail
        self._io_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._io_log_path.write_bytes(b"")
            os.chmod(self._io_log_path, 0o600)
        except OSError:
            pass
        banner_event = threading.Event()
        endpoint: list[str] = []

        def drain_stdout() -> None:
            try:
                for raw in _iter_pipe_lines(stdout):
                    redacted = self._redact_output(raw)
                    with self._dsh_lock:
                        self._extend_tail(stdout_tail, redacted)
                    self._append_io_log(b"STDOUT " + redacted)
                    if not endpoint:
                        line = redacted.decode("utf-8", errors="replace").strip()
                        match = _DSH_WEB_BANNER.match(line)
                        if match:
                            endpoint.append(f"http://127.0.0.1:{match.group(1)}")
            except (OSError, ValueError):
                pass
            finally:
                banner_event.set()
                _close_stream(stdout)

        def drain_stderr() -> None:
            try:
                for raw in _iter_pipe_lines(stderr):
                    redacted = self._redact_output(raw)
                    with self._dsh_lock:
                        self._extend_tail(stderr_tail, redacted)
                    self._append_io_log(b"STDERR " + redacted)
            except (OSError, ValueError):
                pass
            finally:
                _close_stream(stderr)

        threading.Thread(
            target=drain_stdout, name=f"hyprial-dsh-stdout-{self.spec.name}", daemon=True
        ).start()
        threading.Thread(
            target=drain_stderr, name=f"hyprial-dsh-stderr-{self.spec.name}", daemon=True
        ).start()

        deadline = time.monotonic() + DSH_REQUEST_TIMEOUT_SECONDS
        while not endpoint:
            if self._stopping.is_set():
                break
            # stdout reached EOF without a banner: no later line can carry one,
            # so do not spin on a set event until the deadline.
            if banner_event.is_set():
                break
            if process.poll() is not None:
                banner_event.wait(DSH_BANNER_POLL_SECONDS)
                break
            if time.monotonic() >= deadline:
                break
            banner_event.wait(
                min(DSH_BANNER_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
            )
        if endpoint:
            return endpoint[0], stdout_tail, stderr_tail

        group.force_close()
        self._reap(process)
        detail = bytes(stderr_tail).decode("utf-8", errors="replace").strip()
        message = (
            "DSH web banner was not printed within "
            f"{DSH_REQUEST_TIMEOUT_SECONDS:g}s (child exit status "
            f"{process.returncode})"
        )
        if detail:
            message = f"{message}: {detail[-2000:]}"
        raise DshApiError(message)

    @staticmethod
    def _extend_tail(buffer: bytearray, chunk: bytes) -> None:
        buffer.extend(chunk)
        if len(buffer) > _DSH_IO_TAIL_BYTES:
            del buffer[: len(buffer) - _DSH_IO_TAIL_BYTES]

    def _redact_text(self, text: str) -> str:
        for secret in self._secret_values:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _redact_output(self, chunk: bytes) -> bytes:
        """Scrub the child's env secret values before anything keeps them.

        stderr tails become ``exit_error`` -> status ``error`` and the worker
        turn event, and the drain log is on disk; a child that echoes its own
        environment must not leak it into any of them.
        """

        if not self._secret_values:
            return chunk
        return self._redact_text(chunk.decode("utf-8", errors="replace")).encode(
            "utf-8"
        )

    def _append_io_log(self, chunk: bytes) -> None:
        path = self._io_log_path
        if path is None:
            return
        with self._io_log_lock:
            try:
                with open(path, "ab") as handle:
                    handle.write(chunk)
                    handle.flush()
                if path.stat().st_size > _DSH_IO_TAIL_BYTES:
                    path.write_bytes(path.read_bytes()[-_DSH_IO_TAIL_BYTES:])
            except OSError:
                pass

    def _terminate_generation(self) -> None:
        generation = self._generation
        if generation is None:
            return
        if self._stopping.is_set():
            generation.api.close()
        else:
            generation.api.cancel_active()
        generation.group.force_close()
        self._reap(generation.process)

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=DSH_STARTUP_REAP_SECONDS)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                _close_stream_async(stream)

    def _force_stop_client(self) -> None:
        self._terminate_generation()
        with self._lock:
            # During __aenter__ the pump has published only _connecting_client;
            # reaching it is what unblocks a pre-socket / pre-connection call.
            client = self._client or self._connecting_client
        force_stop = getattr(client, "force_stop", None)
        if callable(force_stop):
            force_stop()

    def _force_stopped_client(self) -> bool:
        generation = self._generation
        if generation is not None:
            if not generation.api.stopped():
                return False
            return generation.group.stopped()
        with self._lock:
            client = self._client or self._connecting_client
        force_stopped = getattr(client, "force_stopped", None)
        return bool(force_stopped()) if callable(force_stopped) else True
