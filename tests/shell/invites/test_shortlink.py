"""Short-link minting is best-effort and never blocks the invite."""

from __future__ import annotations

import hashlib
import json
import select
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import URLError

import pytest

from hyprial.shell.impl.invites.shortlink import (
    ShortLinkUnavailable,
    shorten_invite_link,
)

LINK = "https://invite.hyprial.invalid/join#v1.secret"


class _J:
    def __init__(
        self, status: int = 201, payload: object | None = None, location: str | None = None
    ) -> None:
        self.status = status
        self.location = location
        self.payload = (
            payload
            if payload is not None
            else {
                "short": "https://hyprial.ai/s/abc",
                "targetSha256": hashlib.sha256(LINK.encode("utf-8")).hexdigest(),
            }
        )
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server name
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(
                    (
                        self.rfile.read(length).decode("utf-8"),
                        {key.lower(): value for key, value in self.headers.items()},
                    )
                )
                body = json.dumps(outer.payload).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                if outer.location is not None:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_J":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/j"


def test_short_link_posts_plain_url_with_bearer_token() -> None:
    with _J() as server:
        short = shorten_invite_link(
            LINK, access_token="test-token", endpoint=server.endpoint
        )
    assert short == "https://hyprial.ai/s/abc"
    body, headers = server.requests[0]
    assert body == LINK
    assert headers["authorization"] == "Bearer test-token"


@pytest.mark.parametrize("status", [200, 202, 401, 403, 429, 500])
def test_short_link_status_failures_degrade(status: int) -> None:
    with _J(status=status) as server:
        with pytest.raises(ShortLinkUnavailable, match=f"HTTP {status}"):
            shorten_invite_link(
                LINK, access_token="test-token", endpoint=server.endpoint
            )


@pytest.mark.parametrize(
    "error,reason",
    [
        (TimeoutError("slow"), "timed out"),
        (URLError("down"), "unreachable"),
    ],
)
def test_short_link_transport_failures_degrade(error: BaseException, reason: str) -> None:
    def opener(*args: object, **kwargs: object) -> Any:
        raise error

    with pytest.raises(ShortLinkUnavailable, match=reason):
        shorten_invite_link(
            LINK, access_token="test-token", opener=opener
        )


def test_short_link_requires_a_matching_target_sha() -> None:
    with _J(payload={
        "short": "https://hyprial.ai/s/abc",
        "targetSha256": "0" * 64,
    }) as server:
        with pytest.raises(ShortLinkUnavailable, match="SHORT_LINK_TARGET_MISMATCH"):
            shorten_invite_link(
                LINK, access_token="test-token", endpoint=server.endpoint
            )


def test_short_link_requires_a_short_url_in_the_answer() -> None:
    with _J(payload={
        "short": 7,
        "targetSha256": hashlib.sha256(LINK.encode("utf-8")).hexdigest(),
    }) as server:
        with pytest.raises(ShortLinkUnavailable, match="no short URL"):
            shorten_invite_link(
                LINK, access_token="test-token", endpoint=server.endpoint
            )


def test_unlogged_short_link_reports_its_reason(tmp_path) -> None:
    with pytest.raises(ShortLinkUnavailable) as failure:
        shorten_invite_link(LINK, home=tmp_path)
    assert failure.value.notify is True
    assert str(failure.value) == "SHORT_LINK_NOT_LOGGED_IN"


def test_short_link_token_acquisition_uses_the_shared_deadline(monkeypatch) -> None:
    import time
    from hyprial.shell.impl.invites import shortlink

    seen: list[float] = []

    def token(*, home, environ, deadline):
        seen.append(deadline - time.monotonic())
        return "test-token"

    monkeypatch.setattr(shortlink, "_access_token", token)
    with _J() as server:
        shorten_invite_link(
            LINK, endpoint=server.endpoint, timeout=2.0
        )
    assert seen and 0 < seen[0] <= 2.0


def test_corrupt_login_utf8_degrades_instead_of_raising(tmp_path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "login.json").write_bytes(b"\xff\xfe-not-utf8")
    with pytest.raises(ShortLinkUnavailable, match="SHORT_LINK_FAILED"):
        shorten_invite_link(LINK, home=tmp_path)


def test_slow_drip_response_cannot_outlive_the_total_budget() -> None:
    import time

    class DripHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server name
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "4096")
            self.end_headers()
            for _ in range(5):
                self.wfile.write(b" " * 40)
                self.wfile.flush()
                for _ in range(11):
                    if select.select([self.connection], [], [], 0.1)[0]:
                        return

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), DripHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        started = time.monotonic()
        with pytest.raises(ShortLinkUnavailable, match="SHORT_LINK_TIMEOUT"):
            shorten_invite_link(
                LINK,
                access_token="test-token",
                endpoint=f"http://{host}:{port}/j",
                timeout=3.0,
            )
        assert time.monotonic() - started <= 3.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_short_link_hard_deadline_discards_a_stuck_worker(monkeypatch) -> None:
    import time
    from hyprial.shell.impl.invites import shortlink

    def stuck(*args, **kwargs):
        time.sleep(5)
        return "https://hyprial.ai/s/too-late"

    monkeypatch.setattr(shortlink, "_shorten_invite_link", stuck)
    started = time.monotonic()
    with pytest.raises(ShortLinkUnavailable, match="SHORT_LINK_TIMEOUT"):
        shorten_invite_link(LINK, access_token="test-token", timeout=0.2)
    assert time.monotonic() - started < 1.0


def test_short_link_refuses_cross_origin_redirect_without_forwarding_token() -> None:
    with _J() as target:
        with _J(status=302, payload={}, location=target.endpoint) as redirect:
            with pytest.raises(ShortLinkUnavailable, match="SHORT_LINK_REDIRECT_REFUSED"):
                shorten_invite_link(
                    LINK,
                    access_token="test-token",
                    endpoint=redirect.endpoint,
                )
        assert target.requests == []
        assert redirect.requests
        assert redirect.requests[0][1]["authorization"] == "Bearer test-token"


def test_short_link_error_does_not_chain_a_token_bearing_exception(monkeypatch) -> None:
    import traceback
    from hyprial.shell.impl.invites import shortlink

    def broken(*args, **kwargs):
        raise RuntimeError("Bearer super-secret-token")

    monkeypatch.setattr(shortlink, "_shorten_invite_link", broken)
    with pytest.raises(ShortLinkUnavailable) as failure:
        shorten_invite_link(LINK, access_token="test-token", timeout=1)
    rendered = "".join(traceback.format_exception(failure.value))
    assert "super-secret-token" not in rendered
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_short_link_response_has_a_hard_size_limit() -> None:
    payload = {
        "short": "https://hyprial.ai/s/abc",
        "targetSha256": hashlib.sha256(LINK.encode("utf-8")).hexdigest(),
        "pad": "x" * 5000,
    }
    with _J(payload=payload) as server:
        with pytest.raises(ShortLinkUnavailable, match="SHORT_LINK_TOO_LARGE"):
            shorten_invite_link(
                LINK, access_token="test-token", endpoint=server.endpoint
            )
