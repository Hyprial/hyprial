"""DSH HTTP API client, resolver, and worker preset installation."""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import json
import multiprocessing
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import yaml

from hyprial.kernel import resolve_hostname

from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel

if TYPE_CHECKING:
    pass

# The single DSH HTTP request timeout.  It is also the banner read budget:
# the banner is how the OS-assigned port comes back and is one request's worth
# of waiting, not a second policy.
DSH_REQUEST_TIMEOUT_SECONDS = 15.0

_MCP_PLUGIN_PACKAGE = "@deepseek-ai/dsh-mcp-client"

#: An unterminated line cannot buffer without bound; ``readline(size)`` caps it.
_DSH_MAX_LINE_BYTES = 64 * 1024

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
