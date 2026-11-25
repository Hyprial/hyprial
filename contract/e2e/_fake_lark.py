"""A stand-in for Lark's OpenAPI, answering only what a scenario exercises.

Three separate questions a stand-in has to answer.  They are listed as three
because they are three -- each can be false while the other two are true, and
only the first is the one people remember to ask.

  1. Can the system under test still reach the real service?
     Answered by unreachability, not by inspection: the scenario redirects
     every Lark exit (``HYPRIAL_LARK_BASE_URL``) and strips the proxy variables
     that would otherwise intercept loopback traffic.

  2. Did the traffic actually arrive here?
     A missed call site behaves exactly as before -- it reaches production --
     and the test stays green, because green cannot show "never came to me".
     Answered by counting hits per route and asserting the expected ones are
     non-zero.

  3. Do the values this fake hands back send the system somewhere else?
     The subtle one, and the only one that stays false while 1 and 2 are true.
     A response body is an *input* to the SDK: the websocket handshake
     connects to a URL this fake chooses, and file/image routes hand back keys
     and locations the adapter dereferences later.  A stand-in is therefore a
     control plane, not just an observer -- it can point the SDK anywhere.
     Answered by asserting every URL emitted from here resolves to loopback,
     as a property of the fake rather than a check on one route.

⚠️ Routes are served only where a scenario exercises them.  Anything else gets
a loud 501 naming the path: a fake that answers plausibly for a route nobody
tested is indistinguishable from coverage of that route.

⚠️ ``lark_oapi``'s ``TokenManager.cache`` is a process-global ``LocalCache``
keyed by ``app_id`` alone -- the domain is not part of the key.  Two clients
with the same app id therefore share a tenant token even when one is pointed
here and the other at the vendor.  This module refuses to be handed a real
App's id (see :func:`assert_isolated_app_id`) so that crossing is impossible
rather than merely unlikely.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Callable

#: Deliberately not any App that exists.  The token cache above is keyed by
#: app id, so an id that no real App shares is what keeps a token minted here
#: from ever being presented to Lark, or a real token from being presented to
#: this fake.
FAKE_APP_ID = "cli_fake_e2e_standin"
FAKE_APP_SECRET = "fake-secret-not-a-credential"

#: The App ids that must never be handed to this fake.  Naming them is not the
#: protection -- the check below refuses anything that is not FAKE_APP_ID --
#: but it records why the rule exists.
PRODUCTION_APP_PREFIXES = ("cli_",)

_LOOPBACK = "127.0.0.1"


class FakeLarkError(AssertionError):
    """The fake was used in a way that would have weakened the scenario."""


def assert_isolated_app_id(app_id: str) -> None:
    """Refuse any id but the fake's own.

    Sharing an id with a real App would let the SDK's process-global token
    cache carry a token across the boundary in either direction.  Refusing is
    the only form of this check that cannot be satisfied by accident.
    """

    if app_id != FAKE_APP_ID:
        raise FakeLarkError(
            f"the fake Lark service refuses app id {app_id!r}: lark_oapi caches "
            "tenant tokens process-globally keyed by app id alone, so sharing "
            f"an id with a real App lets a token cross. Use {FAKE_APP_ID!r}."
        )


class _State:
    """What the fake remembers between requests, and what it counts."""

    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.unexpected: list[str] = []
        self.emitted_urls: list[str] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next_id = 0

    def record(self, route: str) -> None:
        with self._lock:
            self.hits[route] += 1

    def record_unexpected(self, path: str) -> None:
        with self._lock:
            self.unexpected.append(path)

    def mint_message_id(self) -> str:
        with self._lock:
            self._next_id += 1
            return f"om_fake{self._next_id:08d}"


def _route_key(method: str, path: str) -> str:
    """Collapse ids out of a path so counts are per route, not per request."""

    parts = []
    for segment in path.strip("/").split("/"):
        if segment.startswith(("om_", "oc_", "ou_", "img_", "file_")):
            parts.append(":id")
        else:
            parts.append(segment)
    return f"{method} /{'/'.join(parts)}"


class FakeLark:
    """A loopback Lark OpenAPI stand-in.

    ⚠️ Serves the REST surface plus the websocket *endpoint-discovery* request
    (``/callback/ws/endpoint``), which is what the SDK's readiness probe calls.
    It does NOT speak the websocket frame protocol, so inbound events do not
    arrive through it.  That is a separate piece of work, and until it exists a
    scenario needing real inbound events must not be described as running
    against this fake.
    """

    def __init__(self) -> None:
        self.state = _State()
        #: When a websocket stand-in is attached, endpoint discovery hands the
        #: SDK its address.  Left unset, discovery still answers -- readiness
        #: and health depend on that request alone -- with a loopback URL that
        #: accepts no connection, so a scenario that silently needed inbound
        #: events fails to connect instead of appearing to work.
        self._events_url: str | None = None
        handler = _build_handler(self)
        self._server = ThreadingHTTPServer((_LOOPBACK, 0), handler)
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-lark", daemon=True
        )

    def __enter__(self) -> FakeLark:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    # -- what a scenario asks the fake afterwards --------------------------

    def attach_events(self, url: str) -> None:
        """Point endpoint discovery at a websocket stand-in.

        Deliberately a URL rather than the object: this fake's only job is to
        answer with an address, and taking the address keeps it from growing a
        dependency on the event server's lifecycle.  The loopback assertion
        still applies -- the URL goes through the same recording path as every
        other URL emitted from here.
        """

        self._events_url = url

    def hits(self, route: str) -> int:
        return self.state.hits[route]

    def assert_routes_were_used(self, *routes: str) -> None:
        """Every named route must have been reached at least once.

        This is question 2.  Without it a call site that still goes to
        production is invisible: the assertion it was supposed to exercise
        passes against the real service, and nothing reports that the fake sat
        idle.
        """

        missing = [route for route in routes if not self.state.hits[route]]
        if missing:
            raise FakeLarkError(
                "these routes were never reached, so nothing proves the "
                f"traffic came here rather than to Lark: {', '.join(missing)}. "
                f"Reached: {dict(self.state.hits) or '(nothing)'}"
            )

    def assert_no_unserved_routes(self) -> None:
        """No request may have hit the 501 catch-all.

        A 501 means the system under test asked for something this fake was
        never taught, which is a gap in the fake -- not something to paper
        over with a plausible-looking body.
        """

        if self.state.unexpected:
            raise FakeLarkError(
                "the system under test called routes this fake does not serve: "
                + ", ".join(sorted(set(self.state.unexpected)))
            )

    def assert_every_emitted_url_is_loopback(self) -> None:
        """Question 3, as a property of the fake rather than of one route.

        Every URL handed back from here becomes an address the SDK may go to.
        Checking only the websocket URL would leave the next route that
        happens to emit one unguarded.
        """

        escapes = [
            url
            for url in self.state.emitted_urls
            if urllib.parse.urlparse(url).hostname not in {_LOOPBACK, "localhost"}
        ]
        if escapes:
            raise FakeLarkError(
                "this fake handed the SDK addresses outside loopback, which "
                f"would send it out of the sandbox: {escapes}"
            )

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Environment that points a child process here and keeps it here.

        The proxy variables matter more than they look: a shell HTTP proxy
        intercepts requests to loopback, the fake is never reached, and the
        symptom is a run that passes without ever having talked to anything --
        question 2 failing silently.
        """

        environment = dict(base if base is not None else {})
        environment["HYPRIAL_LARK_BASE_URL"] = self.base_url
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            environment.pop(name, None)
            environment.pop(name.lower(), None)
        environment["NO_PROXY"] = f"{_LOOPBACK},localhost"
        environment["no_proxy"] = environment["NO_PROXY"]
        return environment

    # -- responses ---------------------------------------------------------

    def _emit_url(self, url: str) -> str:
        self.state.emitted_urls.append(url)
        return url

    def respond(
        self, method: str, path: str, query: dict[str, list[str]], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        route = _route_key(method, path)
        segments = path.strip("/").split("/")

        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.state.record(route)
            return 200, {
                "code": 0,
                "msg": "ok",
                "tenant_access_token": "t-fake-tenant-token",
                "expire": 7200,
            }

        if path == "/open-apis/bot/v3/info":
            self.state.record(route)
            return 200, {
                "code": 0,
                "msg": "ok",
                "bot": {
                    "open_id": "ou_fake_bot",
                    "app_name": "fake-standin",
                    "activate_status": 2,
                },
            }

        if path == "/callback/ws/endpoint":
            self.state.record(route)
            # Question 3's first instance: the SDK connects to whatever host
            # this URL names.  Both branches build it from a loopback address
            # we control, and both record it so
            # assert_every_emitted_url_is_loopback can see it.
            if self._events_url is not None:
                return 200, {
                    "code": 0,
                    "msg": "ok",
                    "data": {"URL": self._emit_url(self._events_url)},
                }
            host, port = self._server.server_address[:2]
            url = self._emit_url(
                f"ws://{host}:{port}/ws?device_id=fake-device&service_id=1"
            )
            return 200, {"code": 0, "msg": "ok", "data": {"URL": url}}

        if path == "/open-apis/application/v6/scopes":
            self.state.record(route)
            return 200, {"code": 0, "msg": "ok", "data": {"scopes": []}}

        if path == "/open-apis/application/v6/scopes/apply":
            self.state.record(route)
            return 200, {"code": 0, "msg": "ok", "data": {}}

        if path == "/open-apis/im/v1/messages" and method == "POST":
            self.state.record(route)
            payload = _decode_json(body)
            message_id = self.state.mint_message_id()
            self.state.sent.append({"message_id": message_id, **payload})
            record = _message_record(message_id, payload)
            self.state.messages[message_id] = record
            return 200, {"code": 0, "msg": "ok", "data": record}

        if path == "/open-apis/im/v1/messages" and method == "GET":
            self.state.record(route)
            return 200, {
                "code": 0,
                "msg": "ok",
                "data": {
                    "items": list(self.state.messages.values()),
                    "has_more": False,
                    "page_token": "",
                },
            }

        if len(segments) == 5 and segments[3] == "messages" and method == "GET":
            self.state.record(route)
            record = self.state.messages.get(segments[4])
            if record is None:
                return 200, {"code": 230001, "msg": "message not found"}
            return 200, {"code": 0, "msg": "ok", "data": {"items": [record]}}

        if (
            len(segments) == 6
            and segments[3] == "messages"
            and segments[5] == "reply"
            and method == "POST"
        ):
            self.state.record(route)
            payload = _decode_json(body)
            message_id = self.state.mint_message_id()
            record = _message_record(message_id, payload, parent_id=segments[4])
            self.state.messages[message_id] = record
            self.state.sent.append({"message_id": message_id, **payload})
            return 200, {"code": 0, "msg": "ok", "data": record}

        if len(segments) == 6 and segments[5] == "reactions":
            self.state.record(route)
            if method == "POST":
                return 200, {
                    "code": 0,
                    "msg": "ok",
                    "data": {"reaction_id": "fake-reaction"},
                }
            return 200, {"code": 0, "msg": "ok", "data": {"items": []}}

        if len(segments) == 7 and segments[5] == "reactions" and method == "DELETE":
            self.state.record(route)
            return 200, {"code": 0, "msg": "ok", "data": {}}

        if path == "/open-apis/im/v1/chats" and method == "GET":
            self.state.record(route)
            return 200, {
                "code": 0,
                "msg": "ok",
                "data": {"items": [], "has_more": False, "page_token": ""},
            }

        if len(segments) == 6 and segments[3] == "chats" and segments[5] == "members":
            self.state.record(route)
            return 200, {
                "code": 0,
                "msg": "ok",
                "data": {"items": [], "has_more": False, "page_token": ""},
            }

        self.state.record_unexpected(f"{method} {path}")
        return 501, {
            "code": 599999,
            "msg": (
                f"the fake Lark service does not serve {method} {path}. "
                "Teach it this route deliberately; answering plausibly for an "
                "untested route would look exactly like coverage."
            ),
        }


def _decode_json(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _message_record(
    message_id: str, payload: dict[str, Any], *, parent_id: str | None = None
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "parent_id": parent_id or "",
        "root_id": parent_id or message_id,
        "chat_id": payload.get("receive_id", "oc_fake_chat"),
        "msg_type": payload.get("msg_type", "text"),
        "sender": {"id": "ou_fake_bot", "sender_type": "app"},
        "body": {"content": payload.get("content", "{}")},
        "deleted": False,
        "updated": False,
    }


def _build_handler(fake: FakeLark) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            status, payload = fake.respond(
                method, parsed.path, urllib.parse.parse_qs(parsed.query), raw
            )
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            self._dispatch("DELETE")

        def log_message(self, *_args: object) -> None:
            return  # the runner's report is the output, not an access log

    return _Handler


ClientFactory = Callable[[str, str], Any]
