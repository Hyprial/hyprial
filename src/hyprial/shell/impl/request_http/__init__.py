"""Local HTTP request entry: one explicit config, one daemon, safe unary reads."""

from __future__ import annotations

import signal
import threading
from pathlib import Path

from .config import ConfigFailure, RequestHttpConfig, load_config
from .server import RequestHttpServer, StartupRefusal

__all__ = [
    "ConfigFailure",
    "RequestHttpConfig",
    "RequestHttpServer",
    "StartupRefusal",
    "load_config",
    "serve",
]


def serve(config_path: Path) -> int:
    """Run the foreground transport until stopped, then drain and clean.

    Only a ``local-operator`` config is served here, with the daemon-backed
    request path.  A ``local-gui`` config is refused (``CONFIG_INVALID``, exit 2):
    the GUI host serves that principal in its own process, so no child process
    of this entry carries it.

    The lifecycle is signal-only: SIGTERM or SIGINT drains and cleans.
    """

    try:
        config = load_config(config_path)
        if config.principal_kind == "local-gui":
            raise ConfigFailure(
                "CONFIG_INVALID",
                "a local-gui config is served by the GUI host, not by this entry",
            )
        return _serve_server(RequestHttpServer(config))
    except ConfigFailure as failure:
        _report(failure.code, failure.message)
        return failure.exit_code
    except StartupRefusal as refusal:
        _report(refusal.code, refusal.message)
        return refusal.exit_code


def _serve_server(server: RequestHttpServer) -> int:
    stop = threading.Event()

    def _request_stop(signum: int, frame: object) -> None:
        stop.set()

    previous: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, _request_stop)
    try:
        server.start()
        while not stop.wait(0.25):
            pass
    except ConfigFailure as failure:
        _report(failure.code, failure.message)
        return failure.exit_code
    except StartupRefusal as refusal:
        _report(refusal.code, refusal.message)
        return refusal.exit_code
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        server.shutdown()
    return 0


def _report(code: str, message: str) -> None:
    import json
    import sys

    print(json.dumps({"ok": False, "code": code, "message": message}), file=sys.stderr, flush=True)
