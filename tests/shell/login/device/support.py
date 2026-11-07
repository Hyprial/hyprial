"``hyprial login`` identity stage against the in-repo fake OIDC (login U2).\n\nEvery test runs the real flow over a real threaded ``http.server``\n(``tests/fixtures/network/fake_oidc.py``) with only the *clock* injected where the\nassertion is about rhythm (T4) — everything else is real HTTP on loopback.\n\n⚠️ The autouse home fixture sets ``HYPRIAL_OWNER``; step 7 of the flow treats a\nnon-empty *different* override as fatal, so every test here clears or\nmatches it explicitly (``monkeypatch.setenv``/``delenv``) — the spec's\nfixture reminder, honored per-test instead of trusted silently.\n"

from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'tests/fixtures'))


from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.shell.impl.login.flow import (
    run_login,
)

_SRC_ROOT = Path(__file__).resolve().parents[4] / 'src'
_ISSUER_LITERAL = "auth.hyprial.com"


# -- helpers -------------------------------------------------------------------


def _profile_for(server, *, client_id: str = "fake-client-id") -> NetworkProfile:
    """The profile whose issuer is the fake server (plain http is the test
    seam: production issuers are https by profile validation)."""

    return NetworkProfile(
        org="acme",
        issuer=server.issuer,
        client_id=client_id,
    )


class _Clock:
    """Injectable clock+sleep pair: sleeping advances time instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def _snapshot(home: Path) -> dict[str, bytes | None]:
    snap: dict[str, bytes | None] = {}
    for path in sorted(home.rglob("*")):
        snap[str(path.relative_to(home))] = (
            path.read_bytes() if path.is_file() else None
        )
    return snap


def _run(server, home, *, env=None, profile=None, **kwargs):
    environ = {"HYPRIAL_HOME": str(home)}
    if env is not None:
        environ.update(env)
    environ.setdefault("HYPRIAL_OWNER", "")
    resolved_profile = profile or _profile_for(server)
    kwargs.setdefault("binding_profile", replace(resolved_profile))
    return run_login(
        resolved_profile,
        profile_source="profile.json",
        environ=environ,
        hyprial_home=home,
        open_browser=False,
        **kwargs,
    )


# -- T11: the daemon neither imports login nor reads the credential ------------------


_DAEMON_TREE = _SRC_ROOT / "hyprial" / "daemon"
_FORBIDDEN = re.compile(
    r"(?:from\s+hyprial\.login\s+import|import\s+hyprial\.login|from\s+hyprial\s+import\s+login|"
    r"login\.json|SECRETS_DIRNAME|read_login_credential|refresh_access_token|run_login)"
)


# -- Transport errors during the device-code poll ---------------------------------
#
# Observed on a member's macOS host while cutting over to self-hosted
# headscale: after the user had already authorised, a poll hit
# "[SSL: UNEXPECTED_EOF_WHILE_READING]" or "[Errno 54] Connection reset by
# peer" (the issuer sits behind Cloudflare; IPv4 reachability 2/6-4/6) and
# the whole login ended ISSUER_UNREACHABLE with a still-valid device code.
# Only access_denied / expired_token / invalid_client are terminal; a
# transport error while the code is unexpired is retried at the server's
# interval, bounded by the code's own expires_in.


def _fail_token_polls(monkeypatch, failures: dict[int, BaseException]) -> list[int]:
    """Make chosen token-endpoint polls (1-based) fail at the socket boundary."""

    from hyprial.shell.impl.login import oidc

    real_url_opener = oidc.url_opener
    seen: list[int] = []

    def url_opener(*args, **kwargs):
        open_url, route = real_url_opener(*args, **kwargs)

        def wrapped(request, *call_args, **call_kwargs):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if url.endswith("/api/token"):
                seen.append(len(seen) + 1)
                failure = failures.get(len(seen))
                if failure is not None:
                    raise failure
            return open_url(request, *call_args, **call_kwargs)

        return wrapped, route

    monkeypatch.setattr(oidc, "url_opener", url_opener)
    return seen


# -- An ALL_PROXY-only shell routes the login through the proxy -------------------
#
# urllib ignores ALL_PROXY for http/https (an "all" entry ProxyHandler never
# applies), so on an ALL_PROXY-only host every issuer request went DIRECT --
# the route the member's host measured at 2/6 and 4/6 -- while curl used the
# proxy.  hyprial.proxy_route applies the workers' rule to login too.


class _ForwardingProxy:
    """HTTP forward proxy: relays absolute-form requests and records them."""

    def __init__(self) -> None:
        import http.client
        import http.server
        import threading
        from urllib.parse import urlsplit as _split

        seen: list[str] = []
        self.seen = seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def _relay(self) -> None:
                seen.append(f"{self.command} {self.path}")
                target = _split(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                upstream = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
                try:
                    path = target.path + (f"?{target.query}" if target.query else "")
                    headers = {
                        k: v for k, v in self.headers.items()
                        if k.lower() not in {"proxy-connection", "connection", "host"}
                    }
                    upstream.request(self.command, path, body=body, headers=headers)
                    response = upstream.getresponse()
                    payload = response.read()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    upstream.close()

            do_GET = _relay  # noqa: N815 - stdlib hook names
            do_POST = _relay  # noqa: N815

            def log_message(self, *args: object) -> None:
                return None

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
