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
import socket
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.parse import urlparse
from uuid import uuid4

import yaml

from hyprial._dsh_resolver import resolve_hostname
from hyprial.daemon.desired_state import HarnessLaunchSpec

from .streaming import (
    StreamingTurnProcess,
    TurnClientFactory,
    resolve_turn_timeout_seconds,
)
from .worker_channel import WorkerChannel
from .model_provider import dsh_provider_id, validate_model_selection

DEFAULT_DSH_ENDPOINT = "http://127.0.0.1:3080"
_MCP_PLUGIN_PACKAGE = "@deepseek-ai/dsh-mcp-client"

# Retired wall-clock cap (#277: no timeout kills a turn).  The value is
# still resolved so the pre-existing ``--turn-timeout`` argument and the
# persisted ``turnTimeoutSeconds`` field keep parsing (old launch specs
# and callers must not crash), but nothing enforces it.
MANAGED_TURN_TIMEOUT_SECONDS = 3600.0


class DshApiError(RuntimeError):
    """DSH transport or RPC envelope failure."""


class DshApi(Protocol):
    async def call(self, method: str, payload: dict[str, object]) -> object: ...


def resolve_dsh_endpoint(spec: HarnessLaunchSpec) -> str:
    """One endpoint precedence rule for the runtime and diagnostic projection."""

    return _option_value(spec.args, "--endpoint") or spec.endpoint or DEFAULT_DSH_ENDPOINT


def dsh_status_endpoint(spec: HarnessLaunchSpec) -> str | None:
    """Project only the URL components actually used by DshHttpApi.

    HTTP userinfo, query and fragment are not used by this transport and must
    not leak into daemon status. Invalid launch configuration stays diagnosable
    without crashing the lifecycle actor's publication.
    """

    try:
        parsed = urlparse(resolve_dsh_endpoint(spec))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path.rstrip('/')}"
    except ValueError:
        return None


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

    def __init__(self, endpoint: str, *, timeout_seconds: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
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
                raise DshApiError(
                    f"DSH {method} returned HTTP {response.status}: {detail}"
                )
            envelope = json.loads(response_body.decode("utf-8"))
        except DshApiError:
            raise
        except (OSError, TimeoutError, json.JSONDecodeError) as error:
            if isinstance(error, OSError):
                with self._lock:
                    self._resolved.pop((endpoint.hostname, port), None)
            raise DshApiError(f"DSH {method} failed: {error}") from error
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
            raise DshApiError(f"DSH {method} failed: {detail}")
        return result.get("value")

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


def _dsh_home(args: tuple[str, ...]) -> Path:
    configured = _option_value(args, "--dsh-home") or os.environ.get("DSH_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".dsh"


def _loopback_endpoint(endpoint: str) -> bool:
    host = urlparse(endpoint).hostname
    return host in {"127.0.0.1", "localhost", "::1"}


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
    ) -> None:
        self.spec = spec
        self.api = api or DshHttpApi(resolve_dsh_endpoint(spec))
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
        self.dsh_home = _dsh_home(spec.args)

    @property
    def session_id(self) -> str | None:
        return self._session.session_id

    async def __aenter__(self) -> Self:
        validate_model_selection(
            self.spec.harness, self.spec.model_provider, self.spec.model
        )
        await self.api.call("host.describe", {})
        if self.worker_channel is not None:
            if not _loopback_endpoint(resolve_dsh_endpoint(self.spec)):
                raise DshApiError(
                    "per-worker DSH MCP injection requires a loopback DSH endpoint "
                    "whose DSH_HOME is on this machine"
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


class DshHarnessProcess(StreamingTurnProcess):
    """Daemon-managed DSH session driven through the shared turn pump."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: TurnClientFactory | None = None,
        worker_channel: WorkerChannel | None = None,
    ) -> None:
        if spec.harness != "dsh" or not spec.headless:
            raise ValueError("DSH API process requires a headless dsh spec")
        self._session = _DshSession(_option_value(spec.args, "--session-id"))
        self.worker_channel = worker_channel
        self._owned_api = (
            DshHttpApi(resolve_dsh_endpoint(spec))
            if client_factory is None
            else None
        )
        factory = client_factory or (
            lambda: DshApiClient(
                spec,
                api=self._owned_api,
                session=self._session,
                worker_channel=worker_channel,
            )
        )
        super().__init__(
            harness="dsh",
            label="DSH API",
            client_factory=factory,
            thread_name=f"hyprial-dsh-{spec.name}",
            force_stop=self._force_stop_client,
            force_stopped=self._force_stopped_client,
        )

    @property
    def session_ref(self) -> str | None:
        return self._session.session_id

    def _force_stop_client(self) -> None:
        if self._owned_api is not None:
            if self._stopping.is_set():
                self._owned_api.close()
            else:
                self._owned_api.cancel_active()
            return
        with self._lock:
            client = self._client
        force_stop = getattr(client, "force_stop", None)
        if callable(force_stop):
            force_stop()

    def _force_stopped_client(self) -> bool:
        if self._owned_api is not None:
            return self._owned_api.stopped()
        with self._lock:
            client = self._client
        force_stopped = getattr(client, "force_stopped", None)
        return bool(force_stopped()) if callable(force_stopped) else True
