"""Bounded-retry and network-failure classification policy.

The command module supplies its exception type so its public constants
and monkeypatch seams remain authoritative (same split as ``intranet``).
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# --- bounded retry for transient network failures -----------------------------

#: Publish run 18149 (2026-09-30) died ~5 s into the publish stage: the host
#: proxy's exit node timed out for a few seconds, the proxy re-selected a
#: healthy exit within minutes, and the tag's publish was lost until someone
#: re-dispatched it.  READ-ONLY network steps (the API credential check,
#: `git ls-remote`, `git clone`) therefore get a bounded retry: 3 attempts
#: total, 10 s then 30 s of backoff (worst-case added sleep: 40 s per
#: retried step, 120 s across all three -- well inside the job's
#: `timeout-minutes: 25`).
#:
#: Tag pushes use this same named bound only after a read-back proves whether
#: the prior attempt applied; branch pushes are never blindly retried.
NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_BACKOFF: tuple[float, ...] = (10.0, 30.0)

_URL_CREDENTIAL = re.compile(r"(://[^/@\s:]+:)[^@\s]+@")


def _git_failure_detail(stderr: str, token: str) -> str:
    """The whole git stderr, with any credential redacted.

    Keeping only the last line hid the cause: GitHub's reason (for example
    "refusing to allow a Personal Access Token to create or update workflow
    ... without `workflow` scope") comes before the generic "failed to push
    some refs" line.  The token normally travels through GIT_ASKPASS and never
    appears here; both redactions are a second line of defence.
    """

    text = stderr.strip()
    if not text:
        return "unknown"
    if token:
        text = text.replace(token, "***")
    return _URL_CREDENTIAL.sub(r"\1***@", text)


def _run(
    argv: list[str], *, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=None if cwd is None else str(cwd), env=env,
        capture_output=True, text=True,
    )


def backoff_schedule(env: dict[str, str], error_type) -> tuple[float, ...]:
    """The retry backoff, overridable via ``GH_PUBLISH_RETRY_BACKOFF``.

    The override exists so the subprocess-level tests can exercise the retry
    path without paying real seconds; production runs use
    :data:`NETWORK_RETRY_BACKOFF`.
    """
    raw = env.get("GH_PUBLISH_RETRY_BACKOFF", "").strip()
    if not raw:
        return NETWORK_RETRY_BACKOFF
    try:
        values = tuple(float(part) for part in raw.split(","))
    except ValueError as exc:
        raise error_type(
            f"GH_PUBLISH_RETRY_BACKOFF must be comma-separated seconds, got {raw!r}"
        ) from exc
    if not values or any(v < 0 for v in values):
        raise error_type(
            f"GH_PUBLISH_RETRY_BACKOFF must be non-negative seconds, got {raw!r}"
        )
    return values


def _backoff_for(attempt: int, backoff: tuple[float, ...]) -> float:
    """Backoff before attempt ``attempt + 1`` (attempt is 1-based)."""
    return backoff[min(attempt - 1, len(backoff) - 1)]


_GIT_AUTH_FAILURE = re.compile(r"Authentication failed|returned error: 40[13]")
_GIT_TRANSIENT_NETWORK = re.compile(
    r"Could not resolve host"
    r"|Failed to connect"
    r"|Connection timed out"
    r"|Connection reset"
    r"|unable to access"
    r"|expected flush after ref listing"
    r"|early EOF"
    r"|RPC failed"
    r"|The requested URL returned error: 5\d\d"
)


def is_transient_git_network_error(stderr: str) -> bool:
    """Classify the stderr of a READ-ONLY git network command.

    Connection-class failures (proxy exit-node timeouts, DNS hiccups, 5xx)
    are worth a bounded retry.  Auth failures (``Authentication failed``,
    401/403) and everything else fail immediately -- a wrong token never
    gets healthier by waiting.  The auth check runs first because a 401/403
    response also travels inside an ``unable to access ...`` line.
    """
    if _GIT_AUTH_FAILURE.search(stderr):
        return False
    return _GIT_TRANSIENT_NETWORK.search(stderr) is not None


def _run_git_network(
    argv: list[str],
    *,
    env: dict[str, str],
    label: str,
    token: str,
    sleep: Callable[[float], None],
    backoff: tuple[float, ...],
    cleanup: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a read-only git network command with bounded transient retry.

    ``cleanup`` runs before each retry (a retried clone must first remove
    its partial target directory).
    """
    attempts = NETWORK_RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        result = _run(argv, env=env)
        if result.returncode == 0:
            return result
        if attempt < attempts and is_transient_git_network_error(result.stderr):
            wait = _backoff_for(attempt, backoff)
            detail = _git_failure_detail(result.stderr, token).splitlines()[-1]
            print(
                f"{label}: attempt {attempt}/{attempts} failed ({detail}); "
                f"retrying in {wait:g}s"
            )
            if cleanup is not None:
                cleanup()
            sleep(wait)
            continue
        return result
    raise AssertionError("unreachable: the retry loop always returns")


def _is_transient_network_error(exc: BaseException) -> bool:
    """True for transient network failures worth one more attempt.

    ``HTTPError`` is NOT transient: 401, 403 and 404 mean a token or repo
    problem and must fail on the first attempt, exactly as before this
    retry existed.  (``HTTPError`` is a ``URLError`` subclass, hence the
    explicit exclusion.)
    """
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(
        exc,
        (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout),
    )


def api_credential_check(
    api_origin: str,
    repo_slug: str,
    env: dict[str, str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    backoff: tuple[float, ...] = NETWORK_RETRY_BACKOFF,
    opener: Callable[..., Any] | None = None,
    error_type=Exception,
) -> None:
    """Verify the token against the GitHub API (an anonymous-readable repo
    would make a bare `git ls-remote` uninformative about the token).

    Only the HTTP status is logged; the token itself is passed via a
    header built from the environment and is never echoed.  Retry lines
    carry the attempt number and the exception CLASS, never the exception
    message (which can embed the request context).

    Transient network failures (publish run 18149: the proxy's exit node
    timed out for seconds) are retried up to :data:`NETWORK_RETRY_ATTEMPTS`
    times; ``sleep``/``opener`` are injectable so tests do not sleep.
    """
    import urllib.error
    import urllib.request

    if opener is None:
        opener = urllib.request.urlopen

    attempts = NETWORK_RETRY_ATTEMPTS
    code = 0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            f"{api_origin}/repos/{repo_slug}",
            headers={"Authorization": f"Bearer {env['GH_PUBLISH_TOKEN']}"},
            method="GET",
        )
        # Anonymous proxies in the environment apply, same egress as the push.
        try:
            with opener(request, timeout=60) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            raise error_type(
                f"credential check: API returned {exc.code} for {repo_slug}; "
                "token rejected or repo invisible"
            ) from exc
        except OSError as exc:
            if not _is_transient_network_error(exc):
                raise error_type(
                    f"credential check: cannot reach {api_origin} "
                    f"({exc.__class__.__name__})"
                ) from exc
            if attempt < attempts:
                wait = _backoff_for(attempt, backoff)
                print(
                    f"credential check: attempt {attempt}/{attempts} cannot reach "
                    f"{api_origin} ({exc.__class__.__name__}); retrying in {wait:g}s"
                )
                sleep(wait)
                continue
            raise error_type(
                f"credential check: cannot reach {api_origin} "
                f"({exc.__class__.__name__}) after {attempts} attempts"
            ) from exc
        break
    print(f"credential check: API {code} for {repo_slug}")
