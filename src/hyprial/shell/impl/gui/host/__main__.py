# Decision record (GUI session host, PR 2b):
# - The host is a child process the launcher owns, the Node host's position:
#   `python -m hyprial.shell.impl.gui.host serve --port N --root DIR`, with the
#   HYPRIAL_GUI_* environment. Secrets stay in the environment, never in argv.
# - The child creates, binds and listens on its own socket, and prints
#   HYPRIAL_GUI_READY only after Uvicorn finished startup (lifespan included),
#   so a client that connects the instant it reads the line is accepted. It
#   hands the already-listening socket to Uvicorn.
# - Startup refusals follow the request_http convention: one JSON object
#   {ok:false, code, message} on stderr, exit 2 for configuration, argument or
#   asset refusals and exit 4 for a bind failure. stdout carries protocol
#   lines only; no message ever contains the grant or the bearer.
# - Only a loopback bind is accepted. Port 0 is supported here; the CLI keeps
#   refusing it until a later PR lifts that.
# - Signals are handled here, not by Uvicorn: Uvicorn re-raises a captured
#   signal after a graceful stop, which would kill the process with the signal
#   status instead of a clean exit 0 and HYPRIAL_GUI_STOPPED.
# - HTTP stack limits are explicit, for a loopback host with one local user.
#   httptools (kept: it routes absolute-form targets by path, which the
#   contract requires) has no header limit to configure, so the protocol
#   subclass counts the request-target and header bytes and fields of each
#   message and answers 431 beyond 16 KiB (the Node default) or 100 fields.
#   A refused request is never dispatched to the app; one that follows an
#   in-flight response on the same connection is answered 431 only after that
#   response completed, and any not yet dispatched pipelined request is dropped.
#   Pipelining is not a feature: the only promise is that a dispatched response
#   is not lost to bytes of a later request.
# - Connections: uvicorn limit_concurrency is 256 open connections; connections
#   beyond that are still accepted and each request on them is answered 503 by
#   the HTTP stack. An idle keep-alive connection lives 5 s.
# - Incomplete requests are bounded twice while no response is in flight: a
#   connection that delivered nothing for 10 s (READ_IDLE_SECONDS, refreshed by
#   every received byte) is closed, and a request, headers and body together,
#   that is not complete 15 s after its first byte (REQUEST_DEADLINE_SECONDS,
#   not refreshed) is closed, so a client dripping one byte per interval cannot
#   hold a connection. Once a request is complete the app's own 15 s upstream
#   timeout governs. If either deadline fires while an earlier response is
#   still in flight (bytes of a pipelined next request), the close waits for
#   that response and the timer is re-armed.
#   X-Forwarded-* is never trusted (proxy_headers off), no Server header is sent.
#   The subclass overrides uvicorn hooks by name, so _require_protocol_shape
#   fails the start loudly if a uvicorn upgrade renames or removes one.
# - WebSocket support (PR 3) is on only with --live-upstream (ws websockets-sansio),
#   which is what the HMR relay needs; with --root nothing upgrades (ws none).
#   Exactly one of --root and --live-upstream is required. A live upstream that
#   is not a loopback literal origin is a refusal (exit 2). The ready line and
#   /__gui/healthz say mode development then, production otherwise. A completed
#   upgrade request has already stopped both read deadlines (the message is
#   complete), so a healthy long-lived socket is not closed by them; the live
#   tests pin that with deadlines shorter than the socket's life.
# - Developer tools (PR 4b-H) are an argument, never an environment variable:
#   --dev-tools turns them on and the ready line says devTools true or false,
#   and apiDocs true only when this host serves the docs page (the launcher
#   reports both; it never probes /docs to find out).
#   The API docs files come from --docs-assets DIR when it is given, else from
#   dev-tools/docs of the --root tree (through the tree, so a verified tree
#   yields verified bytes), else there are none and the host starts without the
#   docs page. An explicit directory that is missing or incomplete is a refusal
#   (exit 2, GUI_ASSETS_UNAVAILABLE); --docs-assets without --dev-tools is an
#   argument error, so no argument enables anything by implication.
# - Decision host-serves-daemon-in-process-20261119 (2822, B3): the daemon driver
#   is served in this process.  --home, --state-dir, --socket and --node go
#   together (all four or none; all are non-secret paths and a node id) and select
#   the in-process driver: the environment then carries the driver, the grant and
#   the launch paths only, and read_local_config refuses an endpoint or upstream
#   bearer there.  Without them the host reads its upstream
#   from the environment as before (the Contract Mock, the conformance suite's
#   fake upstream).  The daemon is attached after the listener is bound and
#   before the app is built, so a refusal (BACKEND_UNAVAILABLE,
#   DAEMON_OWNERSHIP_BUSY: exit 3, one JSON object on stderr, no ready line)
#   closes the listener and never announces.  A watcher that dies stops uvicorn
#   through should_exit and the host exits 3 (see local/__init__.py); the
#   shutdown order (watcher, server, live daemon host) is LocalDaemon.close,
#   after uvicorn has drained.
# - Decision w1e2-process-umask-20261121: serve() applies apply_private_umask()
#   once at GUI session host startup so any files created by the host or an
#   in-process daemon inherit 0o077 on POSIX.
"""Entry point: ``python -m hyprial.shell.impl.gui.host serve``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

from hyprial.kernel import apply_private_umask, ipc_errors

from .config import ConfigError, HostConfig, read_config, read_local_config

SERVICE = "hyprial-gui"
READY_PREFIX = "HYPRIAL_GUI_READY "
STOPPED_LINE = "HYPRIAL_GUI_STOPPED {}"
EXIT_REFUSED = 2
EXIT_BIND_FAILED = 4
GRACEFUL_SHUTDOWN_SECONDS = 2
BIND_BACKLOG = 128
MAX_CONNECTIONS = 256
MAX_HEADER_BYTES = 16_384
MAX_HEADER_FIELDS = 100
KEEP_ALIVE_SECONDS = 5
READ_IDLE_SECONDS = 10
REQUEST_DEADLINE_SECONDS = 15
LOOPBACK_BINDS = {"127.0.0.1": socket.AF_INET, "localhost": socket.AF_INET, "::1": socket.AF_INET6}


def _report(code: str, message: str) -> None:
    print(json.dumps({"ok": False, "code": code, "message": message}), file=sys.stderr, flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hyprial-gui-host", description="Same-origin GUI session host")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve", help="serve /daemon/* sessions and the built GUI")
    run.add_argument("--host", default="127.0.0.1", help="loopback address to bind")
    run.add_argument("--port", type=int, default=4173, help="port to bind; 0 picks a free port")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", type=Path, help="directory holding the built GUI")
    source.add_argument("--live-upstream", help="loopback Vite dev server origin to relay assets and HMR to")
    run.add_argument("--home", type=Path, help="GUI home of the daemon driver served in this process")
    run.add_argument("--state-dir", type=Path, help="daemon state directory (with --home)")
    run.add_argument("--socket", type=Path, help="daemon socket path (with --home)")
    run.add_argument("--node", help="verified daemon node id (with --home)")
    run.add_argument("--dev-tools", action="store_true", help="serve the developer tool layer and the API docs")
    run.add_argument("--docs-assets", type=Path, help="directory holding the Swagger UI files (with --dev-tools)")
    return parser


def _listen(host: str, port: int) -> socket.socket:
    family = LOOPBACK_BINDS[host]
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        listener.setsockopt(socket.SOL_SOCKET, exclusive if exclusive is not None else socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1" if host == "localhost" else host, port))
        listener.listen(BIND_BACKLOG)
    except OSError:
        listener.close()
        raise
    return listener


def _ready_payload(
    config: HostConfig, host: str, port: int, *, live: bool = False, dev_tools: bool = False, api_docs: bool = False
) -> dict[str, Any]:
    shown = "127.0.0.1" if host == "localhost" else host
    origin_host = f"[{shown}]" if ":" in shown else shown
    return {
        "service": SERVICE,
        "mode": "development" if live else "production",
        "dataMode": "hosted",
        "host": shown,
        "port": port,
        "url": f"http://{origin_host}:{port}",
        "driver": config.driver,
        "backendConfigured": True,
        "session": "launch-required",
        "devTools": dev_tools,
        "apiDocs": dev_tools and api_docs,
    }


PROTOCOL_HOOKS = {
    "connection_made": ("transport",),
    "connection_lost": ("exc",),
    "data_received": ("data",),
    "on_message_begin": (),
    "on_url": ("url",),
    "on_header": ("name", "value"),
    "on_headers_complete": (),
    "on_body": ("body",),
    "on_message_complete": (),
    "on_response_complete": (),
}


def _require_protocol_shape(base: type = HttpToolsProtocol) -> None:
    for name, parameters in PROTOCOL_HOOKS.items():
        hook = getattr(base, name, None)
        if not callable(hook):
            raise RuntimeError(f"uvicorn {base.__name__} has no {name}; the HTTP limits would be silently off")
        actual = tuple(inspect.signature(hook).parameters)[1:]
        if actual != parameters:
            raise RuntimeError(f"uvicorn {base.__name__}.{name} takes {actual}, expected {parameters}")


class BoundedHttpProtocol(HttpToolsProtocol):
    """httptools with header limits, read deadlines and no dispatch of a refused request."""

    def connection_made(self, transport: Any) -> None:
        super().connection_made(transport)
        self._receiving = True
        self._header_bytes = 0
        self._header_fields = 0
        self._refused = False
        self._refusal_pending = False
        self._idle_timer: asyncio.TimerHandle | None = None
        self._request_timer: asyncio.TimerHandle | None = None
        self._refresh_idle_deadline()

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        self._refresh_idle_deadline()

    def connection_lost(self, exc: Exception | None) -> None:
        self._stop_timer("_idle_timer")
        self._stop_timer("_request_timer")
        super().connection_lost(exc)

    def on_message_begin(self) -> None:
        if self._refused:
            return
        self._receiving = True
        self._header_bytes = 0
        self._header_fields = 0
        self._start_timer("_request_timer", REQUEST_DEADLINE_SECONDS)
        super().on_message_begin()

    def on_url(self, url: bytes) -> None:
        if not self._admit(len(url)):
            return
        super().on_url(url)

    def on_header(self, name: bytes, value: bytes) -> None:
        if self._refused:
            return
        self._header_fields += 1
        if self._header_fields > MAX_HEADER_FIELDS or not self._admit(len(name) + len(value) + 4):
            self._refuse()
            return
        super().on_header(name, value)

    def on_headers_complete(self) -> None:
        if not self._refused:
            super().on_headers_complete()

    def on_body(self, body: bytes) -> None:
        if not self._refused:
            super().on_body(body)

    def on_message_complete(self) -> None:
        self._receiving = False
        self._stop_timer("_request_timer")
        if not self._refused:
            super().on_message_complete()

    def on_response_complete(self) -> None:
        super().on_response_complete()
        if self._refusal_pending:
            self._refusal_pending = False
            self._unset_keepalive_if_required()
            self._write_refusal()

    def _admit(self, size: int) -> bool:
        if self._refused:
            return False
        self._header_bytes += size
        if self._header_bytes > MAX_HEADER_BYTES:
            self._refuse()
            return False
        return True

    def _in_flight(self) -> bool:
        if self.cycle is None or self.cycle.response_complete:
            return False
        return bool(self.pipeline) or not self.cycle.more_body

    def _refuse(self) -> None:
        if self._refused:
            return
        self._refused = True
        self._receiving = False
        self._stop_timer("_idle_timer")
        self._stop_timer("_request_timer")
        if self._in_flight():
            self.pipeline.clear()
            self._refusal_pending = True
            return
        self._write_refusal()

    def _write_refusal(self) -> None:
        if self.transport.is_closing():
            return
        self.transport.write(
            b"HTTP/1.1 431 Request Header Fields Too Large\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
        )
        self.transport.close()

    def _stop_timer(self, attribute: str) -> None:
        handle = getattr(self, attribute)
        if handle is not None:
            handle.cancel()
            setattr(self, attribute, None)

    def _start_timer(self, attribute: str, seconds: float) -> None:
        self._stop_timer(attribute)
        if not self.transport.is_closing():
            setattr(self, attribute, self.loop.call_later(seconds, self._expired, attribute, seconds))

    def _expired(self, attribute: str, seconds: float) -> None:
        setattr(self, attribute, None)
        if self.transport.is_closing():
            return
        if self._in_flight():
            self._start_timer(attribute, seconds)
            return
        self.transport.close()

    def _refresh_idle_deadline(self) -> None:
        if self._receiving:
            self._start_timer("_idle_timer", READ_IDLE_SECONDS)
        else:
            self._stop_timer("_idle_timer")


def _make_server(app: Any, listener: socket.socket, announce: Callable[[], None], *, websockets: bool = False) -> Any:
    import uvicorn

    _require_protocol_shape()

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            handled = (signal.SIGINT, signal.SIGTERM)
            previous = {sig: signal.signal(sig, self.handle_exit) for sig in handled}
            try:
                yield
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)

        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            if not self.should_exit:
                announce()

    return Server(
        uvicorn.Config(
            app,
            log_config=None,
            log_level="warning",
            access_log=False,
            server_header=False,
            date_header=False,
            lifespan="on",
            http=BoundedHttpProtocol,
            ws="websockets-sansio" if websockets else "none",
            proxy_headers=False,
            limit_concurrency=MAX_CONNECTIONS,
            timeout_keep_alive=KEEP_ALIVE_SECONDS,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    )


def serve(argv: Sequence[str] | None = None) -> int:
    """Run the host until it is stopped; return the process exit status."""
    apply_private_umask()
    args = _parser().parse_args(argv)
    if args.host not in LOOPBACK_BINDS:
        _report(ipc_errors.INVALID_ARGUMENT, "--host must be a loopback address: 127.0.0.1, localhost or ::1")
        return EXIT_REFUSED
    if not 0 <= args.port <= 65535:
        _report(ipc_errors.INVALID_ARGUMENT, "--port must be between 0 and 65535")
        return EXIT_REFUSED
    if args.docs_assets is not None and not args.dev_tools:
        _report(ipc_errors.INVALID_ARGUMENT, "--docs-assets is only valid with --dev-tools")
        return EXIT_REFUSED
    local_arguments = (args.home, args.state_dir, args.socket, args.node)
    in_process = args.home is not None
    if in_process != all(value is not None for value in local_arguments) or (
        not in_process and any(value is not None for value in local_arguments)
    ):
        _report(ipc_errors.INVALID_ARGUMENT, "--home, --state-dir, --socket and --node are given together")
        return EXIT_REFUSED
    try:
        config = read_local_config(os.environ) if in_process else read_config(os.environ)
    except ConfigError as error:
        _report("INVALID_CONFIG", str(error))
        return EXIT_REFUSED
    from .app import build_app
    from .web.assets import AssetTree, AssetRootError
    from .web.devtools import DOCS_TREE_DIRECTORY, DevTools, DocsAssetsError, collect_docs, read_docs_directory
    from .web.live import LiveOriginError, LiveUpstream

    assets = None
    live = None
    if args.live_upstream is not None:
        try:
            live = LiveUpstream(args.live_upstream)
        except LiveOriginError as error:
            _report(ipc_errors.INVALID_ARGUMENT, str(error))
            return EXIT_REFUSED
    else:
        try:
            assets = AssetTree.open(args.root, dev_tools=args.dev_tools)
        except AssetRootError as error:
            _report("GUI_ASSETS_UNAVAILABLE", str(error))
            return EXIT_REFUSED
    dev_tools = None
    if args.dev_tools:
        docs = None
        if args.docs_assets is not None:
            try:
                docs = read_docs_directory(args.docs_assets)
            except DocsAssetsError as error:
                _report("GUI_ASSETS_UNAVAILABLE", str(error))
                return EXIT_REFUSED
        elif assets is not None:
            tree = assets
            docs = collect_docs(lambda name: tree.read(f"{DOCS_TREE_DIRECTORY}/{name}"))
        dev_tools = DevTools(docs=docs)
    try:
        listener = _listen(args.host, args.port)
    except OSError as error:
        _report("GUI_BIND_FAILED", f"cannot listen on {args.host}:{args.port}: {error.strerror or error}")
        return EXIT_BIND_FAILED
    port = listener.getsockname()[1]
    payload = _ready_payload(
        config,
        args.host,
        port,
        live=live is not None,
        dev_tools=dev_tools is not None,
        api_docs=dev_tools is not None and dev_tools.api_docs(config.driver),
    )

    def announce() -> None:
        print(READY_PREFIX + json.dumps(payload), flush=True)

    local = None
    transport = None
    try:
        if in_process:
            from .local import EXIT_WATCHER_FAILED, LocalDaemon
            from ...request_http import StartupRefusal

            try:
                local = LocalDaemon.open(
                    config, home=args.home, state_dir=args.state_dir, socket=args.socket, node=args.node
                )
            except StartupRefusal as refusal:
                _report(refusal.code, refusal.message)
                return refusal.exit_code
            config, transport = local.config, local.transport
        app = build_app(
            config,
            assets,
            transport=transport,
            live=live,
            dev_tools=dev_tools,
            authority=None if local is None else local.authority,
        )
        server = _make_server(app, listener, announce, websockets=live is not None)
        if local is not None:
            local.stop_on_failure(lambda: setattr(server, "should_exit", True))
        asyncio.run(server.serve(sockets=[listener]))
    finally:
        listener.close()
        if local is not None:
            local.close()
    print(STOPPED_LINE, flush=True)
    failure = None if local is None else local.failure
    if failure is not None:
        _report("GUI_WATCHER_FAILED", "the principal watcher ended; the host stopped serving")
        return EXIT_WATCHER_FAILED
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return serve(argv)


if __name__ == "__main__":
    sys.exit(main())
