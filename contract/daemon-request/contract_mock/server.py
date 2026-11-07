"""Loopback HTTP surface for the Contract Mock.

Decision record (M-03, 2026-10-31)
- The mock listens only on a loopback address and requires a per-process
  synthetic bearer token.  A browser cannot self-report an identity; callers
  must copy the token printed at startup.  This mirrors the real transport's
  trusted-principal rule without reading any daemon identity.
- Path, Host, Origin, Content-Type and Content-Length are checked before the
  body is read.  The mock is a development tool, not a hardened public server,
  but it fails closed on the same framing mistakes the real entry rejects.
- ``/healthz`` and ``/mock/contract`` are mock metadata.  All business calls
  stay on ``POST /daemon/request/<method>`` so a generated client can switch
  from the mock to H without a code path change.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .contract import (
    DEFAULT_CONTRACT,
    DEFAULT_ENVELOPE,
    SCENARIOS,
    ContractError,
    load_contract,
)
from .envelope import Envelope
from .service import MockService, Response

logger = logging.getLogger("hyprial.contract_mock")
_METHOD_PATH = re.compile(r"^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")
MAX_BODY_BYTES = 1024 * 1024
_DEFAULT_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)
_FIXED_GET_PATHS = frozenset({"/healthz", "/mock/contract"})


class MockHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    service: MockService
    allowed_origins: frozenset[str]
    allowed_hosts: frozenset[str]


def _host_name(value: str) -> str:
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "HyprialContractMock/1"
    sys_version = ""

    @property
    def mock_server(self) -> MockHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _duplicate(self, name: str) -> bool:
        return len(self.headers.get_all(name) or []) > 1

    def _origin(self) -> str | None:
        return self.headers.get("Origin")

    def _cors_headers(self, origin: str | None) -> dict[str, str]:
        if origin is None or origin not in self.mock_server.allowed_origins:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Expose-Headers": "X-Hyprial-Request-Id",
        }

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        origin: str | None,
        *,
        request_id: str | None = None,
    ) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if request_id is not None:
            self.send_header("X-Hyprial-Request-Id", request_id)
        for name, value in self._cors_headers(origin).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_response(self, response: Response, origin: str | None) -> None:
        request_id = response.payload.get("requestId")
        self._send_json(
            response.status,
            response.payload,
            origin,
            request_id=request_id if isinstance(request_id, str) else None,
        )

    def _send_empty(self, status: int, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _failure(self, status: int, code: str, message: str, origin: str | None) -> None:
        response = self.mock_server.service.error_response(
            code, message, status=status
        )
        self._send_response(response, origin)

    def _validate_host(self, origin: str | None) -> bool:
        host = self.headers.get("Host")
        if not isinstance(host, str) or not host:
            self._failure(403, "HTTP_HOST_DENIED", "Host header is required", origin)
            return False
        if _host_name(host) not in self.mock_server.allowed_hosts:
            self._failure(403, "HTTP_HOST_DENIED", "Host is not allowed", origin)
            return False
        return True

    def _validate_origin(self, origin: str | None) -> bool:
        if origin is not None and origin not in self.mock_server.allowed_origins:
            self._failure(403, "HTTP_ORIGIN_DENIED", "Origin is not allowed", origin)
            return False
        return True

    def _validate_duplicates(self, origin: str | None) -> bool:
        for name in (
            "Host",
            "Origin",
            "Authorization",
            "Content-Type",
            "Content-Length",
            "X-Hyprial-Catalog-Digest",
        ):
            if self._duplicate(name):
                self._failure(
                    400,
                    "INVALID_REQUEST",
                    f"duplicate {name} header",
                    origin,
                )
                return False
        return True

    def _preflight(self, origin: str | None) -> None:
        if origin is None:
            self._failure(403, "HTTP_ORIGIN_DENIED", "preflight requires an Origin", None)
            return
        headers = self._cors_headers(origin)
        headers.update(
            {
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Authorization, Content-Type, X-Hyprial-Catalog-Digest"
                ),
                "Access-Control-Max-Age": "600",
                "Cache-Control": "no-store",
            }
        )
        self._send_empty(HTTPStatus.NO_CONTENT, headers)

    def _get(self, origin: str | None) -> None:
        path = self.path
        if path == "/healthz":
            self._send_json(200, {"ok": True}, origin)
            return
        service = self.mock_server.service
        if path == "/mock/contract":
            if not service.authorized(self.headers.get("Authorization")):
                self._send_response(service.unauthorized(), origin)
                return
            self._send_json(200, service.contract_document(), origin)
            return
        if path.startswith("/daemon/request/"):
            self._failure(405, "HTTP_METHOD_NOT_ALLOWED", "method is not allowed", origin)
            return
        self._failure(404, "METHOD_NOT_FOUND", "unknown path", origin)

    def _read_body(self, origin: str | None) -> bytes | None:
        if self._duplicate("Content-Type"):
            self._failure(400, "INVALID_REQUEST", "duplicate Content-Type header", origin)
            return None
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._failure(
                415,
                "HTTP_UNSUPPORTED_MEDIA_TYPE",
                "Content-Type must be application/json",
                origin,
            )
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._failure(411, "HTTP_LENGTH_REQUIRED", "Content-Length is required", origin)
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._failure(400, "INVALID_REQUEST", "Content-Length is not an integer", origin)
            return None
        if length < 0:
            self._failure(400, "INVALID_REQUEST", "Content-Length is negative", origin)
            return None
        if length > MAX_BODY_BYTES:
            self._failure(413, "HTTP_BODY_TOO_LARGE", "request body is too large", origin)
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._failure(400, "INVALID_REQUEST", "request body is truncated", origin)
            return None
        return body

    def _post(self, origin: str | None) -> None:
        service = self.mock_server.service
        if self.path in _FIXED_GET_PATHS:
            self._failure(405, "HTTP_METHOD_NOT_ALLOWED", "method is not allowed", origin)
            return
        if not self.path.startswith("/daemon/request/"):
            self._failure(404, "METHOD_NOT_FOUND", "unknown path", origin)
            return
        method = self.path[len("/daemon/request/") :]
        if not _METHOD_PATH.fullmatch(method):
            self._failure(404, "METHOD_NOT_FOUND", "unknown daemon method", origin)
            return
        if not service.authorized(self.headers.get("Authorization")):
            self._send_response(service.unauthorized(), origin)
            return
        body = self._read_body(origin)
        if body is None:
            return
        try:
            params = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_response(service.invalid_json(), origin)
            return
        self._send_response(
            service.handle(method, params, self.headers.get("Authorization")),
            origin,
        )

    def _route(self, command: str) -> None:
        origin = self._origin()
        if not self._validate_host(origin):
            return
        if not self._validate_origin(origin):
            return
        if not self._validate_duplicates(origin):
            return
        if "?" in self.path or "#" in self.path:
            self._failure(400, "INVALID_REQUEST", "query and fragment are not accepted", origin)
            return
        if command == "OPTIONS":
            self._preflight(origin)
            return
        if command == "GET":
            self._get(origin)
            return
        if command == "POST":
            self._post(origin)
            return
        self._failure(405, "HTTP_METHOD_NOT_ALLOWED", "method is not allowed", origin)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_OPTIONS(self) -> None:
        self._route("OPTIONS")


def create_server(
    service: MockService,
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
    allowed_origins: tuple[str, ...] = _DEFAULT_ORIGINS,
) -> MockHTTPServer:
    if not _is_loopback(host):
        raise ContractError("the Contract Mock only binds a loopback address")
    host_names = {"127.0.0.1", "localhost", "[::1]"}
    host_names.add(host)
    server = MockHTTPServer((host, port), _Handler)
    server.service = service
    server.allowed_origins = frozenset(allowed_origins)
    server.allowed_hosts = frozenset(host_names)
    return server


def _parse_pair(value: str, label: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator or not key or not item:
        raise ContractError(f"{label} must be METHOD=VALUE")
    return key, item


def _pairs(values: list[str] | None, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        key, item = _parse_pair(value, label)
        if key in result:
            raise ContractError(f"{label} repeats method {key!r}")
        result[key] = item
    return result


def _check(service: MockService, token: str) -> int:
    print(
        f"PASS contract {service.contract.digest} version "
        f"{service.contract.contract_version}"
    )
    for method in sorted(service.contract.methods):
        contract = service.contract.methods[method]
        available = ", ".join(contract.available_scenarios()) or "none"
        print(f"{method}: scenarios={available}")
    unsupported = service.unsupported_methods()
    if unsupported:
        print(
            "unsupported for the selected scenario: " + ", ".join(unsupported),
            file=sys.stderr,
        )
    for method in sorted(service.contract.methods):
        contract = service.contract.methods[method]
        case = service.selected_case(method)
        if case is None:
            print(f"{method}/{service.scenario}: not-declared")
            continue
        response = service.handle(method, case["params"], f"Bearer {token}")
        print(
            f"{method}/{case['name']}: {response.status} {case['kind']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--openapi", type=Path, default=DEFAULT_CONTRACT, help="business OpenAPI projection"
    )
    parser.add_argument(
        "--envelope",
        type=Path,
        default=DEFAULT_ENVELOPE,
        help="pinned HTTP envelope schema",
    )
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--method-scenario", action="append", default=[])
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--token", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--allow-origin", action="append", default=None, help="repeatable browser Origin"
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        contract = load_contract(args.openapi, args.envelope)
        envelope = Envelope(contract)
        token = args.token or envelope.token()
        service = MockService(
            contract,
            envelope,
            scenario=args.scenario,
            token=token,
            method_scenarios=_pairs(args.method_scenario, "--method-scenario"),
            method_cases=_pairs(args.case, "--case"),
        )
    except ContractError as error:
        print(f"BLOCKED {error}", file=sys.stderr)
        return 2

    if args.check:
        return _check(service, token)

    origins = tuple(args.allow_origin) if args.allow_origin else _DEFAULT_ORIGINS
    server = create_server(
        service, host=args.host, port=args.port, allowed_origins=origins
    )
    host, port = server.server_address[:2]
    browser_host = f"[{host}]" if ":" in str(host) else host
    print(
        f"hyprial Contract Mock (not the daemon, not H)\n"
        f"endpoint: http://{browser_host}:{port}/daemon/request/<method>\n"
        f"scenario: {service.scenario}\n"
        f"token: Bearer {token}\n"
        f"contract: {contract.digest} version {contract.contract_version}\n"
        f"envelope source: {contract.envelope_source.get('commit', 'unknown')}\n"
        f"allowed origins: {', '.join(sorted(origins))}\n"
        f"unsupported for this scenario: "
        f"{', '.join(service.unsupported_methods()) or 'none'}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
