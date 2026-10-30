"""Optional short-link minting for org invites.

The daemon owns the long invite; this shell-only edge exchanges it for a
``hyprial.ai/j`` short link using the operator's login access token.  A
short link is another secret, so this module never logs the token and the
caller degrades to the long link on every failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request

from hyprial.shell.impl.login.flow import (
    LoginError,
    read_login_credential,
    refresh_access_token,
)

SHORT_LINK_ENDPOINT = "https://hyprial.ai/j"
SHORT_LINK_TIMEOUT_SECONDS = 3.0
SHORT_LINK_RESPONSE_LIMIT_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ShortLinkUnavailable(Exception):
    """The short link could not be minted; ``notify`` is False when unlogged."""

    reason: str
    notify: bool = True

    def __str__(self) -> str:
        return self.reason


def _access_token(
    *, home: Path | None, environ: Mapping[str, str] | None, deadline: float
) -> str:
    from hyprial.daemon import resolve_profile  # public face of the profile

    try:
        credential = read_login_credential(environ, hyprial_home=home)
    except LoginError as error:
        raise ShortLinkUnavailable("SHORT_LINK_NOT_LOGGED_IN") from error
    profile, _source = resolve_profile(hyprial_home=home)
    if profile.issuer != credential.issuer:
        raise ShortLinkUnavailable(
            "login issuer does not match this hyprial home; short link skipped"
        )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ShortLinkUnavailable("SHORT_LINK_TIMEOUT")
    try:
        return refresh_access_token(
            profile,
            environ=environ,
            hyprial_home=home,
            timeout=remaining,
        )
    except LoginError as error:
        raise ShortLinkUnavailable(
            f"login credential could not be refreshed ({error.code}); short link skipped"
        ) from error


def _no_redirect_opener() -> Callable[..., Any]:
    """Keep the daemon package off the CLI import path until minting runs."""

    from hyprial.daemon import url_opener

    return url_opener(follow_redirects=False)[0]


def _read_bounded(response: Any, deadline: float) -> bytes:
    """Read a small response without letting a drip feed outlive the budget."""

    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ShortLinkUnavailable("SHORT_LINK_TIMEOUT")
        _set_socket_timeout(response, remaining)
        try:
            read = getattr(response, "read1", response.read)
            chunk = read(min(1024, SHORT_LINK_RESPONSE_LIMIT_BYTES + 1 - total))
        except (TimeoutError, socket.timeout) as error:
            raise ShortLinkUnavailable("SHORT_LINK_TIMEOUT") from error
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > SHORT_LINK_RESPONSE_LIMIT_BYTES:
            raise ShortLinkUnavailable("SHORT_LINK_TOO_LARGE")
        chunks.append(chunk)


def _set_socket_timeout(response: Any, remaining: float) -> None:
    raw = getattr(response, "fp", None)
    raw = getattr(raw, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None:
        try:
            sock.settimeout(remaining)
        except OSError:
            return


def shorten_invite_link(
    link: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    access_token: str | None = None,
    endpoint: str | None = None,
    timeout: float = SHORT_LINK_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
) -> str:
    """POST the long link as plain text to ``/j`` and return the short URL.

    The service owns the TTL (it parses the invite payload); there is no
    client-side expiresAt or JSON envelope.  Raises
    :class:`ShortLinkUnavailable` for every non-success; the caller keeps the
    long link.  This function never retries and never emits the access token.
    """

    outcome: list[tuple[str | None, str | None]] = []

    def worker() -> None:
        try:
            outcome.append((
                _shorten_invite_link(
                    link,
                    home=home,
                    environ=environ,
                    access_token=access_token,
                    endpoint=endpoint,
                    timeout=timeout,
                    opener=opener,
                ),
                None,
            ))
        except ShortLinkUnavailable as error:
            outcome.append((None, str(error)))
        except BaseException as error:  # noqa: BLE001 - class name only
            outcome.append((None, f"SHORT_LINK_FAILED ({type(error).__name__})"))

    thread = threading.Thread(
        target=worker, name="hyprial-short-link", daemon=True
    )
    thread.start()
    thread.join(max(0.0, timeout))
    if thread.is_alive() or not outcome:
        raise ShortLinkUnavailable("SHORT_LINK_TIMEOUT")
    short, reason = outcome[0]
    if reason is not None:
        # No `from error`: a raw transport exception can carry the bearer
        # token in its text.  Only this class name/reason reaches the caller.
        raise ShortLinkUnavailable(reason)
    assert short is not None
    return short


def _shorten_invite_link(
    link: str,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    access_token: str | None = None,
    endpoint: str | None = None,
    timeout: float = SHORT_LINK_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
) -> str:
    deadline = time.monotonic() + timeout
    token = access_token or _access_token(
        home=home, environ=environ, deadline=deadline
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ShortLinkUnavailable("SHORT_LINK_TIMEOUT")
    target = endpoint or os.environ.get("HYPRIAL_SHORTLINK_ENDPOINT") or SHORT_LINK_ENDPOINT
    request = Request(
        target,
        data=link.encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    open_call = opener or _no_redirect_opener()
    try:
        with open_call(request, timeout=remaining) as response:
            status = getattr(response, "status", 200)
            final_url = getattr(response, "geturl", lambda: target)()
            if final_url != target:
                raise ShortLinkUnavailable("SHORT_LINK_REDIRECT_REFUSED")
            raw = _read_bounded(response, deadline)
    except ShortLinkUnavailable:
        raise
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise ShortLinkUnavailable("SHORT_LINK_REDIRECT_REFUSED")
        raise ShortLinkUnavailable(
            f"short link service refused the invite (HTTP {error.code})"
        )
    except (URLError, TimeoutError, OSError) as error:
        reason = "timed out" if isinstance(error, TimeoutError) else "unreachable"
        raise ShortLinkUnavailable(f"short link service {reason}") from error
    except Exception as error:  # noqa: BLE001 - degrade, never mask invite
        raise ShortLinkUnavailable("short link service failed") from error
    if status != 201:
        raise ShortLinkUnavailable(
            f"short link service refused the invite (HTTP {status})"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
        short = payload.get("short")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as error:
        raise ShortLinkUnavailable("short link service returned invalid JSON") from error
    if not isinstance(short, str) or not short.startswith("https://"):
        raise ShortLinkUnavailable("short link service returned no short URL")
    target_sha = payload.get("targetSha256")
    expected = hashlib.sha256(link.encode("utf-8")).hexdigest()
    if target_sha != expected:
        raise ShortLinkUnavailable("SHORT_LINK_TARGET_MISMATCH")
    return short
